"""3단계: 요약 + 인사이트 추출.

단순 요약과 구분되는 지점은 두 가지다.
  (1) why_it_matters : 타깃 독자(국내 ESG 실무자) 관점에서의 의미를 원문 사실에 근거해 해석
  (2) reader_takeaway: 독자가 당장 확인/행동할 것 한 줄
검수를 받기 위해 claims(사실 주장 + 원문 인용문) 를 함께 만들게 한다. 인용문이 원문에
그대로 없으면 검수 노드가 바로 잡아낸다.
"""
from __future__ import annotations

from .llm import chat_json

SYS = """너는 한국어 뉴스레터 작가다. 주어진 기사 본문만 근거로 요약을 만든다.
반드시 JSON 객체 하나만 출력한다. 형식:
{"headline":"...","what_happened":"...","why_it_matters":"...","reader_takeaway":"...",
 "claims":[{"claim":"요약에 쓴 사실 한 문장","quote":"본문에서 그대로 복사한 근거 문장"}]}

규칙:
- quote 는 본문에 있는 문장을 글자 그대로 복사한다. 번역하거나 다듬지 않는다.
- 본문에 없는 숫자·날짜·기관명·인용을 만들지 않는다. 단위 환산과 반올림도 하지 않는다.
- why_it_matters 는 해석이지만, 해석의 재료는 본문 안의 사실이어야 한다.
- 확실하지 않으면 그 내용을 빼고 쓴다.
- claims 는 3~5개."""


def summarize_one(item: dict, audience: dict, feedback: str = "") -> dict:
    cfg = audience["summarize"]
    user = (
        f"[타깃 독자]\n{audience['audience']['persona']}\n"
        f"이미 아는 것: {audience['audience']['knows_already']}\n"
        f"원하는 것: {audience['audience']['needs']}\n\n"
        f"[문체 규칙]\n{cfg['style']}\n\n"
        f"[필드 설명]\n" + "\n".join(f"- {k}: {v}" for k, v in cfg["fields"].items()) + "\n\n"
        + (f"[직전 시도의 검수 지적사항 - 반드시 고칠 것]\n{feedback}\n\n" if feedback else "")
        + f"[기사]\n제목: {item['title']}\n소스: {item['source_name']}\nURL: {item['url']}\n\n본문:\n{item['body'][:9000]}"
    )
    out = chat_json("summarize", cfg["model_tier"], SYS, user, temperature=0.3, max_tokens=1800)
    out["claims"] = [c for c in out.get("claims", []) if isinstance(c, dict) and c.get("claim")][:5]
    return out
