"""Tool: generate the email body and send it, with the resume PDF, to yourself via SMTP."""

from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from pulse.config import settings, work_dir
from pulse.tools.linkedin import Job
from pulse.tools.matching import Match

OUTBOX = work_dir / "outbox"
SMTP_TIMEOUT = 30  # seconds; without it a stalled server hangs the whole heartbeat


def _one_line(s: str) -> str:
    # header values may not contain CR/LF; scraped titles occasionally do
    return " ".join(s.split())


def compose(job: Job, match: Match, resume: dict) -> EmailMessage:
    """`resume` is the resume agent's result: {"ok", "pdf", "error", ...}."""
    user = os.getenv("SMTP_USER", "")
    msg = EmailMessage()
    msg["Subject"] = _one_line(f"[Pulse {match.score}] {job.title} @ {job.company}")
    msg["From"] = user
    msg["To"] = os.getenv("EMAIL_TO") or user
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="linkedin-pulse")

    lines = [
        f"{job.title} @ {job.company}",
        f"{job.location} | posted {job.posted_at}",
        job.url,
        "",
        f"Match score: {match.score}/100",
        *[f"- {r}" for r in match.reasons],
        "",
        f"Matched: {', '.join(match.matched) or '-'}",
        f"Missing: {', '.join(match.missing) or '-'}",
        "",
    ]
    pdf = resume.get("pdf")
    attach = bool(resume.get("ok") and pdf and Path(pdf).is_file())
    if attach:
        lines.append("Tailored resume attached.")
    elif resume.get("ok") and pdf:
        lines.append(f"Resume was generated but the file is missing: {pdf}")
    else:
        lines.append(f"Resume not generated: {resume.get('error') or 'unknown error'}")
    # the body must be set before attaching: set_content on a multipart message raises
    msg.set_content("\n".join(lines))
    if attach:
        name = re.sub(r"[^\w-]+", "_", job.company).strip("_") or job.id
        msg.add_attachment(Path(pdf).read_bytes(), maintype="application", subtype="pdf",
                           filename=f"resume_{name}.pdf")
    return msg


def check_config() -> None:
    """Raise early if real sending is on but SMTP is not configured. Call at startup: a send
    failure leaves the job unrecorded, so it would be re-scored and re-tailored every heartbeat."""
    if settings.get("email", {}).get("dry_run", True):
        return
    missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD") if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"email.dry_run is off but {', '.join(missing)} not set in .env")


def send(msg: EmailMessage, job_id: str) -> str:
    """Send via SMTP, or write work/outbox/<job_id>.eml when email.dry_run is on. Returns where it went."""
    if settings.get("email", {}).get("dry_run", True):
        OUTBOX.mkdir(parents=True, exist_ok=True)
        path = OUTBOX / f"{job_id}.eml"
        path.write_bytes(msg.as_bytes())
        return str(path)
    check_config()
    user, password = os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"]
    host, port = os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))
    if port == 465:  # implicit TLS
        smtp = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT)
    else:            # 587 and others: plain connect, then STARTTLS
        smtp = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT)
        smtp.starttls()
    with smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    return msg["To"]
