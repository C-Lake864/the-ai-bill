"""본문 수집. 요약·검수 모두 '원문 텍스트'에 의존하므로 여기서 실패하면 그 기사는 버린다."""
from __future__ import annotations

import concurrent.futures as cf
import re
import time

import httpx

MIN_BODY_CHARS = 600
PAYWALL_MARKERS = [
    "preview of subscription content", "subscribe to continue", "sign in to read",
    "subscribers only", "this article is for subscribers",
    "이 기사는 유료", "로그인 후 이용", "구독자 전용", "유료회원",
]


def _clean(t: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", (t or "").strip())


def extract_body(html: str) -> str:
    try:
        import trafilatura

        got = trafilatura.extract(html, include_comments=False, include_tables=False)
        if got and len(got) > 400:
            return _clean(got)
    except Exception:
        pass
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for bad in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            bad.decompose()
        node = soup.find("article") or soup.find("main") or soup.body
        if not node:
            return ""
        paras = [p.get_text(" ", strip=True) for p in node.find_all("p")]
        return _clean("\n".join(p for p in paras if len(p) > 40))
    except Exception:
        return ""


def is_paywalled(body: str) -> bool:
    low = (body or "").lower()
    return any(m in low for m in PAYWALL_MARKERS) and len(body or "") < 3500


def fetch_bodies(items: list, fetch_cfg: dict) -> tuple[list, list]:
    """(본문 확보 성공 목록, 실패 목록). 실패 사유를 아이템에 남긴다."""
    ua = fetch_cfg["user_agent"]
    delay = fetch_cfg.get("polite_delay_sec", 0.3)

    def one(it):
        try:
            time.sleep(delay)
            with httpx.Client(headers={"User-Agent": ua}, timeout=fetch_cfg["timeout_sec"],
                              follow_redirects=True) as cli:
                r = cli.get(it["url"])
                r.raise_for_status()
                body = extract_body(r.text)
        except Exception as ex:
            return {**it, "body": "", "body_error": (type(ex).__name__ + ": " + str(ex))[:160]}
        if len(body) < MIN_BODY_CHARS:
            return {**it, "body": body, "body_error": f"too_short({len(body)}<{MIN_BODY_CHARS})"}
        if is_paywalled(body):
            return {**it, "body": body, "body_error": "paywalled"}
        return {**it, "body": body, "body_chars": len(body), "body_error": None}

    with cf.ThreadPoolExecutor(max_workers=fetch_cfg.get("concurrency", 8)) as ex:
        out = list(ex.map(one, items))
    ok = [i for i in out if not i["body_error"]]
    bad = [i for i in out if i["body_error"]]
    return ok, bad
