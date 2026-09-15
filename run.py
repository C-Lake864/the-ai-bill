"""파이프라인 실행 스크립트.

  python run.py              # 수집 → 선별 → 요약 → 검수 → 메일 발행
  python run.py --dry-run    # 메일만 보내지 않고 나머지 전부 실행 (out/ 에 사본 생성)
  python run.py --graph      # 그래프 구조만 출력하고 종료
"""
from __future__ import annotations

import argparse
import sys
import traceback

from nl.config import load_env


def main() -> int:
    # 윈도우 콘솔(cp949)에서 한글/이모지 출력이 죽지 않도록
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="메일 전송 없이 실행")
    ap.add_argument("--graph", action="store_true", help="그래프 구조(mermaid) 출력 후 종료")
    args = ap.parse_args()

    load_env()
    from graph import build_graph, initial_state

    app = build_graph()

    if args.graph:
        print(app.get_graph().draw_mermaid())
        return 0

    state = initial_state(dry_run=args.dry_run)
    print(f"=== run_id {state['run_id']} / {state['audience']['newsletter']['name']} ===")
    try:
        final = app.invoke(state, config={"recursion_limit": 150})
    except Exception:
        traceback.print_exc()
        print("\n파이프라인이 예외로 중단되었습니다. store/runs/ 의 마지막 덤프를 확인하세요.")
        return 2

    m = final["metrics"]
    pr = final["publish_result"]
    print("\n--- 요약 ---")
    print(f"수집 {m['n_collected']} → 규칙통과 {m['n_after_rules']} → 본선 {m['n_finalists']} "
          f"→ 발행 {m['n_published']}건 (검수탈락 {m['n_verify_failed']}, 재생성 {m['n_regenerated']}회)")
    print(f"LLM 호출 {m['llm_usage']['calls']}회 / "
          f"{m['llm_usage']['prompt_tokens'] + m['llm_usage']['completion_tokens']:,} 토큰")
    print(f"발행 상태: {pr.get('status')}  파일: {final.get('files', {}).get('html')}")

    # 발행이 확실히 실패했고 기사도 없으면 실패 코드로 끝낸다 (스케줄러가 감지할 수 있게)
    if m["n_published"] == 0:
        return 1
    if pr.get("status") in ("smtp_error",):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
