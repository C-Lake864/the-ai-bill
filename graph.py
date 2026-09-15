"""LangGraph 워크플로우 정의.

    collect ─→ prescreen ─→ extract ─→ finalscreen ─→ summarize ⇄ verify
                                                                    ↑         │
                                                                 advance ←────┘
                                                                    │
                                                                 render ─→ publish ─→ record ─→ END

핵심은 summarize ⇄ verify ⇄ advance 사이클이다.
verify 결과에 따라
  pass      → advance (기사 채택)
  fail+여유 → summarize (지적사항을 붙여 재생성)
  fail+소진 → advance (기사 폐기 + 대기 후보로 교체)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from nl import collect as C
from nl import extract as X
from nl import render as R
from nl import publish as P
from nl import screen as S
from nl import summarize as SUM
from nl import verify as V
from nl.config import OUT, STORE, load_audience, load_sources, now_local, run_dir
from nl.llm import USAGE


class State(TypedDict, total=False):
    run_id: str
    started_at: str
    audience: dict
    sources: dict
    dry_run: bool

    source_status: list
    collected: list          # 규칙 필터 통과 아이템
    dropped_rules: list      # 규칙/중복으로 버린 아이템 (사유 포함)
    body_failed: list        # 본문 확보 실패
    prescreened: list
    finalists: list
    scored: list
    selected: list
    backups: list

    queue: list              # 요약 대기열
    cursor: int
    attempts: int
    draft: dict
    last_verify: dict
    articles: list           # 검수 통과 기사
    rejected: list           # 검수 최종 탈락 기사
    n_regenerated: int

    html: str
    markdown: str
    publish_result: dict
    files: dict
    metrics: dict
    log: list


def _log(state: State, msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    state.setdefault("log", []).append(line)
    print(line, flush=True)


def _dump(state: State, name: str, payload) -> None:
    (run_dir(state["run_id"]) / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------- 1. 수집
def node_collect(state: State) -> dict:
    _log(state, "1/8 수집 시작")
    res = C.collect(state["sources"], state["audience"]["exclude"])
    ok = [s for s in res["source_status"] if s["ok"]]
    _log(state, f"  소스 {len(ok)}/{len(res['source_status'])} 응답, 원본 {res['n_raw']}건 → "
                f"규칙 필터 통과 {len(res['kept'])}건 (탈락 {len(res['dropped'])}건)")
    for s in res["source_status"]:
        if not s["ok"]:
            _log(state, f"  ! 소스 실패: {s['source']} - {s['error']}")
    _dump(state, "01_collected.json", res)
    return {"source_status": res["source_status"], "collected": res["kept"],
            "dropped_rules": res["dropped"]}


# ---------------------------------------------------------------- 2. 예선
def node_prescreen(state: State) -> dict:
    items = state["collected"]
    cfg = state["audience"]["scoring"]["prescreen"]
    _log(state, f"2/8 예선: {len(items)}건을 {cfg['batch_size']}건씩 채점")
    t0 = time.time()
    scored = S.prescreen(items, state["audience"], lambda m: _log(state, m))
    passed = [s for s in scored if s["prescreen_score"] >= cfg["min_score"]]
    passed.sort(key=lambda x: -x["prescreen_score"])
    top, _rest = S.cap_pick(passed, cfg.get("max_per_source", 3), cfg["keep_top"])
    srcs = {}
    for t in top:
        srcs[t["source_id"]] = srcs.get(t["source_id"], 0) + 1
    _log(state, f"  {len(passed)}건이 {cfg['min_score']}점 이상 → 소스당 최대 "
                f"{cfg.get('max_per_source', 3)}건으로 상위 {len(top)}건 본선행 "
                f"({', '.join(f'{k}:{v}' for k, v in srcs.items())}) ({time.time()-t0:.1f}s)")
    _dump(state, "02_prescreen.json", sorted(scored, key=lambda x: -x["prescreen_score"]))
    return {"prescreened": scored, "finalists": top}


# ---------------------------------------------------------------- 3. 본문 확보
def node_extract(state: State) -> dict:
    items = state["finalists"]
    _log(state, f"3/8 본문 수집: {len(items)}건")
    ok, bad = X.fetch_bodies(items, state["sources"]["fetch"])
    for b in bad:
        _log(state, f"  ! 본문 실패({b['body_error']}): {b['title'][:50]}")
    _log(state, f"  본문 확보 {len(ok)}건 / 실패 {len(bad)}건")
    _dump(state, "03_bodies.json", {"ok": [{k: v for k, v in i.items() if k != 'body'} | {"body_chars": len(i["body"])} for i in ok],
                                    "failed": [{k: v for k, v in i.items() if k != 'body'} for i in bad]})
    return {"finalists": ok, "body_failed": bad}


# ---------------------------------------------------------------- 4. 본선 + 선정
def node_finalscreen(state: State) -> dict:
    items = state["finalists"]
    cfg = state["audience"]["scoring"]["finalscreen"]
    _log(state, f"4/8 본선: {len(items)}건을 {cfg['batch_size']}건씩 본문 채점")
    scored = S.finalscreen(items, state["audience"], lambda m: _log(state, m))
    chosen, backup = S.select(scored, state["audience"])
    for c in chosen:
        _log(state, f"  선정 {c['weighted']:>5} | {c['source_name'][:12]:12s} | {c['title'][:52]}")
    _log(state, f"  선정 {len(chosen)}건 / 대기 후보 {len(backup)}건")
    _dump(state, "04_finalscreen.json", sorted(
        [{k: v for k, v in s.items() if k != "body"} for s in scored], key=lambda x: -x["weighted"]))
    return {"scored": scored, "selected": chosen, "backups": backup,
            "queue": list(chosen), "cursor": 0, "attempts": 0,
            "articles": [], "rejected": [], "n_regenerated": 0}


# ---------------------------------------------------------------- 5. 요약
def node_summarize(state: State) -> dict:
    it = state["queue"][state["cursor"]]
    tag = "재생성" if state["attempts"] > 0 else "요약"
    _log(state, f"5/8 {tag} [{state['cursor']+1}/{len(state['queue'])}] {it['title'][:50]}")
    fb = V.feedback_text(state["last_verify"]) if state["attempts"] > 0 and state.get("last_verify") else ""
    try:
        draft = SUM.summarize_one(it, state["audience"], feedback=fb)
    except Exception as ex:
        _log(state, f"  ! 요약 실패: {type(ex).__name__}: {ex}")
        draft = {"_error": f"{type(ex).__name__}: {ex}"}
    return {"draft": draft,
            "n_regenerated": state["n_regenerated"] + (1 if state["attempts"] > 0 else 0)}


# ---------------------------------------------------------------- 6. 검수
def node_verify(state: State) -> dict:
    it = state["queue"][state["cursor"]]
    draft = state["draft"]
    if draft.get("_error"):
        v = {"passed": False, "issues": [draft["_error"]], "n_claims": 0, "n_supported": 0,
             "n_partial": 0, "n_unsupported": 0, "support_rate": 0.0, "quote_unverified": [],
             "numbers_not_in_source": [], "note": "요약 생성 실패"}
    else:
        v = V.verify(draft, it, state["audience"])
    mark = "통과" if v["passed"] else "실패"
    _log(state, f"6/8 검수 {mark} | 근거 {v['n_supported']}/{v['n_claims']} "
                f"(partial {v['n_partial']}, unsupported {v['n_unsupported']}) "
                f"| 미확인 인용 {len(v['quote_unverified'])} | 미확인 숫자 {len(v['numbers_not_in_source'])}")
    for i in v["issues"][:3]:
        _log(state, f"    - {i[:140]}")
    return {"last_verify": v}


def route_after_verify(state: State) -> str:
    if state["last_verify"]["passed"]:
        return "accept"
    if state["attempts"] < state["audience"]["verify"]["max_retries"]:
        return "retry"
    return "give_up"


def node_retry(state: State) -> dict:
    _log(state, f"  → 재생성 시도 {state['attempts']+1}/{state['audience']['verify']['max_retries']}")
    return {"attempts": state["attempts"] + 1}


# ---------------------------------------------------------------- 7. 채택/폐기 + 대기 후보 교체
def node_advance(state: State) -> dict:
    it = state["queue"][state["cursor"]]
    v = state["last_verify"]
    cfg = state["audience"]["scoring"]["finalscreen"]
    articles, rejected, queue, backups = (list(state["articles"]), list(state["rejected"]),
                                          list(state["queue"]), list(state["backups"]))

    if v["passed"]:
        articles.append({"item": {k: val for k, val in it.items() if k != "body"},
                         "summary": state["draft"], "verify": v})
    else:
        rejected.append({"item": {k: val for k, val in it.items() if k != "body"},
                         "summary": state["draft"], "verify": v,
                         "reason": "검수 재시도 소진"})
        _log(state, f"  → 폐기: {it['title'][:50]}")
        # 최소 건수를 못 채울 상황이면 대기 후보를 하나 끌어온다
        remaining = len(queue) - (state["cursor"] + 1)
        if len(articles) + remaining < cfg["select_min"] and backups:
            nxt = backups.pop(0)
            if not nxt.get("body"):
                ok, _bad = X.fetch_bodies([nxt], state["sources"]["fetch"])
                nxt = ok[0] if ok else None
            if nxt:
                queue.append(nxt)
                _log(state, f"  → 대기 후보 투입: {nxt['title'][:50]}")

    return {"articles": articles, "rejected": rejected, "queue": queue, "backups": backups,
            "cursor": state["cursor"] + 1, "attempts": 0, "last_verify": {}, "draft": {}}


def route_after_advance(state: State) -> str:
    return "next" if state["cursor"] < len(state["queue"]) else "render"


# ---------------------------------------------------------------- 8. 렌더 / 발행 / 기록
def node_render(state: State) -> dict:
    cfg = state["audience"]
    date = now_local(cfg)
    meta = {
        "run_id": state["run_id"],
        "n_collected": len(state["collected"]) + len(state["dropped_rules"]),
        "n_after_rules": len(state["collected"]),
        "n_prescreen_pass": len(state["finalists"]) + len(state.get("body_failed", [])),
        "n_finalists": len(state["selected"]),
        "n_verify_failed": len(state["rejected"]),
        "n_regenerated": state["n_regenerated"],
    }
    _log(state, f"7/8 렌더링: 기사 {len(state['articles'])}건")
    html = R.render_html(cfg, date, state["articles"], meta)
    md = R.render_markdown(cfg, date, state["articles"], meta)
    return {"html": html, "markdown": md, "metrics": meta}


def node_publish(state: State) -> dict:
    cfg = state["audience"]["publish"]
    date = now_local(state["audience"])
    dstr = date.strftime("%Y-%m-%d")
    files = P.save_local(OUT, dstr, state["html"], state["markdown"]) if cfg.get("save_local", True) else {}

    if not state["articles"]:
        _log(state, "8/8 발행 보류: 검수를 통과한 기사가 0건이라 메일을 보내지 않습니다")
        return {"files": files, "publish_result": {"sent": False, "status": "no_articles"}}

    top = state["articles"][0]["summary"].get("headline", "")
    subject = cfg["subject_format"].format(date=dstr, top_headline=top)

    if state.get("dry_run") or cfg.get("channel") == "dry_run":
        _log(state, f"8/8 드라이런: 메일 미발송, 로컬 사본만 저장 → {files.get('html')}")
        return {"files": files, "publish_result": {"sent": False, "status": "dry_run", "subject": subject}}

    res = P.send_email(subject, state["html"], state["markdown"])
    res["subject"] = subject
    if res["sent"]:
        _log(state, f"8/8 발행 완료: {', '.join(res['to'])} 에게 전송 · 제목 '{subject}'")
    else:
        _log(state, f"8/8 발행 실패({res['status']}): {res.get('error') or res.get('missing')}")
        _log(state, f"    로컬 사본은 남아 있습니다 → {files.get('html')}")
    return {"files": files, "publish_result": res}


def node_record(state: State) -> dict:
    m = dict(state["metrics"])
    pr = state["publish_result"]
    m.update({
        "run_id": state["run_id"],
        "started_at": state["started_at"],
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "n_sources_ok": sum(1 for s in state["source_status"] if s["ok"]),
        "n_sources_total": len(state["source_status"]),
        "n_body_failed": len(state.get("body_failed", [])),
        "n_published": len(state["articles"]),
        "verify_pass_rate": round(len(state["articles"]) /
                                  max(1, len(state["articles"]) + len(state["rejected"])), 2),
        "published_titles": [a["summary"].get("headline") for a in state["articles"]],
        "publish_status": pr.get("status"),
        "publish_sent": pr.get("sent"),
        "llm_usage": USAGE.as_dict(),
        "files": state.get("files", {}),
    })
    STORE.mkdir(exist_ok=True)
    with (STORE / "metrics.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")
    _dump(state, "05_articles.json", {"published": state["articles"], "rejected": state["rejected"]})
    _dump(state, "06_metrics.json", m)
    (run_dir(state["run_id"]) / "run.log").write_text("\n".join(state["log"]), encoding="utf-8")
    _log(state, f"기록 완료 → store/metrics.jsonl, store/runs/{state['run_id']}/")
    return {"metrics": m}


# ---------------------------------------------------------------- 그래프 조립
def build_graph():
    g = StateGraph(State)
    g.add_node("collect", node_collect)
    g.add_node("prescreen", node_prescreen)
    g.add_node("extract", node_extract)
    g.add_node("finalscreen", node_finalscreen)
    g.add_node("summarize", node_summarize)
    g.add_node("verify", node_verify)
    g.add_node("retry", node_retry)
    g.add_node("advance", node_advance)
    g.add_node("render", node_render)
    g.add_node("publish", node_publish)
    g.add_node("record", node_record)

    g.add_edge(START, "collect")
    g.add_edge("collect", "prescreen")
    g.add_edge("prescreen", "extract")
    g.add_edge("extract", "finalscreen")

    # 선정 결과가 비면 요약 단계를 통째로 건너뛴다
    g.add_conditional_edges("finalscreen",
                            lambda s: "summarize" if s["queue"] else "render",
                            {"summarize": "summarize", "render": "render"})
    g.add_edge("summarize", "verify")
    g.add_conditional_edges("verify", route_after_verify,
                            {"accept": "advance", "give_up": "advance", "retry": "retry"})
    g.add_edge("retry", "summarize")
    g.add_conditional_edges("advance", route_after_advance,
                            {"next": "summarize", "render": "render"})
    g.add_edge("render", "publish")
    g.add_edge("publish", "record")
    g.add_edge("record", END)
    return g.compile()


def initial_state(dry_run: bool = False) -> State:
    return {
        "run_id": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "audience": load_audience(),
        "sources": load_sources(),
        "dry_run": dry_run,
        "log": [], "articles": [], "rejected": [], "queue": [], "cursor": 0,
        "attempts": 0, "n_regenerated": 0, "backups": [], "last_verify": {}, "draft": {},
    }
