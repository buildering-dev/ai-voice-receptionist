"""The four things the receptionist can do during a call.

Each handler takes the arguments the voice model chose and returns a short plain-English
result that the model reads back to the caller. Handlers validate everything themselves:
the model can ask for a time that doesn't exist, but it can't book one.

The tools also remember the caller within a call (name, mobile, email), so the assistant
never asks for the same details twice, and they recognise repeat callers by phone number.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Callable

from .agency import Agency, spoken, spoken_address, spoken_phone
from .notify import Notifier
from .store import Store


def _clean_phone(p: str | None) -> str:
    digits = re.sub(r"[^\d+]", "", p or "")
    if digits.startswith("+61"):
        digits = "0" + digits[3:]
    return digits if len(re.sub(r"\D", "", digits)) >= 8 else ""


def _clean_email(e: str | None) -> str:
    e = (e or "").strip().replace(" at ", "@").replace(" dot ", ".").replace(" ", "")
    return e if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", e) else ""


def _clock(dt: datetime) -> str:
    return dt.strftime("%I %p" if dt.minute == 0 else "%I:%M %p").lstrip("0").lower()


def _ranges(slots: list) -> str:
    """Half-hour slots as runs: 'every half hour from 10 am [code] to 4:30 pm [code]'."""
    runs, cur = [], [slots[0]]
    for s in slots[1:]:
        if s.start == cur[-1].end:
            cur.append(s)
        else:
            runs.append(cur); cur = [s]
    runs.append(cur)
    out = []
    for r in runs:
        if len(r) == 1:
            out.append(f"at {_clock(r[0].start)} [{r[0].key}]")
        else:
            out.append(f"every half hour from {_clock(r[0].start)} [{r[0].key}] to {_clock(r[-1].start)} [{r[-1].key}]")
    return "; ".join(out) + " (codes for times in between follow the same pattern)" if any(len(r) > 2 for r in runs) else "; ".join(out)


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]


def _parse_day(text: str, today: date) -> date | None:
    """'tomorrow', 'Tuesday', 'next tue', '2026-10-13', '13 October', 'the 13th'."""
    t = (text or "").lower().strip()
    if not t:
        return None
    if "today" in t or "tonight" in t:
        return today
    if "tomorrow" in t:
        return today + timedelta(days=1)
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name[:3]}", t):
            ahead = (i - today.weekday()) % 7
            if ahead == 0 and "next" in t:
                ahead = 7
            return today + timedelta(days=ahead)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        month = next((i + 1 for i, mn in enumerate(_MONTHS) if mn[:3] in t), None)
        d = int(m[1])
        for k in range(0, 3):
            mo = (month or today.month + k - 1) if month else ((today.month - 1 + k) % 12) + 1
            yr = today.year + (1 if mo < today.month else 0)
            try:
                cand = date(yr, mo, d)
            except ValueError:
                continue
            if cand >= today:
                return cand
            if month:
                break
    return None


def _rule_text(rules: dict) -> str:
    days = rules["days"]
    span = f"{_WEEKDAYS[['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].index(days[0])].title()} to {_WEEKDAYS[['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].index(days[-1])].title()}"
    st = datetime.strptime(rules["start"], "%H:%M"); en = datetime.strptime(rules["end"], "%H:%M")
    return (f"{span}, {_clock(st)} to {_clock(en)}, {rules['slot_minutes']} minutes each, "
            f"booked at least {rules['min_notice_hours']} hours ahead and up to {rules['days_ahead']} days out")


def _why_none(wanted: date, now: datetime, rules: dict, listing: dict, agency, free: list) -> str:
    if wanted < now.date():
        return "that day has passed."
    if (wanted - now.date()).days > rules["days_ahead"]:
        return f"we only book {rules['days_ahead']} days ahead; the agent can arrange later dates, so offer to take a message."
    if wanted.strftime("%a") not in rules["days"]:
        return "private inspections only run on weekdays, and there's no open home that day."
    st = datetime.combine(wanted, datetime.strptime(rules["end"], "%H:%M").time(), agency.tz)
    if st - timedelta(minutes=rules["slot_minutes"]) < now + timedelta(hours=rules["min_notice_hours"]):
        return f"private inspections need {rules['min_notice_hours']} hours' notice, so that day is too soon."
    return "that day is fully booked."


class Tools:
    def __init__(self, agency: Agency, store: Store, notifier: Notifier, clock: Callable[[], datetime] | None = None):
        self.agency = agency
        self.store = store
        self.notifier = notifier
        self.clock = clock or (lambda: datetime.now(agency.tz))
        self.memory: dict[str, dict] = {}  # call_id -> caller details given so far in that call

    # name -> handler; names must match the tool definitions in assistant.py
    def handlers(self) -> dict[str, Callable[..., str]]:
        return {
            "get_listing_details": self.get_listing_details,
            "find_inspection_times": self.find_inspection_times,
            "book_inspection": self.book_inspection,
            "cancel_inspection": self.cancel_inspection,
            "take_message": self.take_message,
        }

    def call(self, name: str, args: dict, ctx: dict | None = None) -> str:
        ctx = ctx or {}
        fn = self.handlers().get(name)
        if fn is None:
            return f"Unknown tool {name}. Offer to take a message instead."
        mem = self._memory(ctx)
        try:
            args = {k: v for k, v in (args or {}).items() if v not in (None, "")}
            for flag in ("phone_confirmed", "urgent"):
                if flag in args or (flag == "phone_confirmed" and name in ("book_inspection", "take_message")):
                    args[flag] = args.get(flag) in (True, "true", "True", "yes")
            result = fn(**args, ctx=ctx)
        except TypeError as e:
            result = f"Missing or invalid details ({e}). Ask the caller for them and try again."
        return result + self._caller_note(ctx, mem)

    # ---- memory -------------------------------------------------------------------
    def _memory(self, ctx: dict) -> dict:
        key = ctx.get("call_id") or "no-call-id"
        mem = self.memory.setdefault(key, {})
        if ctx.get("caller_number") and not mem.get("caller_id"):
            mem["caller_id"] = _clean_phone(ctx["caller_number"])
        return mem

    def _remember(self, ctx: dict, name: str = "", phone: str = "", email: str = "") -> dict:
        mem = self._memory(ctx)
        if name and name.strip():
            mem["name"] = name.strip()
        if _clean_phone(phone):
            mem["phone"] = _clean_phone(phone)
        if _clean_email(email):
            mem["email"] = _clean_email(email)
        return mem

    def _confirmed_phone(self, mem: dict, phone_confirmed: bool) -> tuple[str, str]:
        """Returns (phone, "") once the number is confirmed, or ("", what to ask) if not yet.
        A person at a front desk reads the number back, or asks if the caller ID is fine."""
        phone = mem.get("phone", "")
        if not phone and mem.get("caller_id"):
            if phone_confirmed:
                mem["phone"] = mem["phone_ok"] = mem["caller_id"]
                return mem["phone"], ""
            tail = spoken_phone(mem["caller_id"]).split(", ")[-1]
            return "", (f"Don't ask them to read out a number. They're calling from {spoken_phone(mem['caller_id'])}. Ask: "
                        f"\"Is the number you're calling from, ending in {tail}, the best one to reach you on?\" "
                        "If yes, call again with phone_confirmed true; if not, take the new number and read it back.")
        if not phone:
            return "", "I need the caller's mobile number. Ask for it, read it back digit by digit, then try again."
        if phone_confirmed:
            mem["phone_ok"] = phone
        if mem.get("phone_ok") != phone:
            return "", (f"Read the mobile back first and wait for a yes: \"Just to confirm, that's {spoken_phone(phone)}?\" "
                        "Then call again with phone_confirmed true.")
        return phone, ""

    def _caller_note(self, ctx: dict, mem: dict) -> str:
        notes = []
        lookup = mem.get("phone") or mem.get("caller_id")
        if lookup and not mem.get("history_checked"):
            mem["history_checked"] = True
            past = self.store.caller_history(lookup, ctx.get("call_id"))
            if past:
                mem.setdefault("name", past["name"])
                seen = []
                if past["booking"]:
                    b = past["booking"]
                    l = self.agency.listings.get(b["listing_id"], {})
                    seen.append(f"booked an inspection at {spoken_address(l.get('address', b['listing_id']))}")
                if past["message"]:
                    seen.append(f"left a message ({past['message']['reason']})")
                notes.append(f" Returning caller: this number belongs to {past['name']}, who previously {' and '.join(seen)}."
                             " Greet them by name and ask if they're calling about that.")
        phone = mem.get("phone")
        phone_txt = phone and f"mobile {spoken_phone(phone)}" + (" (confirmed)" if mem.get("phone_ok") == phone else " (not read back yet)")
        known = [v for v in (mem.get("name"), phone_txt, mem.get("email") and f"email {mem['email']}") if v]
        if known:
            notes.append(f" Caller details already given: {', '.join(known)}. Don't ask for these again.")
        elif mem.get("caller_id") and not mem.get("caller_id_noted"):
            mem["caller_id_noted"] = True
            notes.append(f" Caller ID: {spoken_phone(mem['caller_id'])}. When you need their number, ask if this one is fine.")
        return "".join(notes)

    # ---- handlers -----------------------------------------------------------------
    def get_listing_details(self, query: str, ctx: dict) -> str:
        found = self.agency.find_listings(query)
        if not found:
            names = "; ".join(f"{spoken_address(l['address'])} ({l['kind']})" for l in self.agency.listings.values())
            return f"No listing matches '{query}'. Current listings: {names}. Ask which one they mean."
        if len(found) > 1:
            return "More than one listing matches: " + "; ".join(f"{spoken_address(l['address'])} ({l['id']}, {l['kind']})" for l in found) + ". Ask which one."
        return self.agency.describe(found[0])

    def find_inspection_times(self, listing: str, day: str = "", ctx: dict = None) -> str:
        found = self.agency.find_listings(listing)
        if len(found) != 1:
            return self.get_listing_details(listing, ctx)
        l = found[0]
        now = self.clock()
        rules = self.agency.data["private_inspections"]
        opens = self.agency.open_homes(l["id"], now)
        privates = [s for s in self.agency.private_slots(l["id"], now) if not self.store.private_slot_taken(l["id"], s.key)]
        head = (f"Times for {spoken_address(l['address'])} ({l['id']}). Booking codes are in brackets; don't read them aloud. "
                f"Private inspections: {_rule_text(rules)}.")
        wanted = _parse_day(day, now.date()) if day else None
        if day and wanted is None:
            return head + f" I couldn't work out which day '{day}' means; ask the caller for the day."
        if wanted is not None:
            o = [s for s in opens if s.start.date() == wanted]
            p_ = [s for s in privates if s.start.date() == wanted]
            label = f"{wanted.strftime('%A')} {wanted.day} {wanted.strftime('%B')}"
            if o or p_:
                lines = [head, f"On {label}:"]
                if o:
                    lines.append("open home " + "; ".join(f"at {_clock(s.start)} [{s.key}]" for s in o) + ".")
                if p_:
                    lines.append("private inspections " + _ranges(p_) + ".")
                return " ".join(lines)
            why = _why_none(wanted, now, rules, l, self.agency, privates)
            nxt = [s for s in privates if s.start.date() > wanted][:1] + [s for s in opens if s.start.date() > wanted][:1]
            alt = "; ".join(f"{spoken(s.start)} [{s.key}] ({'open home' if s.kind == 'open_home' else 'private'})" for s in sorted(nxt, key=lambda s: s.start))
            return f"{head} Nothing free on {label}: {why}" + (f" Nearest options: {alt}." if alt else ("" if "message" in why else " Offer to take a message for the agent."))
        lines = [head]
        if opens:
            lines.append("Open homes, anyone can come: " + "; ".join(f"{spoken(s.start)} [{s.key}]" for s in opens[:3]) + ".")
        if privates:
            days = sorted({s.start.date() for s in privates})[:2]
            lines.append("Private inspections available: " + " ".join(
                f"{d.strftime('%A')} {d.day} {d.strftime('%B')}: {_ranges([s for s in privates if s.start.date() == d])}." for d in days)
                + " Other weekdays in the next two weeks too; if they want a particular day, call this again with day.")
        if not opens and not privates:
            lines.append("No times are available in the next two weeks. Offer to take a message for the agent.")
        return " ".join(lines)

    def book_inspection(self, listing: str, time_code: str, name: str = "", phone: str = "", email: str = "",
                        phone_confirmed: bool = False, ctx: dict = None) -> str:
        ctx = ctx or {}
        mem = self._remember(ctx, name, phone, email)
        found = self.agency.find_listings(listing)
        if len(found) != 1:
            return "I couldn't tell which property that is. Confirm the address and try again."
        l = found[0]
        name, email = mem.get("name", ""), mem.get("email", "")
        if not name:
            return "I need the caller's full name before booking. Ask for it and try again."
        now = self.clock()
        slot = self.agency.parse_slot(l["id"], time_code, now)
        if slot is None:
            return "That time isn't available. Use find_inspection_times and offer one of those times."
        phone, ask = self._confirmed_phone(mem, phone_confirmed)
        if ask:
            return "Not booked yet. " + ask
        if self.store.existing_booking(l["id"], slot.key, phone):
            return f"{name} is already booked for {spoken(slot.start)} (from an earlier call). Tell them they're already on the list."
        if slot.kind == "private" and self.store.private_slot_taken(l["id"], slot.key):
            return "Someone just took that private slot. Use find_inspection_times and offer another time."
        code = self.store.add_booking(l["id"], slot.key, slot.kind, name, phone, email or None, ctx.get("call_id"))
        what = "open home" if slot.kind == "open_home" else "private inspection"
        self.notifier.send(f"New {what}: {name} ({phone}{', ' + email if email else ''}) for {l['address']} on {spoken(slot.start)}. Booking {code}.")
        follow = f" A confirmation will go to {email}." if email else " Tell the caller a text confirmation follows."
        return (f"Booked: {what} at {spoken_address(l['address'])} on {spoken(slot.start)} for {name}, mobile {spoken_phone(phone)}. "
                f"Confirmation code {code} (only read it out if asked). {l['agent']} will be there.{follow}")

    def cancel_inspection(self, listing: str = "", time_code: str = "", phone: str = "", ctx: dict = None) -> str:
        ctx = ctx or {}
        mem = self._remember(ctx, phone=phone)
        number = mem.get("phone") or mem.get("caller_id")
        if not number:
            return "Ask for the mobile number they booked with, read it back, then try again with phone."
        listing_id = None
        if listing:
            found = self.agency.find_listings(listing)
            if len(found) == 1:
                listing_id = found[0]["id"]
        mine = self.store.bookings_for_phone(number, listing_id)
        if time_code:
            mine = [b for b in mine if b["slot_start"] == time_code.strip()[:16]] or mine
        if not mine:
            return ("There's no booking under that number" + (" for that property" if listing_id else "") +
                    ". Check the property and the number they booked with; if it still can't be found, take a message for the agent.")
        if len(mine) > 1:
            opts = "; ".join(f"{spoken_address(self.agency.listings[b['listing_id']]['address'])} on "
                             f"{spoken(datetime.fromisoformat(b['slot_start']))} [{b['slot_start']}]" for b in mine)
            return f"They have more than one booking: {opts}. Ask which one to cancel, then call again with time_code."
        b = mine[0]
        l = self.agency.listings[b["listing_id"]]
        when = spoken(datetime.fromisoformat(b["slot_start"]))
        self.store.cancel_booking(b["code"])
        self.notifier.send(f"Cancelled: {b['name']} ({b['phone']}) is no longer coming to {l['address']} on {when}. Booking {b['code']}.")
        return f"Cancelled: {b['name']}'s inspection at {spoken_address(l['address'])} on {when}. {l['agent']} has been told."

    def take_message(self, reason: str = "", name: str = "", phone: str = "", for_agent: str = "",
                     property_address: str = "", phone_confirmed: bool = False, urgent: bool = False, ctx: dict = None) -> str:
        ctx = ctx or {}
        mem = self._remember(ctx, name, phone)
        name = mem.get("name", "")
        if not reason:
            return "Ask briefly what the call is about, then take the message."
        if not name:
            return "I need the caller's name for the message. Ask for it and try again."
        new_appraisal = re.search(r"apprais|valu|sell", reason, re.I) and not re.search(r"cancel|withdraw|no longer|not needed|call off", reason, re.I)
        if new_appraisal and not property_address:
            return "For an appraisal, ask for the property address first, then take the message again with property_address."
        phone, ask = self._confirmed_phone(mem, phone_confirmed)
        if ask:
            return "Message not saved yet. " + ask
        addr = property_address.strip()
        full = reason.strip() + (f" Property: {addr}." if addr and addr.lower()[:12] not in reason.lower() else "")
        line = self.agency.data.get("line_agent")
        who = for_agent or line or "the right agent"
        if urgent:
            full = "URGENT: " + full
        self.store.add_message(name, phone, full, who if who != "the right agent" else None, ctx.get("call_id"))
        self.notifier.send(f"{'URGENT ' if urgent else ''}Call back {name} on {phone} (for {who}): {full}")
        when = f"as soon as {line.split()[0]}'s free" if line else "within business hours"
        if line and who != line:
            when = f"from {who.split()[0]} soon ({line.split()[0]} will make sure of it)"
        return f"Message saved for {who}{' and flagged urgent' if urgent else ''}. Tell the caller they'll get a call back {when}."
