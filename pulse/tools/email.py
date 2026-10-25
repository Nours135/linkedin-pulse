"""Tool: generate the email body and send it, with the resume PDF, to yourself via SMTP."""

from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from tqdm import tqdm

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


def preview(msg: EmailMessage, saved_to: str | Path) -> str:
    """Human-readable dump of a message: headers, attachments and the plain-text body."""
    attachments = [f"{a.get_filename()} ({len(a.get_content()) // 1024} KB)" for a in msg.iter_attachments()]
    body = msg.get_body(preferencelist=("plain",)).get_content().rstrip()
    rule = "-" * 72
    return "\n".join([
        rule,
        f"email sent and saved to {saved_to}",
        f"To:         {msg['To'] or '(EMAIL_TO / SMTP_USER not set)'}",
        f"Subject:    {msg['Subject']}",
        f"Attachment: {', '.join(attachments) or '-'}",
        "",
        body,
        rule,
    ])


def send(msg: EmailMessage, job_id: str) -> str:
    """Send via SMTP, or write work/outbox/<job_id>.eml and print it when email.dry_run is on.
    Returns where it went."""
    if settings.get("email", {}).get("dry_run", True):
        OUTBOX.mkdir(parents=True, exist_ok=True)
        path = OUTBOX / f"{job_id}.eml"
        path.write_bytes(msg.as_bytes())
        tqdm.write(preview(msg, path))  # tqdm.write keeps the job progress bar intact
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
