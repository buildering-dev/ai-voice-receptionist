import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from receptionist.agency import Agency
from receptionist.assistant import TOOL_SPECS, build_assistant
from receptionist.config import ROOT, Settings
from receptionist.notify import Notifier
from receptionist.server import _tool_calls, create_app
from receptionist.store import Store
from receptionist.tools import Tools

TZ = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 10, 12, 14, 0, tzinfo=TZ)  # Monday 12 October 2026, 2pm


@pytest.fixture
def agency():
    return Agency.load(ROOT / "data" / "agency.json")


@pytest.fixture
def tools(agency, tmp_path):
    return Tools(agency, Store(tmp_path / "t.db"), Notifier(), clock=lambda: NOW)


def settings(tmp_path, **kw):
    s = Settings()
    s.db_path = tmp_path / "s.db"
    s.webhook_secret = kw.get("secret", "")
    s.admin_token = kw.get("admin", "")
    s.server_url = "https://voice.example.com"
    return s


def codes(text):
    return re.findall(r"\[(\d{4}-\d\d-\d\dT\d\d:\d\d)\]", text)


# ---- listings -------------------------------------------------------------------
def test_find_listing_by_suburb_id_and_street(agency):
    assert agency.find_listings("the Lavender Bay apartment")[0]["id"] == "HR-101"
    assert agency.find_listings("hr-202")[0]["id"] == "HR-202"
    assert agency.find_listings("Kendall Road")[0]["id"] == "HR-102"
    assert agency.find_listings("Bondi") == []


def test_listing_details_only_states_known_facts(tools):
    out = tools.call("get_listing_details", {"query": "Chatswood"})
    assert "seven hundred and twenty dollars a week" in out and "Marcus Webb" in out


def test_unknown_listing_lists_what_we_have(tools):
    out = tools.call("get_listing_details", {"query": "Bondi beach house"})
    assert "No listing matches" in out and "Lavender Bay" in out


# ---- times ----------------------------------------------------------------------
def test_open_homes_are_future_and_on_the_right_weekday(agency):
    slots = agency.open_homes("HR-101", NOW)
    assert slots and all(s.start > NOW for s in slots)
    assert {s.start.strftime("%a") for s in slots} <= {"Sat", "Wed"}


def test_private_slots_respect_notice_hours_and_weekdays(agency):
    slots = agency.private_slots("HR-102", NOW)
    assert slots
    assert all((s.start - NOW).total_seconds() >= 24 * 3600 for s in slots)
    assert all(s.start.strftime("%a") in {"Mon", "Tue", "Wed", "Thu", "Fri"} for s in slots)
    assert all("10:00" <= s.start.strftime("%H:%M") <= "16:30" for s in slots)


def test_find_times_gives_spoken_dates_and_codes(tools):
    out = tools.call("find_inspection_times", {"listing": "Willoughby"})
    assert "Saturday 17 October at 11 am [2026-10-17T11:00]" in out
    assert "Private inspections available" in out


# ---- booking --------------------------------------------------------------------
def test_book_open_home_then_duplicate_is_idempotent(tools):
    out = tools.call("book_inspection", {"listing": "HR-101", "time_code": "2026-10-17T10:00", "name": "Jordan Lee", "phone": "0400 000 000", "phone_confirmed": True})
    assert out.startswith("Booked: open home") and "Confirmation code HR" in out
    again = tools.call("book_inspection", {"listing": "HR-101", "time_code": "2026-10-17T10:00", "name": "Jordan Lee", "phone": "0400000000", "phone_confirmed": True})
    assert "already booked" in again
    assert len(tools.store.bookings()) == 1
    assert tools.notifier.sent and "Jordan Lee" in tools.notifier.sent[0]


def test_private_slot_can_only_be_booked_once(tools):
    out = tools.call("find_inspection_times", {"listing": "HR-102"})
    private = codes(out.split("Private inspections available")[1])[0]
    assert tools.call("book_inspection", {"listing": "HR-102", "time_code": private, "name": "A", "phone": "0411111111", "phone_confirmed": True}).startswith("Booked")
    taken = tools.call("book_inspection", {"listing": "HR-102", "time_code": private, "name": "B", "phone": "0422222222", "phone_confirmed": True})
    assert "just took that private slot" in taken
    assert private not in codes(tools.call("find_inspection_times", {"listing": "HR-102"}))


