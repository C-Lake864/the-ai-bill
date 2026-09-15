"""2단계: 선별 (예선 -> 본선).

예선(prescreen)
  제목 + 피드 요약만 보고 0~10점. 값싼 모델, 8건씩 묶어서 호출.
  묶는 이유: 같은 배치 안의 기사끼리 상대 비교가 되므로 점수 분포가 덜 흔들린다.
  8을 넘기면 뒤쪽 기사 평가가 뭉개지고(위치 편향), 4 이하면 호출 수만 늘어난다.

본선(finalscreen)
  본문 전체를 읽고 audience.yaml 의 5개 차원을 각각 0~10점으로 매긴다. 4건씩 묶는다.
  본문이 들어가 배치당 토큰이 커지므로 예선보다 작게 잡는다.

두 단계 모두 기사별 점수/사유/라벨을 남긴다. 이게 '선별 기준이 동작했다'는 증거다.
"""
from __future__ import annotations

import json

from .llm import LLMError, chat_json

PRESCREEN_SYS = """너는 뉴스레터 편집자다. 기사 후보를 빠르게 걸러낸다.
반드시 JSON 객체 하나만 출력한다. 형식:
{"items":[{"idx":0,"score":7,"labels":["policy"],"reason":"한 문장 이유"}]}
- score: 0~10 정수. 타깃 독자 기준 유용성.
- labels: policy|tech|market|science|korea|corporate|lifestyle|event|opinion 중 해당되는 것만.
- 제외 규칙에 걸리는 기사는 score 0~2 와 사유를 준다.
- 모든 입력 기사에 대해 빠짐없이 idx 를 채워 응답한다."""

FINAL_SYS = """너는 뉴스레터 편집장이다. 본문을 읽고 차원별로 채점한다.
반드시 JSON 객체 하나만 출력한다. 형식:
{"items":[{"idx":0,"scores":{"impact":7,"actionability":6,"novelty":8,"evidence":9,"korea_relevance":3},
           "verdict":"keep","reason":"두 문장 이내","one_line":"기사 한 줄 요약"}]}
- 각 점수는 0~10 정수. 본문에 근거가 없으면 낮게 준다.
- verdict: keep | drop. 제외 규칙에 해당하면 drop.
- 추측하지 말고 본문에 실제로 있는 내용만 근거로 삼는다."""


def _fmt_prescreen(batch: list) -> str:
    lines = []
    for i, it in enumerate(batch):
        lines.append(
            f"[{i}] 소스:{it['source_name']}({it['bucket']}) / {it['age_hours']}시간 전\n"
            f"제목: {it['title']}\n요약: {it['feed_summary'][:400]}"
        )
    return "\n\n".join(lines)


def prescreen(items: list, audience: dict, log) -> list:
    """items 에 prescreen_score / prescreen_labels / prescreen_reason 을 붙여 반환."""
    cfg = audience["scoring"]["prescreen"]
    bs = cfg["batch_size"]
    header = (
        f"[타깃 독자]\n{audience['audience']['persona']}\n"
        f"이미 아는 것: {audience['audience']['knows_already']}\n"
        f"원하는 것: {audience['audience']['needs']}\n\n"
        f"[중요도 판단 기준]\n{audience['importance_statement']}\n\n"
        f"[제외 규칙]\n{audience['exclude']['editorial_rules']}\n"
    )

    scored = []
    for start in range(0, len(items), bs):
        batch = items[start:start + bs]
        user = header + "\n[기사 후보]\n" + _fmt_prescreen(batch)
        try:
            res = chat_json("prescreen", cfg["model_tier"], PRESCREEN_SYS, user, max_tokens=1500)
            by_idx = {int(r["idx"]): r for r in res.get("items", []) if "idx" in r}
        except (LLMError, ValueError, KeyError) as ex:
            # 배치 하나가 실패해도 파이프라인은 계속 간다. 해당 배치는 '보류 점수'로 통과시켜
            # 본선에서 본문으로 다시 판단하게 한다 (조용히 버리지 않는다).
            log(f"  ! 예선 배치 {start//bs} 실패 -> 보류 처리: {ex}")
            by_idx = {}
        for i, it in enumerate(batch):
            r = by_idx.get(i)
            if r is None:
                scored.append({**it, "prescreen_score": cfg["min_score"], "prescreen_labels": ["llm_failed"],
                               "prescreen_reason": "예선 LLM 응답 누락 -> 보류 통과"})
            else:
                scored.append({**it, "prescreen_score": int(r.get("score", 0)),
                               "prescreen_labels": r.get("labels", []),
                               "prescreen_reason": str(r.get("reason", ""))[:200]})
    return scored


