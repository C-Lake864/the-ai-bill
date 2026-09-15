"""소스 후보 실측 스크립트.

각 RSS 후보를 실제로 호출해서 '채택/탈락'을 숫자로 결정하기 위한 지표를 뽑는다.
결과: store/source_audit.json  +  docs/source_audit.md (REPORT.md 소스 채택표의 원본 데이터)

측정 지표
  reachable      : HTTP 200 + 피드 파싱 성공 + 엔트리 1개 이상
  n_entries      : 피드가 주는 아이템 수
  dated_ratio    : 발행일시가 파싱되는 아이템 비율
  fresh_7d       : 최근 7일 내 아이템 수 (일간 발행이므로 소재 공급량 지표)
  median_age_h   : 아이템 발행 후 경과시간 중앙값
  topic_hit      : 제목+요약이 환경/기후 키워드에 걸리는 비율 (LLM 없이 규칙 기반)
  body_ok_rate   : 실제 기사 URL 3건을 받아 본문 추출에 성공한 비율
  body_chars     : 추출 본문 길이 중앙값 (검수 노드가 원문 대조를 하므로 필수 조건)
  max_overlap    : 다른 후보와 제목이 겹치는 최대 비율 (정보 다양성 지표)
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
UA = "Mozilla/5.0 (compatible; personal-newsletter-bot/1.0; local research project)"
NOW = datetime.now(timezone.utc)

TOPIC_WORDS = [
    "climate", "carbon", "emission", "renewable", "solar", "wind", "energy", "environment",
    "pollution", "biodiversity", "ecosystem", "greenhouse", "warming", "fossil", "battery",
    "hydrogen", "electric vehicle", "recycl", "waste", "water", "heat", "drought",
    "flood", "wildfire", "sustainab", "esg", "net zero", "netzero", "cop3", "cop2",
    "species", "forest", "ocean", "methane", "grid", "nuclear", "plastic", "pipeline",
    "기후", "탄소", "온실가스", "배출", "재생에너지", "태양광", "풍력", "에너지", "환경",
    "오염", "생물다양성", "생태", "온난화", "화석", "배터리", "수소", "전기차", "재활용",
    "폐기물", "미세먼지", "가뭄", "홍수", "산불", "지속가능", "넷제로", "탄소중립", "원전",
    "플라스틱", "녹색", "기상", "해양", "산림",
]


def strip_tags(s):
    return re.sub(r"<[^>]+>", " ", s or "").strip()


def topic_hit(title, summary):
    blob = (strip_tags(title) + " " + strip_tags(summary)).lower()
    return any(w in blob for w in TOPIC_WORDS)


def entry_dt(e):
    for key in ("published_parsed", "updated_parsed"):
        tp = e.get(key)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def extract_body(html):
    """본문 추출: trafilatura 우선, 실패 시 BeautifulSoup 폴백."""
    try:
        import trafilatura

        got = trafilatura.extract(html, include_comments=False, include_tables=False)
        if got and len(got) > 400:
            return got
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
        return "\n".join(p for p in paras if len(p) > 40)
    except Exception:
        return ""


PAYWALL_MARKERS = [
    "preview of subscription content", "subscribe to continue", "sign in to read",
    "subscribers only", "this article is for subscribers",
    "이 기사는 유료", "로그인 후 이용", "구독자 전용", "유료회원",
]


def is_paywalled(body):
    """페이월 판정: 마커 문구가 있으면서 본문이 짧을 때만.

    'create a free account' 같은 뉴스레터 CTA 문구는 본문이 멀쩡한 기사에도 흔히 붙어서
    마커만으로 판정하면 오탐이 난다(Climate Home 실측 사례). 길이 조건을 함께 건다.
    """
    low = (body or "").lower()
    hit = any(m in low for m in PAYWALL_MARKERS)
    return hit and len(body or "") < 3500


def probe_one(c):
    out = {
        "id": c["id"], "name": c["name"], "url": c["url"], "lang": c["lang"], "type": c["type"],
        "reachable": False, "http_status": None, "latency_ms": None, "n_entries": 0,
        "dated_ratio": 0.0, "fresh_7d": 0, "median_age_h": None, "topic_hit": 0.0,
        "body_ok_rate": 0.0, "body_chars": 0, "paywall_rate": 0.0, "titles": [], "error": None,
    }
    t0 = time.time()
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=25, follow_redirects=True) as cli:
            r = cli.get(c["url"])
            out["http_status"] = r.status_code
            out["latency_ms"] = int((time.time() - t0) * 1000)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            entries = feed.entries or []
            out["n_entries"] = len(entries)
            if not entries:
                out["error"] = "no entries / parse failed"
                return out
            out["reachable"] = True

            ages, dated, hits = [], 0, 0
            for e in entries:
                dt = entry_dt(e)
                if dt:
                    dated += 1
                    ages.append((NOW - dt).total_seconds() / 3600)
                summary = e.get("summary", "")
                if not summary and e.get("content"):
                    summary = e["content"][0].get("value", "")
                if topic_hit(e.get("title", ""), summary):
                    hits += 1
                out["titles"].append(strip_tags(e.get("title", ""))[:160])
            out["dated_ratio"] = round(dated / len(entries), 2)
            out["topic_hit"] = round(hits / len(entries), 2)
            if ages:
                out["median_age_h"] = round(statistics.median(ages), 1)
                out["fresh_7d"] = sum(1 for a in ages if a <= 24 * 7)

            oks, lens, walls = 0, [], 0
            for e in entries[:3]:
                link = e.get("link")
                if not link:
                    continue
                try:
                    rr = cli.get(link)
                    body = extract_body(rr.text)
                    lens.append(len(body))
                    if len(body) >= 600:
                        oks += 1
                    if is_paywalled(body):
                        walls += 1
                except Exception:
                    lens.append(0)
                time.sleep(0.4)
            n = max(1, len(lens))
            out["body_ok_rate"] = round(oks / n, 2)
            out["paywall_rate"] = round(walls / n, 2)
            out["body_chars"] = int(statistics.median(lens)) if lens else 0
    except Exception as ex:
        out["error"] = (type(ex).__name__ + ": " + str(ex))[:200]
    return out


def norm_title(t):
    return re.sub(r"[^a-z0-9가-힣]", "", t.lower())[:60]


def main():
    cfg = yaml.safe_load((ROOT / "tools" / "source_candidates.yaml").read_text(encoding="utf-8"))
    cands = cfg["candidates"]
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(probe_one, cands))

    sets = {r["id"]: {norm_title(t) for t in r["titles"] if t} for r in results}
    for r in results:
        mine = sets[r["id"]]
        worst, worst_id = 0.0, None
        for oid, other in sets.items():
            if oid == r["id"] or not mine or not other:
                continue
            ov = len(mine & other) / len(mine)
            if ov > worst:
                worst, worst_id = ov, oid
        r["max_overlap"] = round(worst, 2)
        r["overlap_with"] = worst_id

    (ROOT / "store").mkdir(exist_ok=True)
    payload = {"probed_at": NOW.isoformat(), "results": results}
    (ROOT / "store" / "source_audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rows = [
        "| id | 소스 | 구분 | 언어 | HTTP | 엔트리 | 최근7일 | 중앙연령(h) | 주제적합 | 본문성공 | 본문길이 | 페이월 | 최대중복 | 비고 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(results, key=lambda x: (not x["reachable"], -x["fresh_7d"])):
        rows.append(
            "| {id} | {name} | {type} | {lang} | {http_status} | {n_entries} | {fresh_7d} | "
            "{median_age_h} | {topic_hit} | {body_ok_rate} | {body_chars} | {paywall_rate} | {max_overlap} | {note} |".format(
                note=(r["error"] or ""), **{k: r[k] for k in (
                    "id", "name", "type", "lang", "http_status", "n_entries", "fresh_7d",
                    "median_age_h", "topic_hit", "body_ok_rate", "body_chars", "paywall_rate", "max_overlap")}
            )
        )
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "source_audit.md").write_text(
        "# 소스 실측 결과 (" + NOW.strftime("%Y-%m-%d %H:%M UTC") + ")\n\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    print("\n".join(rows))
    print("\n-> store/source_audit.json, docs/source_audit.md")


if __name__ == "__main__":
    main()