def test_made_up_or_past_times_are_refused(tools):
    for bad in ["2026-10-17T03:00", "2026-10-11T10:00", "next Saturday", "2026-10-13T10:00"]:  # 13th 10am is < 24h notice
        out = tools.call("book_inspection", {"listing": "HR-101", "time_code": bad, "name": "X", "phone": "0400000000", "phone_confirmed": True})
        assert "isn't available" in out, bad
    assert tools.store.bookings() == []


def test_caller_id_is_offered_not_used_silently(tools):
    ctx = {"caller_number": "+61 491 570 156", "call_id": "c1"}
    first = tools.call("get_listing_details", {"query": "Chatswood"}, ctx)
    assert "Caller ID: zero four nine one, five seven zero, one five six" in first
    ask = tools.call("book_inspection", {"listing": "HR-101", "time_code": "2026-10-17T10:00", "name": "Sam"}, ctx)
    assert ask.startswith("Not booked yet") and "ending in one five six" in ask and tools.store.bookings() == []
    out = tools.call("book_inspection", {"listing": "HR-101", "time_code": "2026-10-17T10:00", "phone_confirmed": True}, ctx)
    assert out.startswith("Booked") and "mobile zero four nine one, five seven zero, one five six" in out
    out2 = tools.call("book_inspection", {"listing": "HR-201", "time_code": "2026-10-17T12:00", "name": "Sam"})
    assert "need the caller's mobile" in out2


def test_a_spoken_number_must_be_read_back_before_booking(tools):
    ctx = {"call_id": "rb"}
    out = tools.call("book_inspection", {"listing": "HR-102", "time_code": "2026-10-17T11:00", "name": "John Smith", "phone": "0491 570 157"}, ctx)
    assert out.startswith("Not booked yet") and "zero four nine one, five seven zero, one five seven" in out
    assert tools.store.bookings() == []
    assert tools.call("book_inspection", {"listing": "HR-102", "time_code": "2026-10-17T11:00", "phone_confirmed": "true"}, ctx).startswith("Booked")
    # confirmed once per call is enough for a later message
    msg = tools.call("take_message", {"reason": "Wants an appraisal", "property_address": "84 Spring St, Chatswood"}, ctx)
    assert msg.startswith("Message saved")
    # a different number has to be read back again
    new = tools.call("take_message", {"reason": "Call me on my work phone", "phone": "0298765432"}, ctx)
    assert new.startswith("Message not saved yet")


def test_take_message(tools):
    assert "Ask briefly" in tools.call("take_message", {"name": "Kim", "phone": "0400000001", "phone_confirmed": True})
    out = tools.call("take_message", {"name": "Kim", "phone": "0400000001", "reason": "Wants a call about maintenance", "for_agent": "Marcus Webb", "phone_confirmed": True})
    assert "Message saved for Marcus Webb" in out
    assert tools.store.messages()[0]["reason"] == "Wants a call about maintenance"


def test_unknown_tool_and_bad_args_dont_crash(tools):
    assert "Unknown tool" in tools.call("delete_everything", {})
    assert "Missing or invalid" in tools.call("book_inspection", {"listing": "HR-101"})