def _fmt_final(batch: list, body_chars: int) -> str:
    lines = []
    for i, it in enumerate(batch):
        lines.append(
            f"[{i}] 소스:{it['source_name']} / {it['age_hours']}시간 전\n"
            f"제목: {it['title']}\n본문:\n{it['body'][:body_chars]}"
        )
    return "\n\n---\n\n".join(lines)


def finalscreen(items: list, audience: dict, log, body_chars: int = 5000) -> list:
    cfg = audience["scoring"]["finalscreen"]
    dims = cfg["dimensions"]
    bs = cfg["batch_size"]
    dim_desc = "\n".join(f"- {k} (가중치 {v['weight']}): {v['desc']}" for k, v in dims.items())
    header = (
        f"[타깃 독자]\n{audience['audience']['persona']}\n원하는 것: {audience['audience']['needs']}\n\n"
        f"[중요도 판단 기준]\n{audience['importance_statement']}\n\n"
        f"[채점 차원]\n{dim_desc}\n\n[제외 규칙]\n{audience['exclude']['editorial_rules']}\n"
    )

    out = []
    for start in range(0, len(items), bs):
        batch = items[start:start + bs]
        user = header + "\n[기사]\n" + _fmt_final(batch, body_chars)
        try:
            res = chat_json("finalscreen", cfg["model_tier"], FINAL_SYS, user, max_tokens=2500)
            by_idx = {int(r["idx"]): r for r in res.get("items", []) if "idx" in r}
        except (LLMError, ValueError, KeyError) as ex:
            log(f"  ! 본선 배치 {start//bs} 실패 -> 해당 배치 제외: {ex}")
            by_idx = {}
        for i, it in enumerate(batch):
            r = by_idx.get(i)
            if r is None:
                out.append({**it, "final_scores": {}, "weighted": 0.0, "verdict": "drop",
                            "final_reason": "본선 LLM 응답 누락", "one_line": ""})
                continue
            sc = {k: float(r.get("scores", {}).get(k, 0)) for k in dims}
            weighted = sum(sc[k] * dims[k]["weight"] for k in dims) * it.get("weight", 1.0)
            out.append({**it, "final_scores": sc, "weighted": round(min(weighted, 10.0), 2),
                        "verdict": r.get("verdict", "keep"),
                        "final_reason": str(r.get("reason", ""))[:300],
                        "one_line": str(r.get("one_line", ""))[:200]})
    return out


def cap_pick(ranked: list, cap: int, limit: int, key: str = "source_id") -> tuple[list, list]:
    """점수 순서를 지키되 소스당 cap 건까지만 뽑는다. (뽑힌 것, 남은 것)

    한 매체가 목록을 독식하면 '뉴스레터'가 아니라 '그 매체 요약본'이 된다.
    실제로 1차 실행에서 선정 5건이 전부 같은 소스에서 나와 이 규칙을 넣었다.
    """
    picked, rest, cnt = [], [], {}
    for it in ranked:
        k = it.get(key)
        if len(picked) < limit and cnt.get(k, 0) < cap:
            cnt[k] = cnt.get(k, 0) + 1
            picked.append(it)
        else:
            rest.append(it)
    return picked, rest


def select(scored: list, audience: dict) -> tuple[list, list]:
    """본선 점수로 최종 선정. (선정, 대기 후보) 를 반환한다.

    대기 후보는 검수 실패 시 빈자리를 메우는 데 쓴다.
    """
    cfg = audience["scoring"]["finalscreen"]
    ranked = sorted([s for s in scored if s["verdict"] == "keep"],
                    key=lambda x: -x["weighted"])
    passing = [s for s in ranked if s["weighted"] >= cfg["min_weighted"]]
    below = [s for s in ranked if s["weighted"] < cfg["min_weighted"]]

    chosen, rest = cap_pick(passing, cfg.get("max_per_source", 2), cfg["select_max"])

    # 소스 상한 때문에 최소 건수를 못 채우면, 상한을 풀어 점수 순으로 채운다
    if len(chosen) < cfg["select_min"]:
        for it in rest[:]:
            if len(chosen) >= cfg["select_min"]:
                break
            chosen.append(it)
            rest.remove(it)

    return chosen, rest + below
