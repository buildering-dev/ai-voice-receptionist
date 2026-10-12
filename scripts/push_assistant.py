"""Create or update the receptionist in your Vapi account.

    export VAPI_API_KEY=...          # your PRIVATE key (Vapi dashboard > API Keys). Never commit it.
    export SERVER_URL=https://...    # where receptionist.server is reachable from the internet
    export WEBHOOK_SECRET=...        # any long random string; the server checks it
    python scripts/push_assistant.py            # creates it, saves the id to .assistant_id
    python scripts/push_assistant.py            # run again after editing the prompt or data: updates it
    python scripts/push_assistant.py --dry-run  # print what would be sent
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from receptionist.assistant import build_assistant  # noqa: E402
from receptionist.config import ROOT, Settings  # noqa: E402

API = "https://api.vapi.ai"
ID_FILE = ROOT / ".assistant_id"


def main() -> int:
    settings = Settings()
    body = build_assistant(settings)
    if "--dry-run" in sys.argv:
        print(json.dumps(body, indent=2))
        return 0
    key = os.getenv("VAPI_API_KEY")
    if not key:
        print("Set VAPI_API_KEY (your private key) first.", file=sys.stderr)
        return 1
    if not settings.server_url:
        print("Set SERVER_URL so Vapi knows where to send tool calls.", file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bearer {key}"}
    existing = ID_FILE.read_text().strip() if ID_FILE.exists() else os.getenv("VAPI_ASSISTANT_ID", "")

    def send(b: dict):
        if existing:
            return httpx.patch(f"{API}/assistant/{existing}", headers=headers, json=b, timeout=30)
        return httpx.post(f"{API}/assistant", headers=headers, json=b, timeout=30)

    # If Vapi rejects an optional extra (voice, pronunciation fixes, turn-taking tweaks), retry without it
    # so the assistant still points at the live server, and say clearly what was dropped.
    fallbacks = [
        ("speech-recognition name hints", lambda b: b["transcriber"].pop("keyterm", None)),
        ("pronunciation replacements", lambda b: b["voice"].pop("chunkPlan", None)),
        ("custom voice (using Vapi's default voice)", lambda b: b.update(voice={"provider": "vapi", "voiceId": "Clara"})),
        ("turn-taking tweaks", lambda b: (b.pop("startSpeakingPlan", None), b.pop("stopSpeakingPlan", None))),
    ]
    try:
        r = send(body)
        dropped = []
        for label, drop in fallbacks:
            if r.status_code < 300 or r.status_code >= 500:
                break
            print(f"Vapi rejected the update ({r.status_code}): {r.text[:500]}", file=sys.stderr)
            print(f"Retrying without: {label}", file=sys.stderr)
            drop(body)
            dropped.append(label)
            r = send(body)
    except httpx.HTTPError as e:
        print(f"Couldn't reach Vapi: {e}", file=sys.stderr)
        return 1
    if r.status_code >= 300:
        print(f"Vapi returned {r.status_code}: {r.text[:800]}", file=sys.stderr)
        return 1
    if dropped:
        print("WARNING: updated, but without: " + "; ".join(dropped) + ". Send this window's text to fix it properly.")
    assistant_id = r.json()["id"]
    ID_FILE.write_text(assistant_id)
    print(f"{'Updated' if existing else 'Created'} assistant {assistant_id}")
    print("Set VAPI_ASSISTANT_ID to this id for the browser demo page.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
