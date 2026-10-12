"""Replay a scripted call against a running server, exactly as Vapi would send it.

    uvicorn receptionist.server:app --port 8000      # in one terminal
    python scripts/simulate_call.py                  # in another

Useful to check your data and webhook before paying for a single real call minute.
"""
from __future__ import annotations

import json
import os
import re
import sys

import httpx

URL = os.getenv("SERVER", "http://localhost:8000") + "/vapi/webhook"
HEADERS = {"X-Vapi-Secret": os.getenv("WEBHOOK_SECRET", "")}
CALL = {"id": "sim-001", "customer": {"number": "+61491570156"}}


def tool(name: str, args: dict) -> str:
    msg = {"message": {"type": "tool-calls", "call": CALL, "toolCallList": [
        {"id": f"tc-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}
    r = httpx.post(URL, json=msg, headers=HEADERS, timeout=10)
    r.raise_for_status()
    return r.json()["results"][0]["result"]


def say(who: str, text: str) -> None:
    print(f"{who:8}{text}\n" if who == "Tool:" else f"{who:8}{text}")


def main() -> int:
    say("Caller:", "What's the price guide for the Lavender Bay apartment?")
    say("Tool:", tool("get_listing_details", {"query": "Lavender Bay apartment"}))
    say("Caller:", "Could I come by on Friday?")
    times = tool("find_inspection_times", {"listing": "Lavender Bay", "day": "Friday"})
    say("Tool:", times)
    code = re.findall(r"\[(\d{4}-\d\d-\d\dT\d\d:\d\d)\]", times)
    if not code:
        print("No times found.")
        return 1
    say("Caller:", "The first one works. I'm Jordan Lee.")
    say("Tool:", tool("book_inspection", {"listing": "HR-101", "time_code": code[0], "name": "Jordan Lee"}))
    say("Caller:", "Yes, the number I'm calling from is fine.")
    say("Tool:", tool("book_inspection", {"listing": "HR-101", "time_code": code[0], "phone_confirmed": True}))
    say("Caller:", "Also, could Sophie do a free appraisal of my place? It's 5 Rose Street, Mosman.")
    say("Tool:", tool("take_message", {"reason": "Wants a free appraisal.", "property_address": "5 Rose Street, Mosman"}))
    say("Caller:", "And I'd like to put an offer in on the Willoughby house.")
    say("Tool:", tool("take_message", {"reason": "Wants to make an offer on 18 Kendall Road, Willoughby.", "urgent": True}))
    report = {"message": {"type": "end-of-call-report", "call": CALL, "endedReason": "assistant-said-end-call-phrase",
                          "analysis": {"summary": "Jordan booked a private inspection at Lavender Bay, asked for an appraisal in Mosman "
                                                  "and wants to make an offer on the Willoughby house.",
                                       "structuredData": {"needs_callback": True, "urgent": True}},
                          "artifact": {"transcript": "(simulated)"}}}
    httpx.post(URL, json=report, headers=HEADERS, timeout=10).raise_for_status()
    print("End-of-call report sent. See /admin?token=YOUR_ADMIN_TOKEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
