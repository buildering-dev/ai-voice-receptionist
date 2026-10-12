"""Settings, read from environment variables (or a .env file loaded by your process manager)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    agency_file: Path = field(default_factory=lambda: Path(os.getenv("AGENCY_FILE", ROOT / "data" / "agency.json")))
    prompt_file: Path = field(default_factory=lambda: Path(os.getenv("PROMPT_FILE", ROOT / "prompts" / "receptionist.md")))
    db_path: Path = field(default_factory=lambda: Path(os.getenv("DB_PATH", ROOT / "data" / "receptionist.db")))

    # Public URL of this server, e.g. https://voice.example.com (Vapi sends webhooks here).
    server_url: str = field(default_factory=lambda: os.getenv("SERVER_URL", "").rstrip("/"))
    # Shared secret Vapi sends in the X-Vapi-Secret header. Leave empty only for local testing.
    webhook_secret: str = field(default_factory=lambda: os.getenv("WEBHOOK_SECRET", ""))
    # Protects the /admin pages. Leave empty to disable them.
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""))

    # For the browser demo page only. The Vapi PUBLIC key is designed to be used in web pages;
    # never put the private API key here.
    vapi_public_key: str = field(default_factory=lambda: os.getenv("VAPI_PUBLIC_KEY", ""))
    vapi_assistant_id: str = field(default_factory=lambda: os.getenv("VAPI_ASSISTANT_ID", ""))

    # Optional notifications to the agency (new bookings, messages, call summaries).
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # Model and voice used when the assistant is created with scripts/push_assistant.py.
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "openai"))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4.1"))
    voice_provider: str = field(default_factory=lambda: os.getenv("VOICE_PROVIDER", "cartesia"))
    # Cartesia "Australian Narrator Lady". Alternatives: "Australian Woman" 043cfc81-d69f-4bee-ae1e-7862cb358650,
    # "Australian Customer Support Man" 41f3c367-e0a8-4a85-89e0-c27bae9c9b6d, or VOICE_PROVIDER=vapi VOICE_ID=Clara.
    voice_id: str = field(default_factory=lambda: os.getenv("VOICE_ID", "8985388c-1332-4ce7-8d55-789628aa3df4"))
    voice_model: str = field(default_factory=lambda: os.getenv("VOICE_MODEL", "sonic-3"))
    max_call_seconds: int = field(default_factory=lambda: int(os.getenv("MAX_CALL_SECONDS", "600")))
