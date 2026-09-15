"""검수 노드가 실제로 할루시네이션을 잡는지 확인하는 자체 테스트.

'검수를 붙였다'는 말만으로는 동작 증거가 되지 않는다. 그래서 정상 요약 1건과,
거기에 고의로 오류를 심은 변형 3건을 같은 검수기에 통과시켜 결과를 비교한다.

  M0 원본           : 정상 요약           -> PASS 기대
  M1 숫자 조작      : 본문에 없는 수치로 교체 -> FAIL 기대 (규칙 검수: 숫자 대조)
  M2 인용문 위조    : claim 의 quote 를 창작  -> FAIL 기대 (규칙 검수: 인용문 대조)
  M3 사실 날조      : 본문에 없는 주장 추가   -> FAIL 기대 (LLM 검수: 근거성 판정)

결과: store/verify_selftest.json
"""
from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nl.config import load_audience, load_env, load_sources, STORE  # noqa: E402
from nl import extract as X  # noqa: E402
from nl import summarize as SUM  # noqa: E402
from nl import verify as V  # noqa: E402

FAKE_QUOTE = "환경부 관계자는 이번 조치가 내년 상반기부터 전면 시행된다고 밝혔다."
FAKE_CLAIM = {
    "claim": "정부는 2035년까지 관련 예산을 3배로 늘리기로 확정했다.",
    "quote": "정부는 2035년까지 관련 예산을 3배로 늘리기로 확정했다고 발표했다.",
}


def pick_article(sources: dict, audience: dict) -> dict:
    """가장 최근 실행에서 발행된 기사 1건을 골라 본문을 다시 받아온다."""
    runs = sorted((STORE / "runs").glob("*/05_articles.json"))
    if not runs:
        raise SystemExit("store/runs 에 실행 기록이 없습니다. 먼저 run.py 를 실행하세요.")
    published = json.loads(runs[-1].read_text(encoding="utf-8"))["published"]
    if not published:
        raise SystemExit("마지막 실행에서 발행된 기사가 없습니다.")
    item = published[0]["item"]
    ok, bad = X.fetch_bodies([item], sources["fetch"])
    if not ok:
        raise SystemExit(f"본문 재수집 실패: {bad[0]['body_error']}")
    return ok[0]


def mutate_number(s: dict) -> dict:
    m = copy.deepcopy(s)
    for field in ("what_happened", "why_it_matters"):
        txt = m.get(field, "")
        found = re.search(r"\d[\d,]{1,}", txt)
        if found:
            raw = found.group(0)
            fake = str(int(raw.replace(",", "")) * 7 + 13)
            m[field] = txt.replace(raw, fake, 1)
            m["_mutation"] = f"{field}: {raw} -> {fake}"
            return m
    m["what_headline"] = m.get("what_happened", "") + " 총 4827억원 규모다."
    m["what_happened"] = m.get("what_happened", "") + " 총 4827억원 규모다."
    m["_mutation"] = "what_happened 에 가짜 수치 4827 추가"
    return m


def mutate_quote(s: dict) -> dict:
    m = copy.deepcopy(s)
    if m.get("claims"):
        m["claims"][0]["quote"] = FAKE_QUOTE
        m["_mutation"] = "claims[0].quote 를 창작 문장으로 교체"
    return m


def mutate_claim(s: dict) -> dict:
    m = copy.deepcopy(s)
    m["claims"] = list(m.get("claims", [])) + [dict(FAKE_CLAIM)]
    m["what_happened"] = m.get("what_happened", "") + " " + FAKE_CLAIM["claim"]
    m["_mutation"] = "본문에 없는 주장을 요약과 claims 에 추가"
    return m


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    load_env()
    audience, sources = load_audience(), load_sources()
    item = pick_article(sources, audience)
    print(f"대상 기사: {item['title'][:60]}\n본문 {len(item['body'])}자\n")

    base = SUM.summarize_one(item, audience)
    cases = [
        ("M0 원본", base, True),
        ("M1 숫자 조작", mutate_number(base), False),
        ("M2 인용문 위조", mutate_quote(base), False),
        ("M3 사실 날조", mutate_claim(base), False),
    ]

    rows, all_ok = [], True
    for name, summary, expect_pass in cases:
        v = V.verify(summary, item, audience)
        ok = (v["passed"] == expect_pass)
        all_ok &= ok
        rows.append({
            "case": name,
            "mutation": summary.get("_mutation", "-"),
            "expected": "PASS" if expect_pass else "FAIL",
            "actual": "PASS" if v["passed"] else "FAIL",
            "as_expected": ok,
            "caught_by": ", ".join(filter(None, [
                "규칙:인용문대조" if v["quote_unverified"] else "",
                "규칙:숫자대조" if v["numbers_not_in_source"] else "",
                "LLM:근거성판정" if v.get("llm_overall") and v["llm_overall"] != "pass" else "",
            ])),
            "support": f"{v['n_supported']}/{v['n_claims']}",
            "numbers_not_in_source": v["numbers_not_in_source"],
            "n_quote_unverified": len(v["quote_unverified"]),
            "issues": v["issues"][:3],
        })
        print(f"{name:14s} 기대 {rows[-1]['expected']:4s} / 실제 {rows[-1]['actual']:4s} "
              f"{'OK' if ok else '<<< 불일치'}  근거 {rows[-1]['support']}  "
              f"검출: {rows[-1]['caught_by'] or '-'}")
        for i in v["issues"][:2]:
            print(f"    - {i[:110]}")

    out = {"article": {"title": item["title"], "url": item["url"], "body_chars": len(item["body"])},
           "cases": rows, "all_as_expected": bool(all_ok)}
    (STORE / "verify_selftest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n전체 결과: {'모두 기대대로' if all_ok else '기대와 다른 케이스 있음'} "
          f"-> store/verify_selftest.json")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
