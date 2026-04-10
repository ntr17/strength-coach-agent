"""
Lightweight Telegram send-only helper.

Used by the email pipeline to push proactive alerts without needing the full bot running.
Just an HTTP POST to the Telegram Bot API — no persistent connection required.

To send a message, the bot must have already started a conversation with the user
(user must have sent /start to the bot at least once).
"""

import urllib.request
import urllib.parse
import json
import sys

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_telegram_message(text: str, chat_id: str = None) -> bool:
    """
    Send a text message via the Telegram Bot API.
    Returns True on success, False on failure (non-fatal).
    """
    token = TELEGRAM_BOT_TOKEN
    cid = chat_id or TELEGRAM_CHAT_ID

    if not token or not cid:
        print("  [Telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping.", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": cid,
        "text": text,
        "parse_mode": "HTML",
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("ok", False)
    except Exception as e:
        print(f"  [Telegram] Send failed: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    # Quick test: python src/telegram_utils.py "Hello from coach"
    msg = " ".join(sys.argv[1:]) or "Test message from strength coach agent."
    ok = send_telegram_message(msg)
    print("Sent." if ok else "Failed.")
