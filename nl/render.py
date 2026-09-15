"""5단계 준비: 이메일용 HTML / 아카이브용 Markdown 렌더링.

이메일 클라이언트는 외부 CSS·flex·grid 를 잘라먹으므로 인라인 스타일과 테이블만 쓴다.
"""
from __future__ import annotations

import html
from datetime import datetime

BADGE = {
    "정책·과학": "#0f766e", "산업·기술": "#1d4ed8", "산업·사회": "#7c3aed",
    "속보·종합": "#b45309", "심층·환경": "#15803d", "연구": "#0891b2",
    "국내 ESG": "#be123c", "국내 기후·생태": "#047857", "국내 에너지 산업": "#4338ca",
}


def _e(s) -> str:
    return html.escape(str(s or ""))


def render_html(cfg: dict, date: datetime, articles: list, meta: dict) -> str:
    name = cfg["newsletter"]["name"]
    sub = cfg["newsletter"]["subtitle"]
    dstr = date.strftime("%Y년 %m월 %d일 (%a)")

    blocks = []
    for i, a in enumerate(articles, 1):
        s, it, v = a["summary"], a["item"], a["verify"]
        color = BADGE.get(it.get("bucket", ""), "#334155")
        blocks.append(f"""
      <tr><td style="padding:0 0 28px 0;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
               style="border:1px solid #e2e8f0;border-radius:10px;background:#ffffff;">
          <tr><td style="padding:20px 22px;">
            <div style="font-size:12px;color:{color};font-weight:700;letter-spacing:.3px;margin-bottom:6px;">
              {i:02d} · {_e(it['source_name'])} · {_e(it.get('bucket',''))}
            </div>
            <div style="font-size:19px;line-height:1.35;font-weight:700;color:#0f172a;margin-bottom:12px;">
              {_e(s.get('headline'))}
            </div>
            <div style="font-size:14px;line-height:1.75;color:#334155;margin-bottom:14px;">
              {_e(s.get('what_happened'))}
            </div>
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
                   style="background:#f8fafc;border-left:3px solid {color};border-radius:0 6px 6px 0;margin-bottom:14px;">
              <tr><td style="padding:12px 14px;">
                <div style="font-size:11px;font-weight:700;color:{color};margin-bottom:5px;">왜 중요한가</div>
                <div style="font-size:13.5px;line-height:1.7;color:#1e293b;">{_e(s.get('why_it_matters'))}</div>
              </td></tr>
            </table>
            <div style="font-size:13.5px;line-height:1.7;color:#0f172a;margin-bottom:14px;">
              <span style="font-weight:700;">&#9654; 실무 체크</span> {_e(s.get('reader_takeaway'))}
            </div>
            <div style="font-size:12px;color:#64748b;">
              <a href="{_e(it['url'])}" style="color:#1d4ed8;text-decoration:none;font-weight:600;">원문 보기 &rarr;</a>
              &nbsp;·&nbsp; 검수 통과 (근거 확인 {v['n_supported']}/{v['n_claims']})
              &nbsp;·&nbsp; 선별점수 {it.get('weighted')}
            </div>
          </td></tr>
        </table>
      </td></tr>""")

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(name)} {dstr}</title></head>
<body style="margin:0;padding:0;background:#f1f5f9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="600" cellpadding="0" cellspacing="0"
         style="max-width:600px;width:100%;font-family:-apple-system,BlinkMacSystemFont,'Malgun Gothic','Apple SD Gothic Neo',sans-serif;">
    <tr><td style="padding:0 0 20px 0;">
      <div style="font-size:24px;font-weight:800;color:#0f172a;letter-spacing:-.4px;">{_e(name)}</div>
      <div style="font-size:13px;color:#64748b;margin-top:4px;">{_e(sub)}</div>
      <div style="font-size:13px;color:#0f766e;margin-top:10px;font-weight:600;">{dstr} · 오늘의 {len(articles)}건</div>
      <div style="height:3px;background:#0f172a;margin-top:12px;"></div>
    </td></tr>
    {''.join(blocks)}
    <tr><td style="padding:16px 4px 0 4px;border-top:1px solid #cbd5e1;">
      <div style="font-size:11.5px;line-height:1.8;color:#64748b;">
        수집 {meta['n_collected']}건 → 규칙 필터 후 {meta['n_after_rules']}건 →
        예선 통과 {meta['n_prescreen_pass']}건 → 본선 {meta['n_finalists']}건 →
        검수 통과 {len(articles)}건<br>
        모든 요약은 원문 대조 자동 검수를 통과한 것만 실었습니다.
        검수 재생성 {meta['n_regenerated']}회 · 재생성 후에도 통과하지 못해 제외한 기사 {meta['n_verify_failed']}건<br>
        run_id {meta['run_id']} · 생성 {date.strftime('%Y-%m-%d %H:%M')}
      </div>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


def render_markdown(cfg: dict, date: datetime, articles: list, meta: dict) -> str:
    lines = [f"# {cfg['newsletter']['name']} — {date.strftime('%Y-%m-%d')}", "",
             f"> {cfg['newsletter']['subtitle']}", ""]
    for i, a in enumerate(articles, 1):
        s, it, v = a["summary"], a["item"], a["verify"]
        lines += [
            f"## {i}. {s.get('headline')}",
            f"`{it['source_name']}` · `{it.get('bucket','')}` · 선별점수 {it.get('weighted')} · "
            f"검수 근거 {v['n_supported']}/{v['n_claims']}",
            "",
            f"**무슨 일**  {s.get('what_happened')}", "",
            f"**왜 중요한가**  {s.get('why_it_matters')}", "",
            f"**실무 체크**  {s.get('reader_takeaway')}", "",
            f"[원문 보기]({it['url']})", "", "---", "",
        ]
    lines += [
        "### 파이프라인 기록", "",
        f"- 수집 {meta['n_collected']} → 규칙 필터 후 {meta['n_after_rules']} → "
        f"예선 통과 {meta['n_prescreen_pass']} → 본선 {meta['n_finalists']} → 발행 {len(articles)}",
        f"- 검수 탈락 {meta['n_verify_failed']}건 / 재생성 {meta['n_regenerated']}회",
        f"- run_id `{meta['run_id']}`",
    ]
    return "\n".join(lines)
