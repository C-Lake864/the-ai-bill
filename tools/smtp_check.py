"""SMTP 접속·로그인만 확인한다. 메일은 보내지 않는다.

파이프라인을 다 돌린 뒤 마지막 단계에서 자격증명 오타로 실패하면 시간과 토큰이 아깝다.
발행 전에 이것부터 돌려 로그인까지 되는지 확인한다.

  python tools/smtp_check.py
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nl.config import load_env  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    load_env()

    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO")
    sender = os.environ.get("MAIL_FROM", user or "")

    missing = [k for k, v in {"SMTP_HOST": host, "SMTP_USER": user,
                              "SMTP_PASS": pw, "MAIL_TO": to}.items() if not v]
    if missing:
        print(f"[FAIL] .env 에 비어 있는 항목: {', '.join(missing)}")
        return 1

    print(f"서버   {host}:{port} ({'SSL' if port == 465 else 'STARTTLS'})")
    print(f"계정   {user}")
    print(f"보내기 {sender} -> {to}")
    print("비밀번호는 출력하지 않습니다.\n")

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, pw)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(user, pw)
    except smtplib.SMTPAuthenticationError as ex:
        print(f"[FAIL] 인증 거부: {ex}")
        print("  - 네이버: 환경설정 > POP3/IMAP 설정에서 'POP3/SMTP 사용함' 이 켜져 있는지 확인")
        print("  - 2단계 인증 계정이면 로그인 비밀번호가 아니라 '애플리케이션 비밀번호' 여야 합니다")
        return 2
    except Exception as ex:
        print(f"[FAIL] 접속 실패: {type(ex).__name__}: {ex}")
        return 3

    print("[OK] 접속과 로그인 성공. 이제 python run.py 로 실제 발행하면 됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
