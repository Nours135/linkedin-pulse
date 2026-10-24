"""Tool: generate the email body and send it, with the resume PDF, to yourself via SMTP."""

from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from pathlib import Path

from pulse.config import settings, work_dir
from pulse.tools.linkedin import Job
from pulse.tools.matching import Match

OUTBOX = work_dir / "outbox"


def compose(job: Job, match: Match, resume: dict) -> EmailMessage:
    """`resume` is the resume agent's result: {"ok", "pdf", "error", ...}."""
    user = os.getenv("SMTP_USER", "")
    msg = EmailMessage()
    msg["Subject"] = f"[Pulse {match.score}] {job.title} @ {job.company}"
    msg["From"] = user
    msg["To"] = os.getenv("EMAIL_TO") or user

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
    attach = bool(resume.get("ok") and pdf)
    if attach:
        lines.append("Tailored resume attached.")
    else:
        lines.append(f"Resume not generated: {resume.get('error') or 'unknown error'}")
    # the body must be set before attaching: set_content on a multipart message raises
    msg.set_content("\n".join(lines))
    if attach:
        name = re.sub(r"[^\w-]+", "_", job.company).strip("_") or job.id
        msg.add_attachment(Path(pdf).read_bytes(), maintype="application", subtype="pdf",
                           filename=f"resume_{name}.pdf")
    return msg


def send(msg: EmailMessage, job_id: str) -> str:
    """Send via SMTP, or write work/outbox/<job_id>.eml when email.dry_run is on. Returns where it went."""
    if settings.get("email", {}).get("dry_run", True):
        OUTBOX.mkdir(parents=True, exist_ok=True)
        path = OUTBOX / f"{job_id}.eml"
        path.write_bytes(msg.as_bytes())
        return str(path)
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465"))) as smtp:
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
    return msg["To"]
