"""주제 전환 타당성 측정.

"AI×환경" / "AI×사회문제" 는 넓은 피드의 부분집합이라, 피드가 살아 있어도
교집합 기사가 하루 2~3건뿐이면 일간 뉴스레터가 성립하지 않는다.
그래서 후보 피드마다 '교집합 기사가 최근 48시간/7일에 몇 건인지'를 직접 센다.

측정
  fresh_48h / fresh_7d : 전체 신선 기사 수
  ai_48h               : AI 키워드에 걸린 기사 수
  env_x_ai_48h         : AI ∧ (전력·탄소·물·냉각 등 환경) 교집합
  soc_x_ai_48h         : AI ∧ (차별·감시·일자리·저작권 등 사회문제) 교집합
  body_ok / body_chars : 교집합 기사 2건을 실제로 받아 본문 추출 성공 여부

결과: store/topic_feasibility.json + docs/topic_feasibility.md
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from source_probe import extract_body, entry_dt, strip_tags, is_paywalled  # noqa: E402

UA = "Mozilla/5.0 (compatible; personal-newsletter-bot/1.0; local research project)"
NOW = datetime.now(timezone.utc)

AI_WORDS = [
    "ai ", " ai", "a.i.", "artificial intelligence", "machine learning", "deep learning",
    "llm", "large language model", "chatgpt", "gpt-", "generative", "chatbot", "algorithm",
    "data center", "data centre", "datacenter", "gpu", "neural network", "deepfake",
    "automation", "copilot", "anthropic", "openai", "nvidia", "model training", "agentic",
    "인공지능", "에이아이", "생성형", "챗봇", "챗지피티", "알고리즘", "데이터센터", "딥페이크",
    "거대언어모델", "머신러닝", "딥러닝", "자동화", "엔비디아", "반도체", "지피유",
]

ENV_WORDS = [
    "energy", "electricity", "power grid", "power demand", "megawatt", "gigawatt",
    "emission", "carbon", "climate", "water use", "water consumption", "cooling",
    "renewable", "solar", "wind power", "nuclear", "fossil", "environment", "pollution",
    "sustainab", "net zero", "grid", "전력", "전기", "탄소", "온실가스", "배출", "기후",
    "냉각", "용수", "물 사용", "재생에너지", "태양광", "풍력", "원전", "환경", "오염",
    "지속가능", "탄소중립", "전력망", "메가와트", "기가와트",
]

SOCIAL_WORDS = [
    "bias", "discriminat", "privacy", "surveillance", "misinformation", "disinformation",
    "deepfake", "job loss", "layoff", "labor", "labour", "worker", "copyright",
    "child", "teen", "minor", "harassment", "abuse", "scam", "fraud", "lawsuit", "sued",
    "court", "regulat", "ban ", "safety", "harm", "mental health", "inequality",
    "accountab", "transparen", "moderation", "consent", "exploit",
    "편향", "차별", "개인정보", "프라이버시", "감시", "허위", "가짜", "딥페이크",
    "일자리", "해고", "노동", "저작권", "아동", "청소년", "괴롭힘", "사기", "소송",
    "규제", "금지", "안전", "피해", "정신건강", "불평등", "책임", "투명성", "착취",
]


def hit(blob: str, words: list) -> bool:
    return any(w in blob for w in words)


def probe(c: dict) -> dict:
    out = {
        "id": c["id"], "name": c["name"], "url": c["url"], "lang": c["lang"], "type": c["type"],
        "reachable": False, "http_status": None, "n_entries": 0,
        "fresh_48h": 0, "fresh_7d": 0, "ai_48h": 0, "ai_7d": 0,
        "env_x_ai_48h": 0, "env_x_ai_7d": 0, "soc_x_ai_48h": 0, "soc_x_ai_7d": 0,
        "body_ok": None, "body_chars": 0, "paywall": False,
        "sample_env": [], "sample_soc": [], "error": None,
    }
    try:
        with httpx.Client(headers={"User-Agent": UA}, timeout=25, follow_redirects=True) as cli:
            r = cli.get(c["url"])
            out["http_status"] = r.status_code
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            entries = feed.entries or []
            out["n_entries"] = len(entries)
            if not entries:
                out["error"] = "no entries / parse failed"
                return out
            out["reachable"] = True

            probe_targets = []
            for e in entries:
                dt = entry_dt(e)
                if not dt:
                    continue
                age = (NOW - dt).total_seconds() / 3600
                if age > 24 * 7:
                    continue
                title = strip_tags(e.get("title", ""))
                summary = e.get("summary", "")
                if not summary and e.get("content"):
                    summary = e["content"][0].get("value", "")
                blob = (title + " " + strip_tags(summary)).lower()

                is_ai = hit(blob, AI_WORDS)
                is_env = is_ai and hit(blob, ENV_WORDS)
                is_soc = is_ai and hit(blob, SOCIAL_WORDS)
                fresh48 = age <= 48

                out["fresh_7d"] += 1
                out["fresh_48h"] += int(fresh48)
                out["ai_7d"] += int(is_ai)
                out["ai_48h"] += int(is_ai and fresh48)
                out["env_x_ai_7d"] += int(is_env)
                out["env_x_ai_48h"] += int(is_env and fresh48)
                out["soc_x_ai_7d"] += int(is_soc)
                out["soc_x_ai_48h"] += int(is_soc and fresh48)

                if is_env and len(out["sample_env"]) < 3:
                    out["sample_env"].append(title[:110])
                if is_soc and len(out["sample_soc"]) < 3:
                    out["sample_soc"].append(title[:110])
                if (is_env or is_soc) and e.get("link") and len(probe_targets) < 2:
                    probe_targets.append(e["link"])

            # 교집합 기사가 없으면 아무 신선 기사로라도 본문 추출 가능성은 확인한다
            if not probe_targets:
                probe_targets = [e.get("link") for e in entries[:2] if e.get("link")]

            oks, lens = 0, []
            for link in probe_targets:
                try:
                    rr = cli.get(link)
                    body = extract_body(rr.text)
                    lens.append(len(body))
                    if len(body) >= 600:
                        oks += 1
                    if is_paywalled(body):
                        out["paywall"] = True
                except Exception:
                    lens.append(0)
                time.sleep(0.3)
            if lens:
                out["body_ok"] = round(oks / len(lens), 2)
                out["body_chars"] = int(statistics.median(lens))
    except Exception as ex:
        out["error"] = (type(ex).__name__ + ": " + str(ex))[:160]
    return out


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    cands = yaml.safe_load((ROOT / "tools" / "topic_candidates.yaml").read_text(encoding="utf-8"))["candidates"]
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(probe, cands))

    (ROOT / "store").mkdir(exist_ok=True)
    (ROOT / "store" / "topic_feasibility.json").write_text(
        json.dumps({"probed_at": NOW.isoformat(), "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    rows = ["| id | 소스 | 언어 | HTTP | 7일 | 48h | AI 48h | **AI×환경 48h** | **AI×사회 48h** | AI×환경 7일 | AI×사회 7일 | 본문 | 비고 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda x: -(x["env_x_ai_7d"] + x["soc_x_ai_7d"])):
        rows.append(
            f"| {r['id']} | {r['name']} | {r['lang']} | {r['http_status']} | {r['fresh_7d']} | {r['fresh_48h']} | "
            f"{r['ai_48h']} | **{r['env_x_ai_48h']}** | **{r['soc_x_ai_48h']}** | {r['env_x_ai_7d']} | "
            f"{r['soc_x_ai_7d']} | {r['body_ok']}/{r['body_chars']} | {r['error'] or ('페이월' if r['paywall'] else '')} |")

    ok = [r for r in results if r["reachable"]]
    tot = {
        "env_48h": sum(r["env_x_ai_48h"] for r in ok), "env_7d": sum(r["env_x_ai_7d"] for r in ok),
        "soc_48h": sum(r["soc_x_ai_48h"] for r in ok), "soc_7d": sum(r["soc_x_ai_7d"] for r in ok),
    }
    rows += ["", f"**합계(도달 가능 {len(ok)}개 소스)** — AI×환경 48h **{tot['env_48h']}건** / 7일 {tot['env_7d']}건, "
                 f"AI×사회 48h **{tot['soc_48h']}건** / 7일 {tot['soc_7d']}건"]

    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "topic_feasibility.md").write_text(
        "# 주제 전환 타당성 실측 (" + NOW.strftime("%Y-%m-%d %H:%M UTC") + ")\n\n" + "\n".join(rows) + "\n",
        encoding="utf-8")
    print("\n".join(rows))
    print("\n-> store/topic_feasibility.json, docs/topic_feasibility.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
