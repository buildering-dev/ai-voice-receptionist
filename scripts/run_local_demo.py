"""Run the whole demo from one machine: server + free Cloudflare tunnel + assistant update.

    set VAPI_API_KEY=...            (your private key; set it once as a user environment variable)
    python scripts/run_local_demo.py

What it does:
  1. starts the webhook server on http://localhost:8000
  2. opens a free Cloudflare "quick tunnel" so Vapi can reach it (needs cloudflared on PATH
     or cloudflared.exe in the project folder; no Cloudflare account needed)
  3. creates/updates the assistant in your Vapi account with the tunnel's address
Then talk to it from the Vapi dashboard ("Talk to Assistant"). Ctrl+C stops everything.
The tunnel address changes every run; step 3 keeps the assistant pointed at the current one.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.getenv("PORT", "8000"))


def find_cloudflared() -> str | None:
    for name in ("cloudflared.exe", "cloudflared"):
        local = ROOT / name
        if local.exists():
            return str(local)
    return shutil.which("cloudflared")


def main() -> int:
    if not os.getenv("VAPI_API_KEY"):
        print("VAPI_API_KEY is not set. Add it as a user environment variable, open a NEW terminal, and run again.")
        return 1
    cf = find_cloudflared()
    if not cf:
        print("cloudflared not found. Put cloudflared.exe in this folder (see the setup notes) and run again.")
        return 1

    env = dict(os.environ)
    env["WEBHOOK_SECRET"] = env.get("WEBHOOK_SECRET") or secrets.token_urlsafe(24)
    env["ADMIN_TOKEN"] = env.get("ADMIN_TOKEN") or secrets.token_urlsafe(12)

    print("Starting the receptionist server...")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "receptionist.server:app", "--port", str(PORT)],
        cwd=ROOT, env=env,
    )
    time.sleep(3)

    print("Opening a Cloudflare quick tunnel...")
    tunnel = subprocess.Popen(
        [cf, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT,
    )
    url = None
    deadline = time.time() + 60
    while time.time() < deadline and url is None:
        line = tunnel.stdout.readline()
        if not line:
            time.sleep(0.2)
            continue
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
    if not url:
        print("Couldn't get a tunnel address from cloudflared. Is the internet connection OK?")
        server.terminate(); tunnel.terminate()
        return 1
    threading.Thread(target=lambda: [None for _ in tunnel.stdout], daemon=True).start()  # keep the pipe drained

    env["SERVER_URL"] = url
    print(f"Tunnel: {url}  (waiting a few seconds for it to go live)")
    time.sleep(8)
    push = subprocess.run([sys.executable, "scripts/push_assistant.py"], cwd=ROOT, env=env)
    if push.returncode != 0:
        print()
        print("NOT READY: updating the assistant in Vapi failed (see the message above), so calls would go to an")
        print("old address. Copy this window's text to Claude. Press Ctrl+C to close.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            server.terminate(); tunnel.terminate()
            return 1
    print()
    if env.get("VAPI_PUBLIC_KEY"):
        print(f"READY. Open http://localhost:{PORT} in your browser and click 'Start call' (always uses the latest settings).")
    else:
        print("READY. In the Vapi dashboard press F5 to REFRESH the page first (it caches the old tunnel address),")
        print("       then open Assistants > 'Harbourside Realty receptionist' and click 'Talk to Assistant'.")
        print("       Tip: set VAPI_PUBLIC_KEY to talk from http://localhost:%d instead (no refresh needed)." % PORT)
    print(f"Bookings, messages and call summaries:  http://localhost:{PORT}/admin?token={env['ADMIN_TOKEN']}")
    print("Keep this window open while testing. Press Ctrl+C to stop.")
    try:
        while server.poll() is None and tunnel.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.terminate(); tunnel.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
