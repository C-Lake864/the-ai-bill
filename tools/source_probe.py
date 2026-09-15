"""소스 후보 실측 스크립트.

각 RSS 후보를 실제로 호출해서 '채택/탈락'을 숫자로 결정하기 위한 지표를 뽑는다.

  python tools/source_probe.py                                  # 기본(환경·기후 주제)
  python tools/source_probe.py --candidates tools/source_candidates_ai.yaml --out ai

결과: store/source_audit[_out].json  +  docs/source_audit[_out].md

측정 지표
  reachable      : HTTP 200 + 피드 파싱 성공 + 엔트리 1개 이상          (C1)
  on_topic_7d    : 최근 7일 중 '주제에 맞는' 기사 수                    (C2')
  on_topic_48h   : 최근 48시간 중 주제에 맞는 기사 수                   (풀 규모 집계용)
  topic_hit      : 전체 엔트리 중 주제 적중 비율                        (C3)
  body_ok_rate   : 주제 적중 기사 3건을 받아 본문 추출에 성공한 비율     (C4)
  body_chars     : 추출 본문 길이 중앙값                                (C4)
  max_overlap    : 다른 후보와 제목이 겹치는 최대 비율                  (C5)
  paywall_rate   : 페이월 검출률                                        (C6)

주제 판정은 후보 yaml 의 topic 블록에서 읽는다 (없으면 기본 환경·기후 키워드).
  topic.require_all : 이 그룹들에 '전부' 걸려야 함
  topic.require_any : 이 그룹들 중 '하나 이상' 걸려야 함
예) AI × (환경 또는 사회문제)  ->  require_all: [ai], require_any: [env, social]
"""
from __future__ import annotations

import argparse
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

# topic 블록이 없는 후보 파일을 위한 기본값 (환경·기후 주제)
DEFAULT_TOPIC = {
    "require_all": ["climate"],
    "require_any": [],
    "min_on_topic_7d": 5,
    "min_topic_hit": 0.60,
    "groups": {
        "climate": [
            "climate", "carbon", "emission", "renewable", "solar", "wind", "energy", "environment",
            "pollution", "biodiversity", "ecosystem", "greenhouse", "warming", "fossil", "battery",
            "hydrogen", "electric vehicle", "recycling", "waste", "water", "heat", "drought",
            "flood", "wildfire", "sustainability", "esg", "net zero", "species", "forest",
            "ocean", "methane", "grid", "nuclear", "plastic", "pipeline",
            "기후", "탄소", "온실가스", "배출", "재생에너지", "태양광", "풍력", "에너지", "환경",
            "오염", "생물다양성", "생태", "온난화", "화석", "배터리", "수소", "전기차", "재활용",
            "폐기물", "미세먼지", "가뭄", "홍수", "산불", "지속가능", "넷제로", "탄소중립", "원전",
            "플라스틱", "녹색", "기상", "해양", "산림",
        ]
    },
}


def compile_group(words):
    """키워드 목록을 정규식 하나로 컴파일한다.

    영문 토큰에는 단어 경계를 붙인다. 그냥 부분 문자열로 찾으면 'said'/'Thai'/'email' 안의
    'ai' 까지 잡혀서 1차 측정이 크게 부풀었다. 한글은 \\b 가 의미가 없어 부분 문자열 그대로 둔다.
    """
    alts = []
    for w in words:
        w = w.strip().lower()
        if not w:
            continue
        esc = re.escape(w)
        if w.isascii():
            if w[0].isalnum():
                esc = r"(?<![a-z0-9])" + esc
            if w[-1].isalnum():
                esc = esc + r"(?![a-z0-9])"
        alts.append(esc)
    return re.compile("|".join(alts), re.IGNORECASE) if alts else None


class TopicMatcher:
    def __init__(self, topic: dict):
        self.name = topic.get("name", "")
        self.require_all = topic.get("require_all", [])
        self.require_any = topic.get("require_any", [])
        self.pats = {g: compile_group(w) for g, w in topic.get("groups", {}).items()}
        self.min_on_topic_7d = topic.get("min_on_topic_7d", 5)
        self.min_topic_hit = topic.get("min_topic_hit", 0.60)

    def _hit(self, group: str, blob: str) -> bool:
        p = self.pats.get(group)
        return bool(p and p.search(blob))

    def matches(self, title: str, summary: str) -> bool:
        blob = (strip_tags(title) + " " + strip_tags(summary)).lower()
        if not all(self._hit(g, blob) for g in self.require_all):
            return False
        if self.require_any and not any(self._hit(g, blob) for g in self.require_any):
            return False
        return True


def strip_tags(s):
    return re.sub(r"<[^>]+>", " ", s or "").strip()


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


