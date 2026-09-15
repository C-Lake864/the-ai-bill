"""대화형으로 .env 의 SMTP 값을 채운다.

.env 를 편집기로 여는 게 번거롭거나(윈도우에서 확장자 연결이 없어 엉뚱한 프로그램이 뜨는 경우)
따옴표·공백 때문에 값이 잘못 들어가는 걸 피하고 싶을 때 쓴다.

  python tools/setup_env.py

- 비밀번호는 입력하는 동안 화면에 찍히지 않는다(getpass).
- 기존 .env 의 주석과 다른 값은 그대로 두고, 해당 키의 값만 바꾼다.
- 아무것도 입력하지 않고 Enter 를 치면 그 항목은 기존 값을 유지한다.
"""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

FIELDS = [
    ("SMTP_HOST", "SMTP 서버", "smtp.naver.com", False),
    ("SMTP_PORT", "포트 (587=STARTTLS, 465=SSL)", "587", False),
    ("SMTP_USER", "네이버 아이디 (@naver.com 없이)", "", False),
    ("SMTP_PASS", "비밀번호 (2단계 인증이면 애플리케이션 비밀번호)", "", True),
    ("MAIL_FROM", "보내는 주소 (예: hongildong@naver.com)", "", False),
    ("MAIL_TO", "받는 주소 (쉼표로 여러 명)", "", False),
]


def read_existing() -> dict:
    if not ENV.exists():
        return {}
    out = {}
    for raw in ENV.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def write_values(values: dict) -> None:
    """기존 파일의 주석·구조를 보존하면서 해당 키의 줄만 교체한다."""
    lines = ENV.read_text(encoding="utf-8-sig").splitlines() if ENV.exists() else []
    seen = set()
    out = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in values:
                out.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        out.append(raw)
    for key, val in values.items():
        if key not in seen:
            out.append(f"{key}={val}")
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not sys.stdin.isatty():
        print("[FAIL] 이 스크립트는 직접 터미널에서 실행해야 합니다.")
        print("       PowerShell 에서: python tools/setup_env.py")
        return 1

    print(f"설정 파일: {ENV}")
    print("Enter 만 치면 기존 값을 그대로 둡니다. 비밀번호는 화면에 표시되지 않습니다.\n")

    existing = read_existing()
    values = {}
    for key, label, default, secret in FIELDS:
        cur = existing.get(key, "")
        shown = "(설정됨)" if (cur and secret) else (cur or default or "(비어 있음)")
        prompt = f"{label}\n  현재: {shown}\n  입력> "
        try:
            entered = getpass.getpass(prompt) if secret else input(prompt)
        except (EOFError, KeyboardInterrupt):
            print("\n취소했습니다. 아무것도 바꾸지 않았습니다.")
            return 1
        entered = entered.strip().strip('"').strip("'")
        if entered:
            values[key] = entered
        elif not cur and default:
            values[key] = default
        print()

    if not values:
        print("바뀐 값이 없습니다.")
        return 0

    write_values(values)
    os.environ.update(values)
    print(f"[OK] {ENV} 를 저장했습니다. 바꾼 항목: {', '.join(values)}")
    print("\n이제 접속을 확인하세요:  python tools/smtp_check.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
