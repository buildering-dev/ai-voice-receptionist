"""Tell the agency about new bookings, messages and call summaries (Telegram, or the log)."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("receptionist")


class Notifier:
    def __init__(self, telegram_token: str = "", telegram_chat_id: str = ""):
        self.token = telegram_token
        self.chat_id = telegram_chat_id
        self.sent: list[str] = []  # kept for tests and the admin page

    def send(self, text: str) -> None:
        self.sent.append(text)
        if not (self.token and self.chat_id):
            log.info("NOTIFY: %s", text)
            return
        try:
            httpx.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
        except httpx.HTTPError as e:  # never let a notification failure break a live call
            log.warning("Telegram notification failed: %s", e)
