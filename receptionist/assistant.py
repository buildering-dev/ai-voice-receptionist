"""Builds the Vapi assistant definition (prompt, voice, tools, call analysis) from your data.

    python -m receptionist.assistant            # print the JSON
    python scripts/push_assistant.py            # create or update it in your Vapi account
"""
from __future__ import annotations

import json
import re

from .agency import Agency, spoken_address
from .config import Settings

# Vapi fills this in at the start of every call, in the agency's time zone.
TODAY = '{{"now" | date: "%A %d %B %Y, %I:%M %p", "Australia/Sydney"}}'

TOOL_SPECS = [
    {
        "name": "get_listing_details",
        "description": "Look up one of our listings by address, suburb or listing ID and get its facts (price, bedrooms, features, agent).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What the caller said, e.g. 'Kendall Road' or 'the Chatswood apartment'."}},
            "required": ["query"],
        },
        "say": "Let me pull that up.",
    },
    {
        "name": "find_inspection_times",
        "description": "Get open homes and free private inspection times for a listing, with booking codes. If the caller asks about a particular day, pass it as day to get every free time that day, or the reason there's none and the nearest options.",
        "parameters": {
            "type": "object",
            "properties": {
                "listing": {"type": "string", "description": "Address, suburb or listing ID."},
                "day": {"type": "string", "description": "Optional. The day the caller wants, e.g. 'Tuesday', 'tomorrow', '2026-10-20'."},
            },
            "required": ["listing"],
        },
        "say": "Let me check the times.",
    },
    {
        "name": "book_inspection",
        "description": "Book the caller into an inspection. Only use a time_code returned by find_inspection_times, after reading the time back. Name, mobile and email can be left out if the caller already gave them in this call.",
        "parameters": {
            "type": "object",
            "properties": {
                "listing": {"type": "string", "description": "Address, suburb or listing ID."},
                "time_code": {"type": "string", "description": "The code in brackets from find_inspection_times, e.g. 2026-10-17T10:00."},
                "name": {"type": "string", "description": "Caller's full name."},
                "phone": {"type": "string", "description": "Caller's mobile number."},
                "email": {"type": "string", "description": "Email for the confirmation, if the caller wants one."},
                "phone_confirmed": {"type": "boolean", "description": "True only after you read the mobile back digit by digit and the caller said yes, or they agreed the number they're calling from is fine."},
            },
            "required": ["listing", "time_code"],
        },
        "slow_say": "Bear with me, just saving that.",
    },
    {
        "name": "cancel_inspection",
        "description": "Cancel an inspection the caller booked (found by their number). If they have more than one, you'll be asked which.",
        "parameters": {
            "type": "object",
            "properties": {
                "listing": {"type": "string", "description": "Address, suburb or listing ID of the booking to cancel."},
                "time_code": {"type": "string", "description": "Only if asked to pick between bookings: the code in brackets."},
                "phone": {"type": "string", "description": "Only if asked: the mobile they booked with."},
            },
            "required": [],
        },
        "slow_say": "Bear with me, just a moment.",
    },
    {
        "name": "take_message",
        "description": "Leave a message for an agent: appraisals, contracts, offers, maintenance, complaints, or anything you can't answer. If the caller already gave their name and number in this call, don't ask again; just leave name and phone out.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "One or two sentences on what they need."},
                "name": {"type": "string", "description": "Leave out if already given in this call."},
                "phone": {"type": "string", "description": "Leave out if already given in this call."},
                "phone_confirmed": {"type": "boolean", "description": "True only after you read the mobile back digit by digit and the caller said yes, or they agreed the number they're calling from is fine."},
                "property_address": {"type": "string", "description": "For appraisals or selling enquiries: the caller's property address."},
                "for_agent": {"type": "string", "description": "Agent's name if known, e.g. Sophie Tran (sales) or Marcus Webb (rentals)."},
                "urgent": {"type": "boolean", "description": "True if it can't wait: an offer, contract or finance deadline, settlement, or the caller says it's urgent."},
            },
            "required": ["reason"],
        },
        "slow_say": "Bear with me, just a moment.",
    },
]


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def tool_messages(spec: dict) -> list[dict]:
    """Lookups get a short line while they run ("Let me check the times"). Saves (book, cancel, message)
    are quick, so they stay silent and only speak up if the server is slow, which stops the same filler
    line repeating through a call."""
    msgs = []
    if spec.get("say"):
        msgs.append({"type": "request-start", "content": spec["say"]})
    if spec.get("slow_say"):
        msgs.append({"type": "request-response-delayed", "content": spec["slow_say"], "timingMilliseconds": 2500})
    return msgs


