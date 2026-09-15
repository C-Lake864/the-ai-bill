"""4단계: 자동 검수 (할루시네이션·오류 판별).

3중 점검. 앞의 두 개는 LLM 없이 돌아가므로 비용이 0이고, 결과가 결정적이다.

  A. 인용문 대조 (규칙)  : claims[].quote 가 원문에 실제로 존재하는가
  B. 숫자·연도 대조 (규칙): 요약에 등장한 2자리 이상 숫자가 원문에 있는가
  C. 근거성 판정 (LLM)   : 원문만 보고 각 claim 이 supported / partial / unsupported 인지

하나라도 걸리면 REVISE. audience.yaml 의 max_retries 만큼 지적사항을 붙여 재생성하고,
그래도 실패하면 그 기사는 버리고 대기 후보로 교체한다(graph.py 의 라우팅).
"""
from __future__ import annotations

import re
import unicodedata

from .llm import chat_json

SYS = """너는 팩트체커다. 오직 주어진 '원문'만을 근거로 판단한다. 외부 지식을 쓰지 않는다.
반드시 JSON 객체 하나만 출력한다. 형식:
{"claims":[{"idx":0,"verdict":"supported","evidence":"원문 근거 문장 또는 없음","note":""}],
 "overall":"pass","issues":["문제점"],"fix_instruction":"수정 지시 한두 문장"}
- verdict: supported | partial | unsupported
  supported  : 원문이 그 주장을 직접 뒷받침한다
  partial    : 방향은 맞지만 범위/수치/주체가 원문보다 넓거나 다르다
  unsupported: 원문에서 확인할 수 없다
- overall: pass 는 모든 claim 이 supported 이고 요약 본문에도 원문에 없는 사실이 없을 때만.
- 숫자 단위 환산, 반올림, 원문에 없는 기관/인물 추가는 전부 문제로 잡는다."""


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def _quote_in_body(quote: str, body_n: str) -> bool:
    q = _norm(quote)
    if len(q) < 12:
        return False
    if q in body_n:
        return True
    # 앞뒤가 잘렸을 수 있으니 가장 긴 연속 조각으로 한 번 더 본다
    words = q.split()
    if len(words) >= 8:
        mid = " ".join(words[: max(6, len(words) // 2)])
        return mid in body_n
    return False


NUM = re.compile(r"\d[\d,\.]*")


def _numbers(text: str) -> set:
    out = set()
    for m in NUM.finditer(text or ""):
        raw = m.group(0).rstrip(".,")
        digits = raw.replace(",", "")
        if len(digits.replace(".", "")) >= 2:
            out.add(digits)
    return out


def rule_checks(summary: dict, body: str) -> dict:
    body_n = _norm(body)
    body_nums = _numbers(body_n.replace(",", ""))

    quote_fail = []
    for i, c in enumerate(summary.get("claims", [])):
        if not _quote_in_body(c.get("quote", ""), body_n):
            quote_fail.append({"idx": i, "claim": c.get("claim", "")[:120],
                               "quote": (c.get("quote") or "")[:160]})

    prose = " ".join(str(summary.get(k, "")) for k in
                     ("headline", "what_happened", "why_it_matters", "reader_takeaway"))
    num_fail = sorted(n for n in _numbers(prose) if n not in body_nums)

    return {
        "quote_unverified": quote_fail,
        "numbers_not_in_source": num_fail,
        "passed": not quote_fail and not num_fail,
    }


def llm_check(summary: dict, item: dict, audience: dict) -> dict:
    claims = summary.get("claims", [])
    claim_txt = "\n".join(f"[{i}] {c.get('claim','')}" for i, c in enumerate(claims))
    user = (
        f"[원문]\n{item['body'][:9000]}\n\n"
        f"[요약문]\n제목: {summary.get('headline','')}\n"
        f"무슨 일: {summary.get('what_happened','')}\n"
        f"왜 중요: {summary.get('why_it_matters','')}\n"
        f"독자 행동: {summary.get('reader_takeaway','')}\n\n"
        f"[검증할 주장]\n{claim_txt}"
    )
    return chat_json("verify", audience["verify"]["model_tier"], SYS, user,
                     temperature=0.0, max_tokens=1800)


def verify(summary: dict, item: dict, audience: dict) -> dict:
    """규칙 + LLM 검수를 합쳐 최종 판정을 낸다."""
    rules = audience["verify"]["rules"]
    rc = rule_checks(summary, item["body"])

    lc, llm_error = {}, None
    try:
        lc = llm_check(summary, item, audience)
    except Exception as ex:
        llm_error = (type(ex).__name__ + ": " + str(ex))[:160]

    verdicts = [c.get("verdict", "unsupported") for c in lc.get("claims", [])]
    n_claims = max(1, len(summary.get("claims", [])))
    n_supported = sum(1 for v in verdicts if v == "supported")
    n_unsupported = sum(1 for v in verdicts if v == "unsupported")
    n_partial = sum(1 for v in verdicts if v == "partial")
    support_rate = round(n_supported / n_claims, 2) if verdicts else 0.0

    issues = []
    if rc["quote_unverified"]:
        issues.append(f"원문에 없는 인용문 {len(rc['quote_unverified'])}건: "
                      + "; ".join(q["quote"][:60] for q in rc["quote_unverified"]))
    if rules.get("number_must_appear", True) and rc["numbers_not_in_source"]:
        issues.append("원문에서 확인되지 않는 숫자: " + ", ".join(rc["numbers_not_in_source"][:6]))
    issues += [str(i)[:200] for i in lc.get("issues", [])]

    if llm_error:
        # LLM 검수가 죽으면 규칙 검수만으로 판단한다. 대신 통과 기준을 더 보수적으로 본다
        # (인용문 1건이라도 미확인이면 탈락).
        passed = rc["passed"]
        reason = f"llm_check_failed({llm_error}) -> 규칙 검수만 적용"
    else:
        passed = (
            rc["passed"]
            and lc.get("overall") == "pass"
            and n_unsupported <= rules.get("max_unsupported_claims", 0)
            and support_rate >= rules.get("min_claim_support_rate", 1.0)
        )
        reason = "" if passed else "검수 기준 미달"

    return {
        "passed": bool(passed),
        "support_rate": support_rate,
        "n_claims": len(summary.get("claims", [])),
        "n_supported": n_supported,
        "n_partial": n_partial,
        "n_unsupported": n_unsupported,
        "quote_unverified": rc["quote_unverified"],
        "numbers_not_in_source": rc["numbers_not_in_source"],
        "llm_overall": lc.get("overall"),
        "llm_error": llm_error,
        "issues": issues,
        "fix_instruction": lc.get("fix_instruction", ""),
        "note": reason,
        "claim_verdicts": lc.get("claims", []),
    }


def feedback_text(v: dict) -> str:
    parts = []
    if v["quote_unverified"]:
        parts.append("다음 인용문은 원문에 존재하지 않는다. 원문 문장을 그대로 복사해서 쓸 것:\n"
                     + "\n".join("- " + q["quote"][:120] for q in v["quote_unverified"]))
    if v["numbers_not_in_source"]:
        parts.append("다음 숫자는 원문에서 확인되지 않는다. 원문에 있는 숫자만 쓰거나 숫자를 빼고 서술할 것: "
                     + ", ".join(v["numbers_not_in_source"][:8]))
    for c in v.get("claim_verdicts", []):
        if c.get("verdict") in ("partial", "unsupported"):
            parts.append(f"claim[{c.get('idx')}] {c.get('verdict')}: {c.get('note','')}")
    if v.get("fix_instruction"):
        parts.append("수정 지시: " + v["fix_instruction"])
    return "\n".join(parts)[:2000]
