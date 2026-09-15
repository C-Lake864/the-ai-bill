"""2단계: 선별 (예선 -> 본선).

예선(prescreen)
  제목 + 피드 요약만 보고 0~10점. 값싼 모델, 8건씩 묶어서 호출.
  묶는 이유: 같은 배치 안의 기사끼리 상대 비교가 되므로 점수 분포가 덜 흔들린다.
  8을 넘기면 절감 효과는 포화되고 응답 누락 위험만 커진다 (store/batch_experiment.json).

본선(finalscreen)
  본문 전체를 읽고 audience.yaml 의 차원들을 각각 0~10점으로 매긴다. 4건씩 묶는다.
  본문이 들어가 배치당 토큰이 커지므로 예선보다 작게 잡는다.

배치 응답은 순번(idx)이 아니라 **ref 식별자**로 되찾는다.
  위치로 매핑하면 모델이 배치 안의 기사 일부를 빠뜨렸을 때 순번이 밀려서
  '다른 기사의 점수와 사유가 이 기사에 붙는' 오배치가 난다. 실제로 본선에서 발생했다
  (12건 중 6건 누락, 누락분의 점수가 엉뚱한 기사에 부착). ref 로 매핑하고,
  누락된 기사는 개별 재요청으로 되찾는다.

두 단계 모두 기사별 점수/사유/라벨을 남긴다. 이게 '선별 기준이 동작했다'는 증거다.
"""
from __future__ import annotations

import re

from .llm import LLMError, chat_json

PRESCREEN_SYS = """너는 뉴스레터 편집자다. 기사 후보를 빠르게 걸러낸다.
반드시 JSON 객체 하나만 출력한다. 형식:
{"items":[{"ref":"A0","score":7,"labels":["policy"],"reason":"한 문장 이유"}]}
- ref: 입력에 붙은 식별자를 그대로 복사한다. 새로 만들거나 순서를 바꾸지 않는다.
- score: 0~10 정수. 타깃 독자 기준 유용성.
- labels: policy|tech|market|science|korea|corporate|lifestyle|event|opinion 중 해당되는 것만.
- 제외 규칙에 걸리는 기사는 score 0~2 와 사유를 준다.
- 입력에 있는 모든 ref 에 대해 빠짐없이 항목을 만든다. 하나라도 빠뜨리면 안 된다."""

FINAL_SYS = """너는 뉴스레터 편집장이다. 본문을 읽고 차원별로 채점한다.
반드시 JSON 객체 하나만 출력한다. 형식:
{"items":[{"ref":"A0","scores":{"<차원명>":7},"verdict":"keep",
           "reason":"두 문장 이내","one_line":"기사 한 줄 요약"}]}
- ref: 입력에 붙은 식별자를 그대로 복사한다. 새로 만들거나 순서를 바꾸지 않는다.
- scores: 아래 [채점 차원]에 나온 이름을 키로, 0~10 정수를 값으로 준다.
  본문에 근거가 없으면 낮게 준다.
- verdict: keep | drop. 제외 규칙에 해당하면 drop.
- 추측하지 말고 본문에 실제로 있는 내용만 근거로 삼는다.
- 입력에 있는 모든 ref 에 대해 빠짐없이 항목을 만든다. drop 이어도 반드시 항목을 넣는다."""


def _refs(batch: list) -> list:
    """배치 안에서만 유효한 짧은 식별자."""
    return [f"A{i}" for i in range(len(batch))]


def _by_ref(res: dict, refs: list) -> dict:
    """LLM 응답을 ref -> 항목 으로 정리한다. 모르는 ref 와 중복 ref 는 버린다."""
    valid, out = set(refs), {}
    for r in res.get("items", []) or []:
        if not isinstance(r, dict):
            continue
        ref = str(r.get("ref", "")).strip().upper()
        if ref in valid and ref not in out:
            out[ref] = r
    return out


def _run_batches(items: list, batch_size: int, ask, log, stage: str) -> dict:
    """items 를 batch_size 로 나눠 ask(batch) 를 부르고, 누락분은 개별 재요청한다.

    ask(batch) 는 {ref: 응답항목} 를 돌려준다. 반환값은 {item_id: 응답항목}.
    """
    answers = {}
    for start in range(0, len(items), batch_size):
        batch = items[start:start + batch_size]
        refs = _refs(batch)
        got = ask(batch)
        for ref, it in zip(refs, batch):
            if ref in got:
                answers[it["id"]] = got[ref]

        missing = [it for it in batch if it["id"] not in answers]
        if missing:
            log(f"  ! {stage} 응답 누락 {len(missing)}건 -> 개별 재요청")
            for it in missing:
                one = ask([it])
                if "A0" in one:
                    answers[it["id"]] = one["A0"]
    return answers


def _fmt_prescreen(batch: list, refs: list) -> str:
    return "\n\n".join(
        f"[{ref}] 소스:{it['source_name']}({it['bucket']}) / {it['age_hours']}시간 전\n"
        f"제목: {it['title']}\n요약: {it['feed_summary'][:400]}"
        for ref, it in zip(refs, batch)
    )


