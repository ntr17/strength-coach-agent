"""
Gmail sender using the Gmail API with OAuth2.
Reuses the same credentials as Google Sheets (single OAuth flow).
"""

import base64
from datetime import date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from io import BytesIO
from typing import Optional

from googleapiclient.discovery import build

from sheets import get_credentials
from config import GMAIL_FROM, GMAIL_TO


def _build_html(text: str, chart_cids: list[str] = None) -> str:
    """Convert plain text email to simple HTML (preserves line breaks)."""
    paragraphs = text.strip().split("\n\n")
    html_parts = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_html = para.replace("\n", "<br>")
        html_parts.append(f"<p>{para_html}</p>")

    charts_html = ""
    if chart_cids:
        chart_tags = "\n".join(
            f'<img src="cid:{cid}" style="max-width:100%;margin:16px 0;display:block;">'
            for cid in chart_cids
        )
        charts_html = f"<div>{chart_tags}</div>"

    return f"""<!DOCTYPE html>
<html>
<head>
<style>
  body {{ font-family: Georgia, serif; font-size: 15px; line-height: 1.6;
          color: #1a1a1a; max-width: 620px; margin: 40px auto; padding: 0 20px; }}
  p {{ margin: 0 0 1em 0; }}
</style>
</head>
<body>
{"".join(html_parts)}
{charts_html}
</body>
</html>"""


def send_email(subject: str, body: str,
               to: str = None, from_addr: str = None,
               charts: list[tuple[BytesIO, str]] = None) -> dict:
    """
    Send an email via Gmail API.

    Args:
        subject: Email subject line
        body: Plain text email body
        to: Recipient address (defaults to GMAIL_TO from config)
        from_addr: Sender address (defaults to GMAIL_FROM from config)
        charts: Optional list of (image_bytes_io, content_id) tuples for inline images.
                content_id should match the cid: reference in HTML (without angle brackets).

    Returns:
        Gmail API response dict
    """
    to = to or GMAIL_TO
    from_addr = from_addr or GMAIL_FROM

    chart_cids = [cid for _, cid in charts] if charts else []

    if charts:
        # multipart/related wraps multipart/alternative + inline images
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to

        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))
        alt.attach(MIMEText(_build_html(body, chart_cids), "html", "utf-8"))
        msg.attach(alt)

        for img_bytes, cid in charts:
            img_bytes.seek(0)
            img = MIMEImage(img_bytes.read())
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline")
            msg.attach(img)
    else:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(_build_html(body), "html", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)
    result = service.users().messages().send(
        userId="me",
        body={"raw": raw}
    ).execute()

    return result


def _extract_body(message: dict) -> str:
    """Extract plain text body from a Gmail message."""
    payload = message.get("payload", {})

    def get_text(part):
        if part.get("mimeType") == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        for subpart in part.get("parts", []):
            result = get_text(subpart)
            if result:
                return result
        return ""

    return get_text(payload)


def read_recent_replies(after_date: Optional[date] = None, max_results: int = 5) -> list[dict]:
    """
    Read recent replies to coaching emails from the Gmail inbox.
    Searches for inbox messages with 'Re: Week' in subject (standard reply prefix).

    Args:
        after_date: Only include messages after this date.
        max_results: Maximum number of reply messages to return.

    Returns:
        List of {"date": str, "subject": str, "body": str} dicts, newest first.
    """
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)

    query = 'subject:"Re: Week" is:inbox'
    if after_date:
        query += f' after:{after_date.strftime("%Y/%m/%d")}'

    try:
        results = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
    except Exception:
        return []

    messages = results.get("messages", [])
    replies = []
    for msg_ref in messages:
        try:
            msg = service.users().messages().get(
                userId="me", messageId=msg_ref["id"], format="full"
            ).execute()
            headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
            subject = headers.get("Subject", "")
            date_str = headers.get("Date", "")
            body = _extract_body(msg).strip()
            if body:
                replies.append({
                    "date": date_str,
                    "subject": subject,
                    "body": body[:800],
                })
        except Exception:
            continue

    return replies


if __name__ == "__main__":
    # Quick test: send a test email
    print(f"Sending test email to {GMAIL_TO}...")
    result = send_email(
        subject="Coach Agent — Test Email",
        body="This is a test email from your strength coach agent. If you received this, Gmail sending is working correctly."
    )
    print(f"Sent. Message ID: {result.get('id')}")