def number_words(n: int) -> str:
    """12 -> 'twelve', 42 -> 'forty-two', 105 -> 'one hundred and five' (enough for street numbers)."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else "-" + _ONES[n % 10])
    if n < 1000:
        rest = n % 100
        return _ONES[n // 100] + " hundred" + ("" if rest == 0 else " and " + number_words(rest))
    return str(n)


def address_replacements(agency: Agency) -> list[dict]:
    """Make the voice say 'unit seven at forty-two Bayview Street' however the model writes a unit address
    ('7/42', '742', '7 42', 'unit 7, 42', 'unit 7 at 42'), so a unit number never gets read as one big street number."""
    out = []
    for l in agency.listings.values():
        m = re.match(r"^(\d+)/(\d+)\s+(.+?)(,|$)", l["address"])
        if not m:
            continue
        unit, num, street = int(m.group(1)), int(m.group(2)), m.group(3)
        street_re = re.escape(street).replace("\\ ", "\\s+")
        out.append({
            "type": "regex",
            "regex": rf"\b(?:unit\s+)?{unit}\s*(?:/|,|\s+at\s+|\s)?\s*{num}\s+{street_re}",
            "options": [{"type": "ignore-case", "enabled": True}],
            "value": f"unit {number_words(unit)} at {number_words(num)} {street}",
        })
    return out


def phone_tail_replacements() -> list[dict]:
    """'ending in 045' -> 'ending in zero four five' (a leading zero would otherwise be read as 'forty-five')."""
    digits = "zero one two three four five six seven eight nine".split()
    return [{"type": "exact", "key": f"ending in 0{a}{b}", "value": f"ending in zero {digits[a]} {digits[b]}", "replaceAllEnabled": True}
            for a in range(10) for b in range(10)]


def keyterms(agency: Agency) -> list[str]:
    """Names the caller is likely to say: agency, agents, streets, suburbs."""
    terms = [agency.data["name"]] + [a["name"] for a in agency.data.get("agents", [])]
    for l in agency.listings.values():
        street_suburb = re.sub(r"^[\d/\s]+", "", l["address"])
        terms += [t.strip() for t in street_suburb.split(",") if t.strip()]
    return list(dict.fromkeys(terms))[:40]


def line_agent(agency: Agency) -> dict | None:
    """The agent whose mobile this assistant answers when they can't pick up (None = agency reception)."""
    name = agency.data.get("line_agent")
    return next((a for a in agency.data.get("agents", []) if a["name"] == name), None) if name else None


def first_message(agency: Agency) -> str:
    agent = line_agent(agency)
    if agent:
        first = agent["name"].split()[0]
        return (f"Hi, you've reached {agent['name']} at {agency.data['name']}. {first}'s tied up at the moment. "
                f"I'm Clara, {first}'s virtual assistant. How can I help?")
    return f"Thanks for calling {agency.data['name']}, this is Clara. How can I help?"


def callback_promise(agency: Agency) -> str:
    agent = line_agent(agency)
    return f"as soon as {agent['name'].split()[0]} is free" if agent else "within business hours"


def _line_section(agency: Agency) -> str:
    agent = line_agent(agency)
    if not agent:
        return ""
    first = agent["name"].split()[0]
    return f"""## Whose line this is
- You answer {agent['name']}'s mobile when {first} can't pick up (with a client, at an open home, an appraisal, a meeting). People called wanting {first}, so be the next best thing: help them on the spot where you can, and make sure {first} can call them back fast with everything needed.
- Never say where {first} is or what {first} is doing; just "tied up" or "with a client".
- Promise a call back "as soon as {first}'s free". Never say "within business hours".
- {first} is a {agent['role'].lower()}. For anything another agent handles (see the agent list), still help, and pass the message to that agent.
- Mark a message urgent when it can't wait: an offer, a contract or finance deadline, settlement, a vendor wanting to talk now, or the caller says it's urgent.
- If they ask whether you're a real person, say plainly you're {first}'s virtual assistant.

"""


def render_prompt(template: str, agency: Agency) -> str:
    d = agency.data
    listing_lines = "\n".join(
        f"- {l['id']}: {spoken_address(l['address'])}, {l['kind']}, {l['bedrooms']} bed, {l.get('price_spoken') or l['price']}. Agent {l['agent']}."
        for l in agency.listings.values()
    )
    hours = ", ".join(f"{day} {o}-{c}" for day, (o, c) in d["office"]["hours"].items())
    faq = "\n".join(f"- {f['q']} {f['a']}" for f in d.get("faq", []))
    agent = line_agent(agency)
    role_intro = (
        f"You are Clara, the virtual assistant who answers {agent['name']}'s phone at {d['name']}, a real estate agency in {d.get('city', '')}, when they can't pick up."
        if agent else f"You are Clara, the receptionist at {d['name']}, a real estate agency in {d.get('city', '')}."
    ) + " Today is {today}. Callers are buyers, renters, owners and other agents. You've worked in real estate for years: relaxed, quick, friendly, and you never make people repeat themselves."
    values = {
        "{role_intro}": role_intro,
        "{line_section}": _line_section(agency),
        "{callback_promise}": callback_promise(agency),
        "{agency_name}": d["name"],
        "{city}": d.get("city", ""),
        "{today}": TODAY,
        "{listing_lines}": listing_lines,
        "{office_line}": f"{d['office']['address']}. Open {hours}.",
        "{agent_line}": "; ".join(f"{a['name']} ({a['role']}, {a['handles']})" for a in d["agents"]),
    }
    out = template
    for k, v in values.items():
        out = out.replace(k, v)
    if faq:
        out += "\n## Common questions\n" + faq + "\n"
    return out


