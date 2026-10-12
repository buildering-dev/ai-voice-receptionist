"""Webhook server Vapi calls during and after every phone call.

    uvicorn receptionist.server:app --port 8000

POST /vapi/webhook   tool calls (live, during the call) and end-of-call reports
GET  /               browser demo: talk to the receptionist from your laptop
GET  /admin          bookings, messages and call summaries (needs ADMIN_TOKEN)
GET  /health
"""
from __future__ import annotations

import html
import hmac
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .admin import render_admin
from .agency import Agency
from .assistant import first_message
from .config import ROOT, Settings
from .notify import Notifier
from .store import Store
from .tools import Tools

log = logging.getLogger("receptionist")


def _tool_calls(message: dict) -> list[tuple[str, str, dict]]:
    """Return (id, name, args) for every tool call, whichever payload shape Vapi sent."""
    out = []
    for tc in message.get("toolCallList") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments", tc.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except json.JSONDecodeError:
                args = {}
        out.append((tc.get("id", ""), fn.get("name") or tc.get("name", ""), args or {}))
    if not out:  # older shape
        for item in message.get("toolWithToolCallList") or []:
            tc = item.get("toolCall") or {}
            fn = tc.get("function") or {}
            args = fn.get("arguments", tc.get("parameters", {}))
            if isinstance(args, str):
                args = json.loads(args or "{}")
            out.append((tc.get("id", ""), fn.get("name") or item.get("name", ""), args or {}))
    return out


def clean_transcript(artifact: dict) -> str:
    """The call as the assistant actually wrote it. Vapi's own transcript is speech-to-text of
    both voices, which garbles the assistant's lines ("one point four five million" -> "1.45",
    "Harbourside" -> "Harborside"). Falls back to that transcript if the messages are missing."""
    lines = []
    for m in artifact.get("messagesOpenAIFormatted") or []:
        text = m.get("content")
        if m.get("role") in ("user", "assistant") and isinstance(text, str) and text.strip():
            lines.append(f"{'AI' if m['role'] == 'assistant' else 'User'}: {text.strip()}")
    return "\n".join(lines) or artifact.get("transcript") or ""


def create_app(settings: Settings | None = None, tools: Tools | None = None) -> FastAPI:
    settings = settings or Settings()
    agency = Agency.load(settings.agency_file)
    store = Store(settings.db_path)
    notifier = Notifier(settings.telegram_bot_token, settings.telegram_chat_id)
    tools = tools or Tools(agency, store, notifier)
    app = FastAPI(title="AI Voice Receptionist")
    app.state.tools, app.state.store, app.state.notifier = tools, tools.store, tools.notifier

    def check_secret(request: Request) -> None:
        if not settings.webhook_secret:
            return
        got = request.headers.get("x-vapi-secret") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(got, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="bad secret")

    @app.get("/health")
    def health():
        return {"ok": True, "agency": agency.data["name"], "listings": len(agency.listings)}

    @app.post("/vapi/webhook")
    async def webhook(request: Request):
        check_secret(request)
        body = await request.json()
        message = body.get("message", body)
        kind = message.get("type")
        call = message.get("call") or {}
        caller = (call.get("customer") or message.get("customer") or {}).get("number")
        # The browser demo has no caller ID, so it can pretend with a "caller_id" variable.
        caller = caller or ((call.get("assistantOverrides") or {}).get("variableValues") or {}).get("caller_id")
        ctx = {"call_id": call.get("id"), "caller_number": caller}

        if kind == "tool-calls":
            results = []
            for call_id, name, args in _tool_calls(message):
                result = tools.call(name, args, ctx)
                log.info("tool %s %s -> %s", name, args, result[:120])
                results.append({"name": name, "toolCallId": call_id, "result": result})
            return JSONResponse({"results": results})

        if kind == "end-of-call-report":
            analysis = message.get("analysis") or {}
            artifact = message.get("artifact") or {}
            summary = analysis.get("summary") or ""
            try:  # keep the last raw report next to the database, for checking what Vapi sent
                (Path(settings.db_path).parent / "last_call_report.json").write_text(json.dumps({"artifact": artifact, "analysis": analysis}, indent=1)[:2_000_000])
            except OSError:
                pass
            tools.store.save_call(
                ctx["call_id"] or f"local-{len(tools.store.calls()) + 1}",
                ctx["caller_number"],
                message.get("endedReason"),
                summary,
                analysis.get("structuredData") or {},
                clean_transcript(artifact),
                artifact.get("recordingUrl") or (artifact.get("recording") or {}).get("mono", {}).get("combinedUrl"),
                message.get("cost"),
            )
            # Safety net: if the call needed a call-back but nothing was saved (the AI said "I'll let Sophie
            # know" without doing it), save one from the call summary so the agent still hears about it.
            structured = analysis.get("structuredData") or {}
            saved = tools.store.messages_for_call(ctx["call_id"]) if ctx["call_id"] else []
            missed_urgent = structured.get("urgent") and not any((m["reason"] or "").startswith("URGENT") for m in saved)
            if ctx["call_id"] and ((structured.get("needs_callback") and not saved) or missed_urgent):
                mem = tools.memory.get(ctx["call_id"], {})
                urgent = bool(structured.get("urgent"))
                reason = (structured.get("callback_reason") or summary or "Call back requested.").strip()
                reason = ("URGENT: " if urgent else "") + reason + " (saved from the call summary; check the recording)"
                who = agency.data.get("line_agent")
                name = mem.get("name") or structured.get("caller_name") or "Unknown caller"
                phone = mem.get("phone") or mem.get("caller_id") or ctx["caller_number"] or "unknown"
                tools.store.add_message(name, phone, reason, who, ctx["call_id"])
                tools.notifier.send(f"{'URGENT ' if urgent else ''}Call back {name} on {phone}: {reason}")
            tools.memory.pop(ctx["call_id"] or "no-call-id", None)  # the call is over; forget its scratch memory
            if summary:
                tools.notifier.send(f"Call summary ({ctx['caller_number'] or 'web call'}): {summary}")
            return {"ok": True}

        return {"ok": True, "ignored": kind}

    @app.get("/", response_class=HTMLResponse)
    def demo():
        page = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        assistant_id = settings.vapi_assistant_id
        id_file = ROOT / ".assistant_id"  # written by scripts/push_assistant.py
        if not assistant_id and id_file.exists():
            assistant_id = id_file.read_text().strip()
        return page.replace("__VAPI_PUBLIC_KEY__", html.escape(settings.vapi_public_key)).replace(
            "__VAPI_ASSISTANT_ID__", html.escape(assistant_id)
        ).replace("__AGENCY__", html.escape(agency.data["name"])).replace(
            '"__FIRST_MESSAGE__"', json.dumps(first_message(agency)).replace("<", "\\u003c")
        )

    @app.get("/admin", response_class=HTMLResponse)
    def admin(token: str = ""):
        if not settings.admin_token or not hmac.compare_digest(token, settings.admin_token):
            raise HTTPException(status_code=404)
        return render_admin(tools.store, agency)

    return app


app = create_app()
