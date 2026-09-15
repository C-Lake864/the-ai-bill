"""5단계: 발행 (SMTP 이메일) + 로컬 아카이브.

- 자격증명은 .env 에서만 읽는다. 코드/저장소에 넣지 않는다.
- 메일 전송이 실패해도 로컬 사본은 반드시 남기고, fail_open 이면 파이프라인은 성공으로 본다.
  (뉴스레터가 하루 안 나가는 것보다 조용히 죽는 게 더 나쁘다. 실패는 metrics 에 기록된다.)
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from pathlib import Path


def save_local(out_dir: Path, date_str: str, html: str, md: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    hp = out_dir / f"{date_str}.html"
    mp = out_dir / f"{date_str}.md"
    hp.write_text(html, encoding="utf-8")
    mp.write_text(md, encoding="utf-8")
    return {"html": str(hp), "md": str(mp)}


def send_email(subject: str, html: str, md: str) -> dict:
    """환경변수: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, MAIL_FROM, MAIL_TO"""
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("MAIL_TO")
    if not all([host, user, pw, to]):
        missing = [k for k, v in {"SMTP_HOST": host, "SMTP_USER": user,
                                  "SMTP_PASS": pw, "MAIL_TO": to}.items() if not v]
        return {"sent": False, "status": "skipped_no_credentials", "missing": missing}

    port = int(os.environ.get("SMTP_PORT", "587"))
    sender = os.environ.get("MAIL_FROM", user)
    recipients = [a.strip() for a in to.split(",") if a.strip()]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("오늘의 기후 브리핑", sender))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(md)
    msg.add_alternative(html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=45) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=45) as s:
                s.ehlo()
                s.starttls(context=ctx)
                s.login(user, pw)
                s.send_message(msg)
        return {"sent": True, "status": "ok", "to": recipients, "host": host, "port": port}
    except Exception as ex:
        return {"sent": False, "status": "smtp_error",
                "error": (type(ex).__name__ + ": " + str(ex))[:300],
                "to": recipients, "host": host, "port": port}