def build_assistant(settings: Settings, agency: Agency | None = None) -> dict:
    agency = agency or Agency.load(settings.agency_file)
    prompt = render_prompt(settings.prompt_file.read_text(encoding="utf-8"), agency)
    server = {"url": f"{settings.server_url or 'https://YOUR-SERVER'}/vapi/webhook", "timeoutSeconds": 20}
    if settings.webhook_secret:
        server["headers"] = {"X-Vapi-Secret": settings.webhook_secret}

    tools = [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]},
            "server": server,
            **({"messages": tool_messages(t)} if tool_messages(t) else {}),
        }
        for t in TOOL_SPECS
    ]

    return {
        "name": f"{agency.data['name']} receptionist",
        "firstMessage": first_message(agency),
        "firstMessageMode": "assistant-speaks-first",
        "model": {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "temperature": 0.3,
            "messages": [{"role": "system", "content": prompt}],
            "tools": tools,
        },
        "voice": {
            "provider": settings.voice_provider,
            "voiceId": settings.voice_id,
            **({"model": settings.voice_model} if settings.voice_provider == "cartesia" and settings.voice_model else {}),
            "chunkPlan": {"enabled": True, "formatPlan": {
                "enabled": True,
                # Any number over 99 is read digit by digit ("990" -> "nine nine zero", never "nine hundred and
                # ninety"). Prices are written in words, so this only affects phone digits and the like.
                "numberToDigitsCutoff": 99,
                "replacements": address_replacements(agency) + phone_tail_replacements(),
            }},
        },
        "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en", "smartFormat": True, "numerals": True,
                        "keyterm": keyterms(agency)},
        # Turn-taking: real callers pause, say "um", and read numbers in chunks. Wait longer
        # before replying, and much longer while they're giving a number, email or address.
        "startSpeakingPlan": {
            "waitSeconds": 0.6,
            "smartEndpointingPlan": {"provider": "livekit", "waitFunction": "200 + 800 * sqrt(x) + 2200 * x^3"},
            "customEndpointingRules": [
                # only when the assistant asked them to SAY a number/email/address, not for yes/no questions
                {"type": "assistant", "regex": "(what'?s|what is|can i (get|grab|have)|could i (get|grab|have)|may i have|read|spell).{0,40}(mobile|number|email|address)",
                 "regexOptions": [{"type": "ignore-case", "enabled": True}], "timeoutSeconds": 2.5},
                {"type": "customer", "regex": "\\d[\\d\\s]*$", "timeoutSeconds": 2.0},
                # caller trailed off mid-sentence ("Can you...", "and um", "it's for my...")
                {"type": "customer",
                 "regex": "\\b(and|but|so|um|uh|er|like|can you|could you|would you|i|i'm|it's|the|a|an|to|for|of|my|with|at|on|is)[\\s.,]*$",
                 "regexOptions": [{"type": "ignore-case", "enabled": True}], "timeoutSeconds": 2.5},
            ],
        },
        "stopSpeakingPlan": {"numWords": 2, "voiceSeconds": 0.3, "backoffSeconds": 1.0},
        "maxDurationSeconds": settings.max_call_seconds,
        # Hang up after Clara finishes saying goodbye. (The endCall tool cut her off mid-goodbye.)
        "endCallPhrases": ["have a great day"],
        "silenceTimeoutSeconds": 30,
        "server": server,
        "serverMessages": ["end-of-call-report"],
        # What the browser demo page receives. Assistant text comes from the model's own words
        # (conversation-update / model-output), not from speech-to-text of its voice, which turns
        # "one point four five million" into "1.45" and "Harbourside" into "Harborside".
        "clientMessages": ["conversation-update", "model-output", "transcript", "speech-update", "status-update", "hang"],
        "analysisPlan": {
            "summaryPlan": {"enabled": True},
            "structuredDataPlan": {
                "enabled": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "caller_name": {"type": "string"},
                        "caller_type": {"type": "string", "enum": ["buyer", "renter", "owner", "tenant", "other"]},
                        "listing": {"type": "string"},
                        "booked_inspection": {"type": "boolean"},
                        "needs_callback": {"type": "boolean", "description": "True only if something is still left for the agent to do after this call (call back, offer, question, appraisal). A booking or cancellation that was completed on the call does not count."},
                        "callback_reason": {"type": "string", "description": "What the agent needs to do, in one sentence."},
                        "urgent": {"type": "boolean", "description": "Offer, deadline, settlement, or the caller said it's urgent."},
                        "budget_or_timeline": {"type": "string"},
                    },
                },
            },
        },
    }


if __name__ == "__main__":
    print(json.dumps(build_assistant(Settings()), indent=2))
