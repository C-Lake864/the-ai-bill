"""공개 웹 아카이브를 GitHub 에 올린다 (docs/ 를 커밋·푸시).

run.py 는 docs/ 아래 파일만 만들고 push 하지 않는다. 공개 발행은 되돌리기 어려운 동작이라
이 스크립트를 따로 실행해야 실제로 공개된다.

  python tools/publish_site.py            # 오늘 자 호를 커밋·푸시
  python tools/publish_site.py --dry-run  # 무엇이 올라갈지만 보여주고 멈춤
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list, check: bool = True) -> str:
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise SystemExit(f"[FAIL] {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")
    return (r.stdout or "").strip()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    index = ROOT / "docs" / "index.html"
    manifest = ROOT / "docs" / "issues.json"
    if not index.exists() or not manifest.exists():
        print("[FAIL] docs/index.html 또는 docs/issues.json 이 없습니다. 먼저 python run.py 를 실행하세요.")
        return 1

    issues = json.loads(manifest.read_text(encoding="utf-8"))
    latest = issues[0]["date"] if issues else datetime.now().strftime("%Y-%m-%d")
    print(f"최신 호: {latest} ({issues[0]['n']}건)" if issues else "발행된 호 없음")

    run(["git", "add", "docs", "out", "store/metrics.jsonl"])
    staged = run(["git", "diff", "--cached", "--name-only"])
    if not staged:
        print("변경 사항이 없습니다. 올릴 것이 없습니다.")
        return 0

    print("\n올라갈 파일:")
    for line in staged.splitlines():
        print("  ", line)

    if args.dry_run:
        run(["git", "reset"], check=False)
        print("\n--dry-run 이라 커밋하지 않았습니다.")
        return 0

    run(["git", "commit", "-m", f"뉴스레터 발행: {latest}"])
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    run(["git", "push", "origin", branch])
    print(f"\n[OK] 푸시 완료. 1~2분 뒤 공개 페이지에 반영됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
