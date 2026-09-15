"""1단계: 수집 + 규칙 기반 1차 필터 + 중복 제거.

LLM 을 쓰기 전에 값싼 규칙으로 최대한 걸러낸다. 버린 아이템도 전부 사유와 함께 남긴다
(store/runs/<run_id>/01_collected.json). '기준이 의도대로 동작했다'는 근거가 이 로그다.
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx

UTM = re.compile(r"^(utm_|fbclid|gclid|mc_cid|mc_eid|ref)", re.I)


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def canon_url(u: str) -> str:
    try:
        p = urlsplit(u)
        q = "&".join(x for x in p.query.split("&") if x and not UTM.match(x.split("=")[0]))
        return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/"), q, ""))
    except Exception:
        return u


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", (t or "").lower())


def entry_dt(e):
    for key in ("published_parsed", "updated_parsed"):
        tp = e.get(key)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def _fetch_one(src: dict, fetch_cfg: dict) -> tuple[list, dict]:
    """소스 하나에서 아이템 목록과 수집 상태를 돌려준다. 실패해도 예외를 밖으로 내지 않는다."""
    status = {"source": src["id"], "ok": False, "n_raw": 0, "error": None, "latency_ms": None}
    items = []
    t0 = time.time()
    try:
        with httpx.Client(headers={"User-Agent": fetch_cfg["user_agent"]},
                          timeout=fetch_cfg["timeout_sec"], follow_redirects=True) as cli:
            r = cli.get(src["url"])
            status["latency_ms"] = int((time.time() - t0) * 1000)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        entries = (feed.entries or [])[: fetch_cfg["max_items_per_source"]]
        status["n_raw"] = len(entries)
        for e in entries:
            link = canon_url(e.get("link") or "")
            if not link:
                continue
            dt = entry_dt(e)
            summary = e.get("summary", "")
            if not summary and e.get("content"):
                summary = e["content"][0].get("value", "")
            items.append({
                "id": hashlib.sha1(link.encode("utf-8")).hexdigest()[:12],
                "source_id": src["id"],
                "source_name": src["name"],
                "bucket": src.get("bucket", ""),
                "weight": float(src.get("weight", 1.0)),
                "lang": src.get("lang", "en"),
                "title": strip_tags(e.get("title", "")),
                "url": link,
                "published": dt.isoformat() if dt else None,
                "age_hours": round((datetime.now(timezone.utc) - dt).total_seconds() / 3600, 1) if dt else None,
                "feed_summary": strip_tags(summary)[:1200],
            })
        status["ok"] = True
    except Exception as ex:
        status["error"] = (type(ex).__name__ + ": " + str(ex))[:200]
    return items, status


def collect(sources_cfg: dict, exclude_cfg: dict) -> dict:
    """수집 결과 + 필터 로그를 한 번에 반환."""
    fetch_cfg = sources_cfg["fetch"]
    srcs = sources_cfg["sources"]

    raw, source_status = [], []
    with cf.ThreadPoolExecutor(max_workers=fetch_cfg.get("concurrency", 8)) as ex:
        for items, st in ex.map(lambda s: _fetch_one(s, fetch_cfg), srcs):
            raw.extend(items)
            source_status.append(st)

    max_age = exclude_cfg["max_age_hours"]
    blocklist = [w.lower() for w in exclude_cfg.get("title_blocklist", [])]
    min_title = exclude_cfg.get("min_title_chars", 10)
    gate = TopicGate(exclude_cfg.get("topic_gate"))

    kept, dropped = [], []
    seen_url, seen_title = set(), {}

    for it in sorted(raw, key=lambda x: (x["age_hours"] is None, x["age_hours"] or 1e9)):
        t_low = it["title"].lower()
        reason = None
        if len(it["title"]) < min_title:
            reason = f"rule:short_title(<{min_title})"
        elif it["published"] is None:
            reason = "rule:no_date"
        elif it["age_hours"] > max_age:
            reason = f"rule:stale({it['age_hours']}h>{max_age}h)"
        elif any(w in t_low for w in blocklist):
            hit = next(w for w in blocklist if w in t_low)
            reason = f"rule:blocklist('{hit}')"
        elif not gate.passes(it["title"], it["feed_summary"]):
            reason = "rule:off_topic(주제 게이트)"
        elif it["url"] in seen_url:
            reason = "dedup:same_url"
        else:
            nt = norm_title(it["title"])
            dup = next((o for o in seen_title if _near_dup(nt, o)), None)
            if dup:
                reason = f"dedup:near_title({seen_title[dup]})"

        if reason:
            dropped.append({**it, "drop_reason": reason})
        else:
            seen_url.add(it["url"])
            seen_title[norm_title(it["title"])] = it["source_id"]
            kept.append(it)

    return {
        "source_status": source_status,
        "n_raw": len(raw),
        "kept": kept,
        "dropped": dropped,
    }


def _compile_group(words) -> re.Pattern | None:
    """키워드 목록 -> 정규식. 영문 토큰에는 단어 경계를 붙인다.

    부분 문자열로 찾으면 'said'/'Thai'/'email' 안의 'ai' 까지 잡힌다(실측으로 확인).
    한글은 \\b 가 의미 없어 부분 문자열 그대로 둔다.
    """
    alts = []
    for w in words or []:
        w = str(w).strip().lower()
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


class TopicGate:
    """LLM 이전 단계에서 주제와 무관한 기사를 쳐내는 규칙 필터 (비용 0)."""

    def __init__(self, cfg: dict | None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled"))
        self.require_all = cfg.get("require_all", [])
        self.require_any = cfg.get("require_any", [])
        self.pats = {g: _compile_group(w) for g, w in (cfg.get("groups") or {}).items()}

    def _hit(self, group: str, blob: str) -> bool:
        p = self.pats.get(group)
        return bool(p and p.search(blob))

    def passes(self, title: str, summary: str) -> bool:
        if not self.enabled:
            return True
        blob = (title + " " + summary).lower()
        if not all(self._hit(g, blob) for g in self.require_all):
            return False
        if self.require_any and not any(self._hit(g, blob) for g in self.require_any):
            return False
        return True


def _near_dup(a: str, b: str) -> bool:
    """제목 근접 중복: 짧은 쪽이 긴 쪽에 포함되거나 토큰 자카드 0.8 이상."""
    if not a or not b:
        return False
    if a == b or (len(a) > 20 and (a in b or b in a)):
        return True
    ta, tb = set(re.findall(r"[a-z0-9가-힣]{2,}", a)), set(re.findall(r"[a-z0-9가-힣]{2,}", b))
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.8