def prescreen(items: list, audience: dict, log) -> list:
    """items 에 prescreen_score / prescreen_labels / prescreen_reason 을 붙여 반환."""
    cfg = audience["scoring"]["prescreen"]
    header = (
        f"[타깃 독자]\n{audience['audience']['persona']}\n"
        f"이미 아는 것: {audience['audience']['knows_already']}\n"
        f"원하는 것: {audience['audience']['needs']}\n\n"
        f"[중요도 판단 기준]\n{audience['importance_statement']}\n\n"
        f"[제외 규칙]\n{audience['exclude']['editorial_rules']}\n"
    )

    def ask(batch):
        refs = _refs(batch)
        user = header + "\n[기사 후보]\n" + _fmt_prescreen(batch, refs)
        try:
            res = chat_json("prescreen", cfg["model_tier"], PRESCREEN_SYS, user, max_tokens=2000)
            return _by_ref(res, refs)
        except (LLMError, ValueError, KeyError) as ex:
            log(f"  ! 예선 배치 실패: {ex}")
            return {}

    answers = _run_batches(items, cfg["batch_size"], ask, log, "예선")

    scored = []
    for it in items:
        r = answers.get(it["id"])
        if r is None:
            # 끝까지 못 받아도 조용히 버리지 않는다. 보류 점수로 본선에 올려 본문으로 다시 본다.
            scored.append({**it, "prescreen_score": cfg["min_score"],
                           "prescreen_labels": ["llm_failed"],
                           "prescreen_reason": "예선 LLM 응답 누락 -> 보류 통과"})
        else:
            try:
                score = int(float(r.get("score", 0)))
            except (TypeError, ValueError):
                score = 0
            scored.append({**it, "prescreen_score": score,
                           "prescreen_labels": r.get("labels", []) or [],
                           "prescreen_reason": str(r.get("reason", ""))[:200]})
    return scored


def _fmt_final(batch: list, refs: list, body_chars: int) -> str:
    return "\n\n---\n\n".join(
        f"[{ref}] 소스:{it['source_name']} / {it['age_hours']}시간 전\n"
        f"제목: {it['title']}\n본문:\n{it['body'][:body_chars]}"
        for ref, it in zip(refs, batch)
    )


def finalscreen(items: list, audience: dict, log, body_chars: int = 5000) -> list:
    cfg = audience["scoring"]["finalscreen"]
    dims = cfg["dimensions"]
    dim_desc = "\n".join(f"- {k} (가중치 {v['weight']}): {v['desc']}" for k, v in dims.items())
    header = (
        f"[타깃 독자]\n{audience['audience']['persona']}\n원하는 것: {audience['audience']['needs']}\n\n"
        f"[중요도 판단 기준]\n{audience['importance_statement']}\n\n"
        f"[채점 차원]\n{dim_desc}\n\n[제외 규칙]\n{audience['exclude']['editorial_rules']}\n"
    )

    def ask(batch):
        refs = _refs(batch)
        user = header + "\n[기사]\n" + _fmt_final(batch, refs, body_chars)
        try:
            res = chat_json("finalscreen", cfg["model_tier"], FINAL_SYS, user, max_tokens=3500)
            return _by_ref(res, refs)
        except (LLMError, ValueError, KeyError) as ex:
            log(f"  ! 본선 배치 실패: {ex}")
            return {}

    answers = _run_batches(items, cfg["batch_size"], ask, log, "본선")

    out, n_missing = [], 0
    for it in items:
        r = answers.get(it["id"])
        if r is None:
            n_missing += 1
            out.append({**it, "final_scores": {}, "weighted": 0.0, "verdict": "drop",
                        "final_reason": "본선 LLM 응답 누락 (개별 재요청도 실패)", "one_line": ""})
            continue
        raw = r.get("scores", {}) or {}
        sc = {}
        for k in dims:
            try:
                sc[k] = float(raw.get(k, 0))
            except (TypeError, ValueError):
                sc[k] = 0.0
        weighted = sum(sc[k] * dims[k]["weight"] for k in dims) * it.get("weight", 1.0)
        out.append({**it, "final_scores": sc, "weighted": round(min(weighted, 10.0), 2),
                    "verdict": r.get("verdict", "keep"),
                    "final_reason": str(r.get("reason", ""))[:300],
                    "one_line": str(r.get("one_line", ""))[:200]})
    if n_missing:
        log(f"  ! 본선에서 끝내 점수를 못 받은 기사 {n_missing}건")
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


def _title_tokens(t: str) -> set:
    return set(re.findall(r"[a-z0-9가-힣]{2,}", (t or "").lower()))


def _same_event(a: str, b: str) -> bool:
    """같은 사건을 다룬 다른 매체 기사인가. 선정 직전에만 쓰는 느슨한 판정."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.6


def dedupe_events(ranked: list, log=None) -> tuple[list, list]:
    """점수 순으로 훑으며 같은 사건의 중복 기사를 걷어낸다. (남긴 것, 걷어낸 것)

    수집 단계 중복 제거는 임계값 0.8 이라 매체가 표현을 바꾸면 통과한다.
    실제로 같은 사건을 다룬 404 Media("12 Celebrity Deepfake Websites")와
    WIRED("a Dozen Celebrity Deepfake Websites")가 둘 다 선정돼 레터 1·2번에 나란히 실렸다.
    후보가 12건뿐인 이 지점에서는 임계값을 0.6 으로 낮춰도 안전하다.
    """
    kept, dropped = [], []
    for it in ranked:
        twin = next((k for k in kept if _same_event(k["title"], it["title"])), None)
        if twin:
            dropped.append({**it, "dup_of": twin["source_name"]})
            if log:
                log(f"  중복 사건 제외: {it['source_name']} | {it['title'][:44]} "
                    f"(← {twin['source_name']} 와 동일 사건)")
        else:
            kept.append(it)
    return kept, dropped


def select(scored: list, audience: dict, log=None) -> tuple[list, list]:
    """본선 점수로 최종 선정. (선정, 대기 후보) 를 반환한다.

    대기 후보는 검수 실패 시 빈자리를 메우는 데 쓴다.
    """
    cfg = audience["scoring"]["finalscreen"]
    ranked = sorted([s for s in scored if s["verdict"] == "keep"], key=lambda x: -x["weighted"])
    ranked, dups = dedupe_events(ranked, log)
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