def probe_one(c, matcher: TopicMatcher):
    out = {
        "id": c["id"], "name": c["name"], "url": c["url"], "lang": c["lang"], "type": c["type"],
        "reachable": False, "http_status": None, "latency_ms": None, "n_entries": 0,
        "dated_ratio": 0.0, "fresh_7d": 0, "fresh_48h": 0, "median_age_h": None,
        "on_topic_7d": 0, "on_topic_48h": 0, "topic_hit": 0.0,
        "body_ok_rate": 0.0, "body_chars": 0, "paywall_rate": 0.0,
        "titles": [], "sample_on_topic": [], "error": None,
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
            topic_links = []
            for e in entries:
                title = strip_tags(e.get("title", ""))
                summary = e.get("summary", "")
                if not summary and e.get("content"):
                    summary = e["content"][0].get("value", "")
                on_topic = matcher.matches(title, summary)
                hits += int(on_topic)
                out["titles"].append(title[:160])

                dt = entry_dt(e)
                if dt:
                    dated += 1
                    age = (NOW - dt).total_seconds() / 3600
                    ages.append(age)
                    if age <= 24 * 7:
                        out["fresh_7d"] += 1
                        out["on_topic_7d"] += int(on_topic)
                    if age <= 48:
                        out["fresh_48h"] += 1
                        out["on_topic_48h"] += int(on_topic)

                if on_topic:
                    if len(out["sample_on_topic"]) < 5:
                        out["sample_on_topic"].append(title[:120])
                    if e.get("link") and len(topic_links) < 3:
                        topic_links.append(e["link"])

            out["dated_ratio"] = round(dated / len(entries), 2)
            out["topic_hit"] = round(hits / len(entries), 2)
            if ages:
                out["median_age_h"] = round(statistics.median(ages), 1)

            # 본문 테스트는 '주제에 맞는 기사' 로 한다. 실제로 파이프라인이 읽게 될 기사가 그것이므로.
            if not topic_links:
                topic_links = [e.get("link") for e in entries[:3] if e.get("link")]

            oks, lens, walls = 0, [], 0
            for link in topic_links:
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


def verdict(r, m: TopicMatcher):
    """6개 기준으로 채택/탈락과 위반 코드를 낸다."""
    fails = []
    if not r["reachable"]:
        fails.append("C1")
        return "탈락", fails
    if r["on_topic_7d"] < m.min_on_topic_7d:
        fails.append("C2'")
    if r["topic_hit"] < m.min_topic_hit:
        fails.append("C3")
    if r["body_ok_rate"] < 0.60 or r["body_chars"] < 800:
        fails.append("C4")
    if r.get("max_overlap", 0) > 0.30:
        fails.append("C5")
    if r["paywall_rate"] > 0:
        fails.append("C6")
    return ("채택" if not fails else "탈락"), fails


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", default="tools/source_candidates.yaml")
    ap.add_argument("--out", default="", help="출력 파일 접미사 (예: ai -> source_audit_ai.json)")
    args = ap.parse_args()

    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    cfg = yaml.safe_load((ROOT / args.candidates).read_text(encoding="utf-8"))
    matcher = TopicMatcher(cfg.get("topic", DEFAULT_TOPIC))
    cands = cfg["candidates"]
    print(f"주제: {matcher.name or '(기본: 환경·기후)'} / 후보 {len(cands)}개\n")

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda c: probe_one(c, matcher), cands))

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
        r["verdict"], r["fails"] = verdict(r, matcher)

    suffix = ("_" + args.out) if args.out else ""
    (ROOT / "store").mkdir(exist_ok=True)
    (ROOT / "store" / f"source_audit{suffix}.json").write_text(
        json.dumps({"probed_at": NOW.isoformat(), "topic": matcher.name,
                    "thresholds": {"min_on_topic_7d": matcher.min_on_topic_7d,
                                   "min_topic_hit": matcher.min_topic_hit,
                                   "min_body_ok": 0.60, "min_body_chars": 800,
                                   "max_overlap": 0.30, "max_paywall": 0.0},
                    "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = ["| id | 소스 | 언어 | HTTP | 엔트리 | 7일 | 주제7일 | 주제48h | 주제적중 | 본문성공 | 본문길이 | 페이월 | 중복 | 판정 | 위반 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda x: (x["verdict"] != "채택", -x["on_topic_7d"])):
        rows.append(
            f"| {r['id']} | {r['name']} | {r['lang']} | {r['http_status']} | {r['n_entries']} | {r['fresh_7d']} | "
            f"**{r['on_topic_7d']}** | {r['on_topic_48h']} | {r['topic_hit']} | {r['body_ok_rate']} | "
            f"{r['body_chars']} | {r['paywall_rate']} | {r['max_overlap']} | {r['verdict']} | "
            f"{', '.join(r['fails']) or '-'} |")

    adopted = [r for r in results if r["verdict"] == "채택"]
    pool48 = sum(r["on_topic_48h"] for r in adopted)
    pool7d = sum(r["on_topic_7d"] for r in adopted)
    rows += ["", f"**채택 {len(adopted)}개 / 후보 {len(results)}개** — 채택 소스 합계 주제 기사 "
                 f"48시간 **{pool48}건**, 7일 **{pool7d}건** "
                 f"(풀 규모 기준 48h ≥ 15건 → {'충족' if pool48 >= 15 else '미달'})"]

    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / f"source_audit{suffix}.md").write_text(
        f"# 소스 실측 결과 — {matcher.name or '환경·기후'} ({NOW.strftime('%Y-%m-%d %H:%M UTC')})\n\n"
        + "\n".join(rows) + "\n", encoding="utf-8")
    print("\n".join(rows))
    print(f"\n-> store/source_audit{suffix}.json, docs/source_audit{suffix}.md")


if __name__ == "__main__":
    main()
