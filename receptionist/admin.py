"""The agent's view: who to call back (urgent first), inspections booked, and every call answered."""
from __future__ import annotations

import html
import re
from datetime import datetime

from .agency import Agency
from .store import Store

STYLE = """
:root { --bg:#f6f3ee; --ink:#1c1a17; --mute:#6f6a62; --line:#e2ddd4; --accent:#1f3a34; --gold:#a8864b; --red:#8c2f22; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:17px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1100px; margin:0 auto; padding:40px 24px 64px; }
.eyebrow { font:600 12px/1 system-ui; letter-spacing:.14em; text-transform:uppercase; color:var(--gold); }
h1 { font:600 38px/1.1 Georgia,"Times New Roman",serif; margin:10px 0 24px; }
h2 { font:600 22px/1.2 Georgia,"Times New Roman",serif; margin:36px 0 12px; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
.stat { background:#fff; border:1px solid var(--line); border-radius:16px; padding:16px 18px; }
.stat b { display:block; font:600 34px/1.1 Georgia,serif; }
.stat span { color:var(--mute); font-size:14px; }
.stat.hot b { color:var(--red); }
.card { background:#fff; border:1px solid var(--line); border-radius:16px; padding:16px 18px; margin-bottom:10px; display:grid; grid-template-columns:1fr auto; gap:4px 16px; }
.card.urgent { border-left:6px solid var(--red); }
.card .who { font-weight:600; }
.card .when { color:var(--mute); font-size:14px; text-align:right; }
.card .why { grid-column:1 / -1; }
.tag { display:inline-block; font:700 11px/1 system-ui; letter-spacing:.08em; color:#fff; background:var(--red); border-radius:6px; padding:4px 6px; margin-right:8px; vertical-align:2px; }
table { width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:16px; overflow:hidden; }
th, td { text-align:left; padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:top; }
td:first-child { white-space:nowrap; }
th { font-size:13px; color:var(--mute); font-weight:600; background:#fbfaf7; }
.empty { color:var(--mute); }
.note { color:var(--mute); font-size:13px; margin-top:28px; }
"""


def _phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return f"{d[:4]} {d[4:7]} {d[7:]}" if len(d) == 10 else (p or "")


def _fmt(dt: datetime) -> str:
    """'Fri 16 Oct, 11 am' / 'Mon 12 Oct, 5:51 pm'."""
    clock = dt.strftime("%I %p" if dt.minute == 0 else "%I:%M %p").lstrip("0").lower()
    return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')}, {clock}"


def _local(ts: str, agency: Agency) -> str:
    try:
        return _fmt(datetime.fromisoformat(ts).astimezone(agency.tz))
    except (TypeError, ValueError):
        return ts or ""


def _strip_urgent(reason: str | None) -> str:
    return re.sub(r"^URGENT:\s*", "", reason or "")


def _slot(ts: str) -> str:
    try:
        return _fmt(datetime.fromisoformat(ts))
    except (TypeError, ValueError):
        return ts or ""


def render_admin(store: Store, agency: Agency, refresh_seconds: int = 10) -> str:
    e = html.escape
    calls, bookings, msgs = store.calls(), store.bookings(), store.messages()
    # A call counts as soon as it books or leaves a message; the summary arrives a minute later.
    call_ids = {c["call_id"] for c in calls} | {b["call_id"] for b in bookings if b["call_id"]} | {m["call_id"] for m in msgs if m["call_id"]}
    urgent = [m for m in msgs if (m["reason"] or "").startswith("URGENT")]
    appraisals = sum(1 for m in msgs if "apprais" in (m["reason"] or "").lower())
    stats = [("Calls answered", len(call_ids), False), ("Inspections booked", len(bookings), False),
             ("Call-backs", len(msgs), False), ("Appraisal leads", appraisals, False), ("Urgent", len(urgent), bool(urgent))]
    stat_html = "".join(f"<div class='stat{' hot' if hot else ''}'><b>{n}</b><span>{label}</span></div>" for label, n, hot in stats)

    ordered = urgent + [m for m in msgs if m not in urgent]
    cards = "".join(
        f"<div class='card{' urgent' if m in urgent else ''}'>"
        f"<div class='who'>{'<span class=tag>URGENT</span>' if m in urgent else ''}{e(m['name'])} · {e(_phone(m['phone']))}</div>"
        f"<div class='when'>{e(_local(m['created_at'], agency))}</div>"
        f"<div class='why'>{e(_strip_urgent(m['reason']))}</div></div>"
        for m in ordered
    ) or "<p class='empty'>Nothing to call back yet.</p>"

    def addr(lid: str) -> str:
        return agency.listings.get(lid, {}).get("address", lid)

    rows = "".join(
        f"<tr><td>{e(_slot(b['slot_start']))}</td><td>{e(addr(b['listing_id']))}</td>"
        f"<td>{'Open home' if b['kind'] == 'open_home' else 'Private'}</td><td>{e(b['name'])}</td><td>{e(_phone(b['phone']))}</td></tr>"
        for b in bookings
    )
    book_html = (f"<table><tr><th>When</th><th>Property</th><th>Type</th><th>Name</th><th>Mobile</th></tr>{rows}</table>"
                 if rows else "<p class='empty'>No inspections booked yet.</p>")

    summarised = {c["call_id"] for c in calls}
    pending = [cid for cid in call_ids if cid not in summarised]
    call_rows = "".join(
        f"<tr><td>{e(_local(c['created_at'], agency))}</td><td>{e(c['summary'] or '')}</td></tr>" for c in calls
    ) + "".join("<tr><td>Just now</td><td class='empty'>Summary on its way…</td></tr>" for _ in pending)
    call_html = (f"<table><tr><th>When</th><th>Summary</th></tr>{call_rows}</table>"
                 if call_rows else "<p class='empty'>No calls yet.</p>")

    agent = agency.data.get("line_agent")
    title = f"{agent.split()[0]}'s missed calls, handled" if agent else "Calls you didn't miss"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<meta http-equiv='refresh' content='{refresh_seconds}'>"
        f"<title>{e(agency.data['name'])} · calls</title><style>{STYLE}</style></head><body><main>"
        f"<div class='eyebrow'>{e(agency.data['name'])} · AI call assistant</div><h1>{e(title)}</h1>"
        f"<div class='stats'>{stat_html}</div>"
        f"<h2>Call back</h2>{cards}"
        f"<h2>Inspections booked</h2>{book_html}"
        f"<h2>Calls answered</h2>{call_html}"
        "<p class='note'>Updates automatically. Every booking and call-back is also sent to the agent's phone as it happens.</p>"
        "</main></body></html>"
    )