# ---- webhook --------------------------------------------------------------------
def tool_payload(name, args, shape="list"):
    if shape == "list":
        return {"message": {"type": "tool-calls", "call": {"id": "c9", "customer": {"number": "+61400111222"}},
                            "toolCallList": [{"id": "tc1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}
    return {"message": {"type": "tool-calls", "call": {"id": "c9"},
                        "toolWithToolCallList": [{"type": "function", "name": name,
                                                  "toolCall": {"id": "tc1", "function": {"name": name, "arguments": args}}}]}}


def test_tool_call_parsing_handles_both_shapes():
    for shape in ("list", "with"):
        calls = _tool_calls(tool_payload("get_listing_details", {"query": "x"}, shape)["message"])
        assert calls == [("tc1", "get_listing_details", {"query": "x"})]


def test_webhook_tool_call_round_trip(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    r = client.post("/vapi/webhook", json=tool_payload("get_listing_details", {"query": "Hunters Hill"}))
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["toolCallId"] == "tc1" and res["name"] == "get_listing_details" and "eleven hundred and fifty dollars a week" in res["result"]


def test_webhook_rejects_wrong_secret(tmp_path):
    client = TestClient(create_app(settings(tmp_path, secret="s3cret")))
    assert client.post("/vapi/webhook", json=tool_payload("get_listing_details", {"query": "x"})).status_code == 401
    ok = client.post("/vapi/webhook", json=tool_payload("get_listing_details", {"query": "x"}), headers={"X-Vapi-Secret": "s3cret"})
    assert ok.status_code == 200


def test_end_of_call_report_is_stored_and_admin_is_protected(tmp_path):
    app = create_app(settings(tmp_path, admin="adm"))
    client = TestClient(app)
    report = {"message": {"type": "end-of-call-report", "call": {"id": "c10", "customer": {"number": "+61400"}},
                          "endedReason": "customer-ended-call", "cost": 0.21,
                          "analysis": {"summary": "Asked about HR-201.", "structuredData": {"caller_type": "renter"}},
                          "artifact": {"transcript": "AI: Hi\nUser: Hello", "recordingUrl": "https://example.com/r.wav"}}}
    assert client.post("/vapi/webhook", json=report).json() == {"ok": True}
    call = app.state.store.calls()[0]
    assert call["summary"] == "Asked about HR-201." and json.loads(call["structured"])["caller_type"] == "renter"
    assert client.get("/admin").status_code == 404
    page = client.get("/admin?token=adm")
    assert page.status_code == 200 and "Asked about HR-201." in page.text


def test_demo_page_and_health(tmp_path):
    client = TestClient(create_app(settings(tmp_path)))
    assert client.get("/health").json()["listings"] == 4
    page = client.get("/").text
    assert "Harbourside Realty" in page and "Start call" in page


# ---- assistant definition -------------------------------------------------------
def test_assistant_tools_match_server_handlers(tmp_path, agency):
    s = settings(tmp_path, secret="abc")
    a = build_assistant(s, agency)
    fn_names = [t["function"]["name"] for t in a["model"]["tools"] if t["type"] == "function"]
    assert fn_names == [t["name"] for t in TOOL_SPECS]
    assert set(fn_names) == set(Tools(agency, Store(tmp_path / "x.db"), Notifier()).handlers())
    for t in a["model"]["tools"]:
        if t["type"] == "function":
            assert t["server"]["url"] == "https://voice.example.com/vapi/webhook"
            assert t["server"]["headers"]["X-Vapi-Secret"] == "abc"
    prompt = a["model"]["messages"][0]["content"]
    assert "Harbourside Realty" in prompt and '{{"now" | date:' in prompt and "{listing_lines}" not in prompt
    assert a["serverMessages"] == ["end-of-call-report"]
    assert "unit 7 at 42 Bayview Street" in prompt and "7/42" not in prompt and "$1,450,000" not in prompt
    # patient turn-taking: waits longer while the caller gives a number
    plan = a["startSpeakingPlan"]
    assert plan["waitSeconds"] >= 0.5 and any(r["type"] == "customer" and r["timeoutSeconds"] >= 2 for r in plan["customEndpointingRules"])
    import re as _re
    assert _re.search(plan["customEndpointingRules"][1]["regex"], "John Smith 0 4 1") and not _re.search(plan["customEndpointingRules"][1]["regex"], "Thanks")


# ---- memory and speech --------------------------------------------------------------
def test_details_given_once_are_reused_in_the_same_call(tools):
    ctx = {"call_id": "call-A"}
    out = tools.call("book_inspection", {"listing": "HR-101", "time_code": "2026-10-17T10:00", "name": "John Smith", "phone": "0491 570 158", "phone_confirmed": True}, ctx)
    assert out.startswith("Booked") and "Don't ask for these again" in out
    # appraisal later in the same call: no name or phone passed, but an address is needed first
    ask = tools.call("take_message", {"reason": "Free appraisal of their home"}, ctx)
    assert ask.startswith("For an appraisal, ask for the property address")
    ok = tools.call("take_message", {"reason": "Free appraisal of their home", "property_address": "5 Rose St, Mosman", "for_agent": "Sophie Tran"}, ctx)
    assert "Message saved for Sophie Tran" in ok
    msg = tools.store.messages()[0]
    assert (msg["name"], msg["phone"]) == ("John Smith", "0491570158") and "5 Rose St, Mosman" in msg["reason"]
    # a different call starts with no details
    assert "need the caller's full name" in tools.call("book_inspection", {"listing": "HR-201", "time_code": "2026-10-17T12:00"}, {"call_id": "call-B"})


def test_email_is_remembered_and_used_for_confirmation(tools):
    ctx = {"call_id": "call-E"}
    out = tools.call("book_inspection", {"listing": "HR-202", "time_code": "2026-10-17T09:30", "name": "Ana", "phone": "0400222333", "email": "ana at example dot com", "phone_confirmed": True}, ctx)
    assert "confirmation will go to ana@example.com" in out
    assert tools.store.bookings()[0]["email"] == "ana@example.com"


def test_returning_caller_is_recognised_by_caller_id(tools):
    tools.call("book_inspection", {"listing": "HR-101", "time_code": "2026-10-17T10:00", "name": "John Smith", "phone": "0491570158", "phone_confirmed": True}, {"call_id": "first"})
    later = tools.call("get_listing_details", {"query": "Chatswood"}, {"call_id": "second", "caller_number": "+61491570158"})
    assert "Returning caller" in later and "John Smith" in later and "unit 7 at 42 Bayview Street" in later
    again = tools.call("get_listing_details", {"query": "Chatswood"}, {"call_id": "second", "caller_number": "+61491570158"})
    assert "Returning caller" not in again  # only mentioned once per call
    # and the name is now known, so booking needs no questions
    assert tools.call("book_inspection", {"listing": "HR-201", "time_code": "2026-10-17T12:00", "phone_confirmed": True}, {"call_id": "second", "caller_number": "+61491570158"}).startswith("Booked")


def test_voice_friendly_addresses_prices_and_times(tools, agency):
    out = tools.call("get_listing_details", {"query": "Lavender Bay"})
    assert out.startswith("unit 7 at 42 Bayview Street") and "Price: one point four five to one point five five million dollars." in out
    assert "$1,450,000" not in out
    times = tools.call("find_inspection_times", {"listing": "HR-101"})
    assert "at 10 am" in times and "don't read them aloud" in times


def test_unit_addresses_are_always_spoken_as_unit_then_street(agency, tmp_path):
    a = build_assistant(settings(tmp_path), agency)
    reps = [r for r in a["voice"]["chunkPlan"]["formatPlan"]["replacements"] if r["type"] == "regex"]
    for written in ["742 Bayview Street", "7/42 Bayview Street", "7 42 Bayview Street", "unit 7, 42 Bayview Street", "unit 7 at 42 Bayview Street"]:
        out = written
        for r in reps:
            out = re.sub(r["regex"], r["value"], out, flags=re.I)
        assert out == "unit seven at forty-two Bayview Street", (written, out)
    # plain street addresses are untouched
    out = "18 Kendall Road"
    for r in reps:
        out = re.sub(r["regex"], r["value"], out, flags=re.I)
    assert out == "18 Kendall Road"


def test_waits_when_caller_trails_off(agency, tmp_path):
    rules = build_assistant(settings(tmp_path), agency)["startSpeakingPlan"]["customEndpointingRules"]
    trail = rules[2]["regex"]
    assert re.search(trail, "When can I inspect the Willoughby house? Can you", re.I)
    assert re.search(trail, "It's for my", re.I) and re.search(trail, "and um", re.I)
    assert not re.search(trail, "Book me in for Saturday.", re.I)


# ---- how numbers are spoken and shown (from test-call feedback) -----------------
def test_phone_numbers_are_spelled_digit_by_digit():
    from receptionist.agency import spoken_phone
    assert spoken_phone("0491570157") == "zero four nine one, five seven zero, one five seven"
    assert spoken_phone("+61 491 570 157".replace("+61 ", "0")) == "zero four nine one, five seven zero, one five seven"
    assert spoken_phone("") == ""


def test_tool_results_never_contain_digit_phone_numbers(tools):
    ctx = {"call_id": "p1"}
    out = tools.call("book_inspection", {"listing": "HR-102", "time_code": "2026-10-17T11:00", "name": "John Smith", "phone": "0491 570 157", "phone_confirmed": True}, ctx)
    assert "zero four nine one, five seven zero, one five seven" in out and not re.search(r"\d{3}", out.split("Confirmation")[0])
    again = tools.call("get_listing_details", {"query": "Chatswood"}, ctx)
    assert "0491" not in again


def test_filler_lines_have_no_number_words_and_booking_has_none(tmp_path, agency):
    a = build_assistant(settings(tmp_path), agency)
    by_name = {t["function"]["name"]: t for t in a["model"]["tools"] if t["type"] == "function"}
    for t in by_name.values():
        for m in t.get("messages", []):
            assert not re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\b", m["content"], re.I)
    # saves are silent unless the server is slow, so no filler line repeats through the call
    for n in ("book_inspection", "take_message", "cancel_inspection"):
        assert [m["type"] for m in by_name[n]["messages"]] == ["request-response-delayed"]
    lines = [m["content"] for t in by_name.values() for m in t.get("messages", []) if m["type"] == "request-start"]
    assert len(lines) == len(set(lines)) == 2
    assert "model-output" in a["clientMessages"]
    assert "phone_confirmed" in by_name["book_inspection"]["function"]["parameters"]["properties"]
    assert "phone_confirmed" in by_name["take_message"]["function"]["parameters"]["properties"]
    assert {"Harbourside Realty", "Kendall Road", "Sophie Tran"} <= set(a["transcriber"]["keyterm"])
    prompt = a["model"]["messages"][0]["content"]
    assert "digit by digit" in prompt and "acknowledge it" in prompt and "0491 570 158" not in prompt


def test_saved_transcript_uses_the_assistants_own_words(tmp_path):
    from receptionist.server import clean_transcript
    art = {"transcript": "AI: Lavender Bay is 1.45 to $1,550,000.",
           "messagesOpenAIFormatted": [
               {"role": "system", "content": "prompt"},
               {"role": "assistant", "content": "Thanks for calling Harbourside Realty."},
               {"role": "user", "content": "What's the price guide?"},
               {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
               {"role": "tool", "content": "Price: ..."},
               {"role": "assistant", "content": "One point four five to one point five five million dollars."}]}
    assert clean_transcript(art) == ("AI: Thanks for calling Harbourside Realty.\nUser: What's the price guide?\n"
                                     "AI: One point four five to one point five five million dollars.")
    assert clean_transcript({"transcript": "AI: Hi"}) == "AI: Hi"


def test_demo_page_shows_first_message_from_config(tmp_path):
    page = TestClient(create_app(settings(tmp_path))).get("/").text
    assert '"Hi, you\'ve reached Sophie Tran at Harbourside Realty.' in page and "virtual assistant" in page
    assert "__FIRST_MESSAGE__" not in page


def test_asking_for_a_particular_day(tools):
    # Monday 2pm: Tuesday 10am-1:30pm is inside the 24h notice, later Tuesday slots are free
    tue = tools.call("find_inspection_times", {"listing": "HR-101", "day": "Tuesday"})
    assert "On Tuesday 13 October" in tue and "[2026-10-13T14:00]" in tue and "[2026-10-13T13:30]" not in tue
    sun = tools.call("find_inspection_times", {"listing": "HR-101", "day": "Sunday"})
    assert "Nothing free on Sunday 18 October" in sun and "only run on weekdays" in sun and "Nearest options" in sun
    far = tools.call("find_inspection_times", {"listing": "HR-101", "day": "2026-11-20"})
    assert "only book 14 days ahead" in far
    assert "couldn't work out which day" in tools.call("find_inspection_times", {"listing": "HR-101", "day": "whenever"})
    # a day inside the notice period says why
    soon = Tools(tools.agency, tools.store, tools.notifier, clock=lambda: NOW.replace(hour=17)).call(
        "find_inspection_times", {"listing": "HR-201", "day": "tomorrow"})
    assert "24 hours' notice" in soon


def test_appraisal_message_does_not_repeat_the_address(tools):
    ctx = {"call_id": "ap"}
    tools.call("take_message", {"reason": "Free appraisal at 84 Spring Street, Chatswood", "name": "Jo", "phone": "0400111222",
                                "property_address": "84 Spring Street, Chatswood", "phone_confirmed": True}, ctx)
    assert tools.store.messages()[0]["reason"].count("84 Spring Street") == 1


def test_agent_line_mode_and_urgent_callbacks(tools, tmp_path, agency):
    a = build_assistant(settings(tmp_path), agency)
    assert a["firstMessage"].startswith("Hi, you've reached Sophie Tran") and "virtual assistant" in a["firstMessage"]
    prompt = a["model"]["messages"][0]["content"]
    assert "## Whose line this is" in prompt and "within business hours" not in prompt.split("## Whose line")[0]
    ctx = {"call_id": "u1"}
    out = tools.call("take_message", {"reason": "Wants to make an offer on Kendall Road", "name": "Ben Ng", "phone": "0400333444",
                                      "phone_confirmed": True, "urgent": True}, ctx)
    assert "Message saved for Sophie Tran and flagged urgent" in out and "as soon as Sophie's free" in out
    assert tools.store.messages()[0]["reason"].startswith("URGENT") and tools.notifier.sent[-1].startswith("URGENT Call back Ben Ng")
    rent = tools.call("take_message", {"reason": "Leaking tap at the Chatswood unit", "for_agent": "Marcus Webb"}, ctx)
    assert "Message saved for Marcus Webb" in rent and "within business hours" not in rent


def test_reception_mode_still_works(tmp_path):
    import json as _json
    data = _json.loads((ROOT / "data" / "agency.json").read_text())
    data.pop("line_agent")
    f = tmp_path / "agency.json"; f.write_text(_json.dumps(data))
    ag = Agency.load(f)
    a = build_assistant(settings(tmp_path), ag)
    assert a["firstMessage"] == "Thanks for calling Harbourside Realty, this is Clara. How can I help?"
    assert "## Whose line this is" not in a["model"]["messages"][0]["content"]
    t = Tools(ag, Store(tmp_path / "r.db"), Notifier(), clock=lambda: NOW)
    out = t.call("take_message", {"reason": "General question", "name": "Al", "phone": "0400555666", "phone_confirmed": True})
    assert "within business hours" in out


def test_admin_shows_leads_captured(tmp_path):
    app = create_app(settings(tmp_path, admin="adm"))
    app.state.tools.call("take_message", {"reason": "Free appraisal please", "name": "Jo", "phone": "0400111222",
                                          "property_address": "1 Test St", "phone_confirmed": True}, {"call_id": "live"})
    page = TestClient(app).get("/admin?token=adm").text
    assert "missed calls, handled" in page and "Appraisal leads" in page
    # counted straight away, before Vapi's summary arrives
    assert "<b>1</b><span>Calls answered</span>" in page and "Summary on its way" in page


def test_cancel_inspection(tools):
    ctx = {"call_id": "cx", "caller_number": "+61400777888"}
    tools.call("book_inspection", {"listing": "HR-102", "time_code": "2026-10-17T11:00", "name": "Lee", "phone_confirmed": True}, ctx)
    tools.call("book_inspection", {"listing": "HR-101", "time_code": "2026-10-17T10:00", "phone_confirmed": True}, ctx)
    later = {"call_id": "cy", "caller_number": "+61400777888"}
    which = tools.call("cancel_inspection", {}, later)
    assert "more than one booking" in which
    out = tools.call("cancel_inspection", {"listing": "Willoughby"}, later)
    assert out.startswith("Cancelled: Lee's inspection at 18 Kendall Road") and tools.notifier.sent[-1].startswith("Cancelled:")
    assert [b["listing_id"] for b in tools.store.bookings()] == ["HR-101"]
    assert "no booking under that number" in tools.call("cancel_inspection", {"listing": "Willoughby"}, later)
    assert "Ask for the mobile number" in tools.call("cancel_inspection", {"listing": "Willoughby"}, {"call_id": "cz"})


def test_call_back_is_saved_even_if_the_ai_forgot(tmp_path):
    app = create_app(settings(tmp_path))
    client = TestClient(app)
    report = {"message": {"type": "end-of-call-report", "call": {"id": "forgot"}, "endedReason": "assistant-ended-call",
                          "analysis": {"summary": "Wants to make an offer on Kendall Road.",
                                       "structuredData": {"caller_name": "John Smith", "needs_callback": True, "urgent": True,
                                                          "callback_reason": "Make an offer on 18 Kendall Road, Willoughby."}},
                          "artifact": {"transcript": "..."}}}
    client.post("/vapi/webhook", json=report)
    msg = app.state.store.messages()[0]
    assert msg["reason"].startswith("URGENT: Make an offer") and msg["for_agent"] == "Sophie Tran" and msg["call_id"] == "forgot"
    # not duplicated when the AI did save one
    app.state.tools.call("take_message", {"reason": "x", "name": "A", "phone": "0400000009", "phone_confirmed": True, "urgent": True}, {"call_id": "saved"})
    report["message"]["call"]["id"] = "saved"
    client.post("/vapi/webhook", json=report)
    assert len(app.state.store.messages_for_call("saved")) == 1


def test_goodbye_is_not_cut_off(tmp_path, agency):
    a = build_assistant(settings(tmp_path), agency)
    assert not any(t["type"] == "endCall" for t in a["model"]["tools"]) and "endCallMessage" not in a
    assert "have a great day" in a["endCallPhrases"]
    assert "Thanks for calling, have a great day." in a["model"]["messages"][0]["content"]
    assert "say NOTHING in that same reply" in a["model"]["messages"][0]["content"]


def test_cancelling_an_appraisal_does_not_ask_for_the_address_again(tools):
    out = tools.call("take_message", {"reason": "Wants to cancel the appraisal request for now", "name": "Jo", "phone": "0400111222", "phone_confirmed": True})
    assert out.startswith("Message saved")


def test_missed_urgent_offer_is_saved_even_if_another_message_was(tmp_path):
    app = create_app(settings(tmp_path))
    app.state.tools.call("take_message", {"reason": "Free appraisal", "name": "A", "phone": "0400000009", "phone_confirmed": True,
                                          "property_address": "1 Test St"}, {"call_id": "both"})
    report = {"message": {"type": "end-of-call-report", "call": {"id": "both"}, "endedReason": "x",
                          "analysis": {"summary": "s", "structuredData": {"needs_callback": True, "urgent": True,
                                                                         "callback_reason": "Offer on Willoughby."}},
                          "artifact": {"transcript": ""}}}
    TestClient(app).post("/vapi/webhook", json=report)
    reasons = [m["reason"] for m in app.state.store.messages_for_call("both")]
    assert len(reasons) == 2 and any(r.startswith("URGENT: Offer on Willoughby.") for r in reasons)


def test_phone_tails_are_read_digit_by_digit(tmp_path, agency):
    fp = build_assistant(settings(tmp_path), agency)["voice"]["chunkPlan"]["formatPlan"]
    assert fp["numberToDigitsCutoff"] == 99
    reps = {r["key"]: r["value"] for r in fp["replacements"] if r["type"] == "exact"}
    assert reps["ending in 045"] == "ending in zero four five" and len(reps) == 100
