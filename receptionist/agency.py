"""The agency's data (listings, open homes, inspection rules) and the scheduling logic.

Everything the receptionist says about a property or a time comes from here, so the
voice model never has to invent or calculate a date.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class Slot:
    listing_id: str
    start: datetime
    end: datetime
    kind: str  # "open_home" (anyone can come) or "private" (one booking per slot)

    @property
    def key(self) -> str:
        return self.start.strftime("%Y-%m-%dT%H:%M")


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def spoken(dt: datetime) -> str:
    """'Saturday 17 October at 10 am' / 'at 3:30 pm' - unambiguous and easy for the voice to say."""
    fmt = "%I %p" if dt.minute == 0 else "%I:%M %p"
    hour = dt.strftime(fmt).lstrip("0").lower()
    return f"{dt.strftime('%A')} {dt.day} {dt.strftime('%B')} at {hour}"


def spoken_address(address: str) -> str:
    """'7/42 Bayview Street' -> 'unit 7 at 42 Bayview Street', the way people say it
    (text-to-speech reads '7/42' badly)."""
    return re.sub(r"^(\d+[A-Za-z]?)/(\d+)", r"unit \1 at \2", address)


_DIGITS = "zero one two three four five six seven eight nine".split()


def spoken_phone(phone: str) -> str:
    """'0491570157' -> 'zero four nine one, five seven zero, one five seven'.
    Spelled out so the voice reads it digit by digit, the way a person would, never as
    'eight hundred and eighty-seven'."""
    d = re.sub(r"\D", "", phone or "")
    if not d:
        return ""
    groups = [d[:4], d[4:7], d[7:]] if len(d) == 10 else [d[i:i + 3] for i in range(0, len(d), 3)]
    return ", ".join(" ".join(_DIGITS[int(c)] for c in g) for g in groups if g)


class Agency:
    def __init__(self, data: dict):
        self.data = data
        self.tz = ZoneInfo(data.get("timezone", "Australia/Sydney"))
        self.listings = {l["id"]: l for l in data["listings"]}

    @classmethod
    def load(cls, path: Path) -> "Agency":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # ---- listings -------------------------------------------------------------
    def find_listings(self, query: str) -> list[dict]:
        """Match by listing ID, street, suburb or a loose description ('the Chatswood one')."""
        q = (query or "").lower().strip()
        if not q:
            return []
        if q.upper() in self.listings:
            return [self.listings[q.upper()]]
        words = [w for w in re.findall(r"[a-z0-9]+", q) if len(w) > 2 and w not in {"the", "one", "street", "road", "avenue", "property", "house", "apartment", "unit", "for", "and"}]
        scored = []
        for l in self.listings.values():
            hay = f"{l['id']} {l['address']} {l['suburb']} {l['kind']}".lower()
            score = sum(1 for w in words if w in hay)
            if score:
                scored.append((score, l))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return []
        best = scored[0][0]
        return [l for s, l in scored if s == best]

    def describe(self, l: dict) -> str:
        kind = "for sale" if l["kind"] == "sale" else "for rent"
        # Give the model the words to say, not digits: text-to-speech reads "$1,550,000" badly.
        price = f"Price: {l.get('price_spoken') or l['price']}."
        parts = [
            f"{spoken_address(l['address'])} ({l['id']}) is {kind}: {l['bedrooms']} bedrooms, {l['bathrooms']} bathrooms, "
            f"{l['parking']} car space{'s' if l['parking'] != 1 else ''}.",
            price,
            l["summary"],
        ]
        if l.get("strata_per_quarter"):
            parts.append(f"Strata levies are {l['strata_per_quarter']} per quarter.")
        parts.append(f"The agent is {l['agent']}.")
        return " ".join(parts)

    # ---- times ----------------------------------------------------------------
    def open_homes(self, listing_id: str, now: datetime, days_ahead: int = 14) -> list[Slot]:
        l = self.listings[listing_id]
        out = []
        for d in range(days_ahead + 1):
            day = (now + timedelta(days=d)).date()
            for oh in l.get("open_homes", []):
                if DAYS[day.weekday()] == oh["day"]:
                    start = datetime.combine(day, _hm(oh["start"]), self.tz)
                    if start > now:
                        out.append(Slot(listing_id, start, datetime.combine(day, _hm(oh["end"]), self.tz), "open_home"))
        return sorted(out, key=lambda s: s.start)

    def private_slots(self, listing_id: str, now: datetime) -> list[Slot]:
        rules = self.data["private_inspections"]
        step = timedelta(minutes=rules["slot_minutes"])
        earliest = now + timedelta(hours=rules["min_notice_hours"])
        open_starts = {s.start for s in self.open_homes(listing_id, now, rules["days_ahead"])}
        out = []
        for d in range(rules["days_ahead"] + 1):
            day: date = (now + timedelta(days=d)).date()
            if DAYS[day.weekday()] not in rules["days"]:
                continue
            t = datetime.combine(day, _hm(rules["start"]), self.tz)
            end = datetime.combine(day, _hm(rules["end"]), self.tz)
            while t + step <= end:
                if t >= earliest and t not in open_starts:
                    out.append(Slot(listing_id, t, t + step, "private"))
                t += step
        return out

    def parse_slot(self, listing_id: str, when: str, now: datetime) -> Slot | None:
        """Turn '2026-10-17T10:00' (what find_inspection_times hands the model) into a valid slot, or None."""
        try:
            start = datetime.fromisoformat(when.strip().replace(" ", "T")[:16]).replace(tzinfo=self.tz)
        except ValueError:
            return None
        for s in self.open_homes(listing_id, now, self.data["private_inspections"]["days_ahead"]):
            if s.start == start:
                return s
        for s in self.private_slots(listing_id, now):
            if s.start == start:
                return s
        return None
