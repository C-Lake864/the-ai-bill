"""공개 웹 아카이브(GitHub Pages) 생성.

발행할 때마다 다음을 만든다.
  docs/issues/YYYY-MM-DD.html  그날 호 (이메일과 같은 HTML)
  docs/issues.json             호별 메타데이터 목록 (목차를 여기서 다시 만든다)
  docs/index.html              목차 페이지
  docs/.nojekyll               Jekyll 처리를 끄기 위한 빈 파일

파일만 만들고 push 는 하지 않는다. 공개 발행은 되돌리기 어려운 동작이라
tools/publish_site.py 를 따로 실행하거나, .env 에 SITE_AUTO_PUSH=true 를 둔 경우에만 밀어올린다.
"""
from __future__ import annotations

import html as H
import json
from datetime import datetime
from pathlib import Path

DISCLAIMER = (
    "각 꼭지는 원문 기사를 AI가 요약하고 자동 검수를 거친 것입니다. "
    "정확한 내용과 맥락은 반드시 원문 링크를 확인하세요."
)


def _e(s) -> str:
    return H.escape(str(s or ""))


def save_issue(root: Path, date_str: str, issue_html: str, articles: list, meta: dict,
               cfg: dict | None = None) -> dict:
    """그날 호를 저장하고 목차를 다시 만든다. 생성한 파일 경로를 돌려준다."""
    docs = root / "docs"
    issues_dir = docs / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    issue_path = issues_dir / f"{date_str}.html"
    issue_path.write_text(issue_html, encoding="utf-8")

    manifest_path = docs / "issues.json"
    manifest = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = []

    entry = {
        "date": date_str,
        "n": len(articles),
        "run_id": meta.get("run_id"),
        "headlines": [
            {
                "headline": a["summary"].get("headline", ""),
                "source": a["item"].get("source_name", ""),
                "bucket": a["item"].get("bucket", ""),
                "url": a["item"].get("url", ""),
            }
            for a in articles
        ],
        "pipeline": {
            "collected": meta.get("n_collected"),
            "after_rules": meta.get("n_after_rules"),
            "finalists": meta.get("n_finalists"),
            "published": len(articles),
            "regenerated": meta.get("n_regenerated"),
        },
    }
    manifest = [m for m in manifest if m.get("date") != date_str] + [entry]
    manifest.sort(key=lambda m: m["date"], reverse=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    index_path = docs / "index.html"
    index_path.write_text(render_index(manifest, cfg), encoding="utf-8")
    return {"issue": str(issue_path), "index": str(index_path), "manifest": str(manifest_path)}


def render_index(manifest: list, cfg: dict | None = None) -> str:
    name = "The AI Bill"
    subtitle = "AI의 발전으로 인한 사회적 비용과 책임"
    if cfg:
        name = cfg.get("newsletter", {}).get("name", name)
        subtitle = cfg.get("newsletter", {}).get("subtitle", subtitle)

    total_issues = len(manifest)
    total_items = sum(m.get("n", 0) for m in manifest)

    cards = []
    for m in manifest:
        heads = "".join(
            f"""<li style="margin:0 0 10px 0;">
                  <div style="font-weight:600;color:var(--fg);line-height:1.45;">{_e(h['headline'])}</div>
                  <div style="font-size:12px;color:var(--muted);margin-top:2px;">{_e(h['source'])}
                    &middot; {_e(h['bucket'])}</div>
                </li>"""
            for h in m.get("headlines", [])
        )
        p = m.get("pipeline", {})
        cards.append(f"""
        <article class="card">
          <header style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;">
            <a class="date" href="issues/{_e(m['date'])}.html">{_e(m['date'])}</a>
            <span class="meta">{m.get('n', 0)}건</span>
          </header>
          <ul style="list-style:none;padding:0;margin:14px 0 0 0;">{heads}</ul>
          <footer class="meta" style="margin-top:12px;">
            수집 {p.get('collected', '-')} → 규칙통과 {p.get('after_rules', '-')} →
            본선 {p.get('finalists', '-')} → 발행 {p.get('published', '-')}
            &middot; <a href="issues/{_e(m['date'])}.html">전체 보기 →</a>
          </footer>
        </article>""")

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(name)} — 아카이브</title>
<meta name="description" content="{_e(subtitle)}">
<style>
  :root {{
    --bg:#f1f5f9; --card:#ffffff; --fg:#0f172a; --muted:#64748b;
    --accent:#0f766e; --line:#e2e8f0;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#0b1120; --card:#131c2e; --fg:#e8eefc; --muted:#94a3b8;
             --accent:#5eead4; --line:#24324a; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--fg);
    font-family:-apple-system,BlinkMacSystemFont,'Malgun Gothic','Apple SD Gothic Neo',sans-serif; }}
  .wrap {{ max-width:720px; margin:0 auto; padding-block:40px; padding-left:20px; padding-right:20px; }}
  h1 {{ font-size:30px; letter-spacing:-.5px; margin:0 0 6px 0; }}
  .sub {{ color:var(--muted); font-size:15px; margin:0 0 18px 0; }}
  .stats {{ font-size:13px; color:var(--accent); font-weight:600; }}
  .rule {{ height:3px; background:var(--fg); margin:14px 0 28px 0; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           padding:20px 22px; margin:0 0 18px 0; }}
  .date {{ font-size:17px; font-weight:800; color:var(--accent); text-decoration:none; }}
  .meta {{ font-size:12px; color:var(--muted); }}
  a {{ color:var(--accent); }}
  .note {{ font-size:12.5px; line-height:1.8; color:var(--muted);
           border-top:1px solid var(--line); margin-top:28px; padding-top:16px; }}
</style></head>
<body><div class="wrap">
  <h1>{_e(name)}</h1>
  <p class="sub">{_e(subtitle)}</p>
  <div class="stats">{total_issues}개 호 &middot; 기사 {total_items}건</div>
  <div class="rule"></div>
  {''.join(cards) if cards else '<p class="meta">아직 발행된 호가 없습니다.</p>'}
  <div class="note">
    {_e(DISCLAIMER)}<br>
    수집 → 선별(예선·본선) → 요약 → 자동 검수 → 발행까지 매일 자동으로 돌아가는 파이프라인이
    만듭니다. 검수를 통과하지 못한 기사는 싣지 않습니다.
    소스 선정 근거와 설계 판단은
    <a href="https://github.com/C-Lake864/the-ai-bill/blob/master/REPORT.md">REPORT.md</a>
    에 공개돼 있습니다.
  </div>
</div></body></html>"""
