"""Memory that lasts (v0.8.4 W2A): the journal archive, dated and superseded facts, source tags, rolling summaries,
recency in search, and core (pinned) memories."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from dom import global_search, journal_archive, notes
from dom.home import Home, PrivateAccessError
from dom.memory_pass import MemoryPass
from dom.models import StoredMessage
from dom.prompt import PromptCache, build_messages, read_fresh_notes
from dom.store.store import recency_weight
from test_memory_pass import add, approve, files, ops, put as say, request_text
from test_traits import put
from tghelpers import FRIEND, GROUP, OWNER

TODAY = date(2026, 3, 1)
OTHER = -3003
JSON = {"Origin": "http://dom.test"}


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def read(root, rel):
    path = root / rel
    return path.read_text() if path.exists() else ""


# --- journal archive -------------------------------------------------------------------------------------------------

def journal(root, days, *, private=False):
    home = Home(root, allow_private=private)
    for offset in range(days):
        home.append_journal(date(2026, 1, 1) + timedelta(days=offset), f"entry: day {offset}", private=private)
    return home


def headings(text):
    return [line for line in text.splitlines() if line.startswith("## ")]


def test_entries_past_the_last_thirty_move_to_monthly_archive_files(tmp_path):
    home = journal(tmp_path, 62)  # 2026-01-01 .. 2026-03-03
    live = headings(read(tmp_path, "memory/JOURNAL.md"))
    assert len(live) == 30 and live[0] == "## 2026-02-02" and live[-1] == "## 2026-03-03"
    assert headings(read(tmp_path, "memory/journal/2026-01.md")) == [f"## 2026-01-{d:02d}" for d in range(1, 32)]
    assert headings(read(tmp_path, "memory/journal/2026-02.md")) == ["## 2026-02-01"]
    assert "entry: day 0" in read(tmp_path, "memory/journal/2026-01.md")
    assert home.journal_entries()[0].startswith("## 2026-02-02")  # the live file is read as before


def test_an_interrupted_move_loses_nothing_and_the_next_write_finishes_it(tmp_path, monkeypatch):
    journal(tmp_path, 30)
    home = Home(tmp_path)
    real, calls = Home._write_regular, []

    def failing(self, rel, content):
        calls.append(rel)
        if len(calls) == 3:  # the live trim after the first archive write
            raise OSError("disk went away")
        real(self, rel, content)

    monkeypatch.setattr(Home, "_write_regular", failing)
    with pytest.raises(OSError):
        home.append_journal(date(2026, 1, 31), "entry: late")
    monkeypatch.undo()
    assert "## 2026-01-01" in read(tmp_path, "memory/journal/2026-01.md")  # archived first ...
    assert "## 2026-01-01" in read(tmp_path, "memory/JOURNAL.md")  # ... and still live: in both, never neither
    home.append_journal(date(2026, 2, 1), "entry: next")
    archived = headings(read(tmp_path, "memory/journal/2026-01.md"))
    assert archived == ["## 2026-01-01", "## 2026-01-02"] and len(headings(read(tmp_path, "memory/JOURNAL.md"))) == 30
    assert headings(read(tmp_path, "memory/JOURNAL.md")).count("## 2026-01-01") == 0


def test_rewriting_a_day_does_not_duplicate_it_in_the_archive(tmp_path):
    home = journal(tmp_path, 31)
    home.append_journal(date(2026, 1, 1), "entry: again")  # same day as the archived one: a new live entry
    assert headings(read(tmp_path, "memory/journal/2026-01.md")).count("## 2026-01-01") == 1


def test_the_private_journal_archives_under_private_and_stays_private(tmp_path):
    journal(tmp_path, 32, private=True)
    assert "## 2026-01-01" in read(tmp_path, "private/journal/2026-01.md")
    assert not (tmp_path / "memory" / "journal").exists()
    public = Home(tmp_path)
    with pytest.raises(PrivateAccessError):
        public.read("private/journal/2026-01.md")
    with pytest.raises(PrivateAccessError):
        public.journal_archive_files(True)


def test_an_existing_short_journal_keeps_working_and_makes_no_archive(tmp_path):
    write(tmp_path, "memory/JOURNAL.md", "# Journal\n\n## 2025-12-30\nentry: old\n")
    Home(tmp_path).append_journal(date(2025, 12, 31), "entry: new")
    assert read(tmp_path, "memory/JOURNAL.md").startswith("# Journal\n\n## 2025-12-30")
    assert not (tmp_path / "memory" / "journal").exists()


async def test_global_search_finds_archive_entries_and_the_private_archive_is_owner_scope_only(make_app):
    app = make_app()
    journal(app.home.root, 40)
    journal(app.home.root, 40, private=True)
    write(app.home.root, "memory/journal/2026-01.md", read(app.home.root, "memory/journal/2026-01.md").replace("day 0", "day zephyr"))
    write(app.home.root, "private/journal/2026-01.md", read(app.home.root, "private/journal/2026-01.md").replace("day 1\n", "day quokka\n"))
    found = (await global_search.search_all(app.store, app.home, "zephyr"))["memory"]
    assert [(h.title, h.at) for h in found] == [("memory/journal/2026-01.md · 2026-01-01", "2026-01-01")]
    private = (await global_search.search_all(app.store, app.home, "quokka"))["memory"]
    assert [h.ref for h in private] == ["private/journal/2026-01.md"]


# --- dated facts, superseded not deleted ----------------------------------------------------------------------------

def test_replaced_and_removed_lines_go_to_the_former_file_with_date_and_reason(tmp_path):
    write(tmp_path, "memory/people/7.md", "- Works at the bakery (as of 2025-06) {any}\n- Has a cat\n")
    home = Home(tmp_path)
    assert home.memory_write("memory/people/7.md", "replace", "Works at the library (as of 2026-03)", "Works at the bakery (as of 2025-06)", today=TODAY).ok
    assert home.memory_write("memory/people/7.md", "remove", "", "Has a cat", today=TODAY).ok
    former = read(tmp_path, "memory/people/7.former.md").splitlines()
    assert former == [
        "- Works at the bakery (as of 2025-06) {any} [former 2026-03-01: replaced]",
        "- Has a cat [former 2026-03-01: removed]",
    ]
    assert home.list_memory_notes()[0] == ["memory/people/7.md"]  # the former file is not a person's notes


def test_a_refused_write_leaves_the_former_file_alone(tmp_path):
    write(tmp_path, "memory/chats/-5.md", "- " + "a" * 995)
    home = Home(tmp_path)
    res = home.memory_write("memory/chats/-5.md", "replace", "b" * 1200, "a" * 995)
    assert res.over_limit and not (tmp_path / "memory/chats/-5.former.md").exists()


def test_until_lines_past_their_date_expire_to_former_but_core_lines_stay(tmp_path):
    write(tmp_path, "memory/people/7.md", (
        "- Trip to Rome (until 2026-02-28) {any}\n- Exam on the 5th (until 2026-03-05)\n"
        "- Visa pending (until 2026-01-01) {core owner 2026-01-02}\n- Likes tea\n"
    ))
    assert Home(tmp_path).sweep_expired(TODAY) == 1
    assert read(tmp_path, "memory/people/7.md") == (
        "- Exam on the 5th (until 2026-03-05)\n- Visa pending (until 2026-01-01) {core owner 2026-01-02}\n- Likes tea\n"
    )
    assert read(tmp_path, "memory/people/7.former.md") == "- Trip to Rome (until 2026-02-28) {any} [former 2026-03-01: expired]\n"


# --- source tags and the leak eval ---------------------------------------------------------------------------------

def test_tag_syntax_round_trips_and_ignores_ordinary_braces():
    line = notes.parse_line("- Hates surprises (as of 2026-01) {chat -100123} {core dom 2026-02-03: knows him well}")
    assert (line.body, line.source, line.core) == (
        "- Hates surprises (as of 2026-01)", "-100123", notes.Core("dom", "2026-02-03", "knows him well"),
    )
    assert notes.format_line(line) == "- Hates surprises (as of 2026-01) {chat -100123} {core dom 2026-02-03: knows him well}"
    assert notes.parse_line("- uses {braces} in code").source is None
    assert notes.strip_tags("- a {any}\n- b {chat 5}\n- c") == "- a\n- b\n- c"


def test_tags_do_not_count_toward_the_size_limit(tmp_path):
    home = Home(tmp_path)
    fill = "x" * 990
    assert home.memory_write("memory/people/7.md", "add", fill, source="-1009").ok  # tagged {chat -1009}
    assert len(read(tmp_path, "memory/people/7.md")) > 1000 and notes.visible_len(read(tmp_path, "memory/people/7.md")) <= 1000
    assert not home.memory_write("memory/people/7.md", "add", "y" * 40, source="-1009").ok


def test_the_pass_tags_lines_with_its_chat_or_any_and_never_with_another_chat_or_a_core_mark(tmp_path):
    home = Home(tmp_path)
    write_ = lambda text: home.memory_write("memory/people/7.md", "add", text, source="-1009", today=TODAY)  # noqa: E731
    write_("Likes tea")
    write_("Works as a nurse {any}")
    write_("Sneaky {chat -777}")
    write_("Pushy {core dom 2026-03-01: because}")
    assert read(tmp_path, "memory/people/7.md").splitlines() == [
        "- Likes tea {chat -1009}", "- Works as a nurse {any}", "- Sneaky {chat -1009}", "- Pushy {chat -1009}",
    ]
    home.memory_write("memory/chats/-1009.md", "add", "Quiz night", source="-1009")
    assert read(tmp_path, "memory/chats/-1009.md") == "- Quiz night {chat -1009}"
    home.memory_write("memory/MEMORY.md", "add", "Dom likes rain", source="-1009")
    assert read(tmp_path, "memory/MEMORY.md") == "- Dom likes rain"  # no tag: it is read everywhere


def test_memory_md_refuses_chat_tagged_lines_even_from_the_owner(tmp_path):
    home = Home(tmp_path)
    assert not home.memory_write("memory/MEMORY.md", "add", "secret {chat -5}").ok
    res = home.save_memory("memory/MEMORY.md", "- secret {chat -5}", notes_version(""))
    assert not res.ok and "tagged" in res.error
    assert home.memory_write("memory/MEMORY.md", "add", "fine {any}").ok


def notes_version(text):
    from dom.home import content_version
    return content_version(text)


def seed_people(root):
    write(root, "memory/people/222.md", (
        "- Name is Fran\n"
        "- Is a nurse {any}\n"
        "- Told group A about the surprise party GROUP-A-SECRET {chat -1001}\n"
        "- Told group B about the new job GROUP-B-ONLY {chat -1002}\n"
    ))
    write(root, "memory/chats/-1001.md", "- A's running joke {chat -1001}\n- A secret {chat -1001} A-NOTE\n")
    write(root, "memory/MEMORY.md", "- Dom likes rain\n- legacy leak LEAKED-INTO-MEMORY {chat -1001}\n")


def every_prompt_text(home, scope, chat_id, people):
    messages = build_messages(home, scope, chat_id, people, "<chat_window/>", "note")
    snap = PromptCache().snapshot(home, scope, chat_id, people, datetime(2026, 3, 1, tzinfo=timezone.utc))
    fresh = read_fresh_notes(home, scope, chat_id, people)
    return json.dumps(messages) + "\n".join(snap.notes_blocks) + snap.memory_text + fresh


def test_leak_eval_a_fact_tagged_to_group_a_never_reaches_group_bs_prompt(tmp_path):
    seed_people(tmp_path)
    home = Home(tmp_path)
    for scope, chat_id in (("group", -1002), ("group", -1003), ("friend_dm", 222)):
        text = every_prompt_text(home, scope, chat_id, [222])
        assert "GROUP-A-SECRET" not in text and "LEAKED-INTO-MEMORY" not in text and "A-NOTE" not in text
        assert "Name is Fran" in text and "Is a nurse" in text and "Dom likes rain" in text
        assert not any(mark in text for mark in ("{chat", "{core", "{any}"))  # no tag is ever shown to the model
    b = every_prompt_text(home, "group", -1002, [222])
    assert "GROUP-B-ONLY" in b and "GROUP-B-ONLY" not in every_prompt_text(home, "group", -1001, [222])
    a = every_prompt_text(home, "group", -1001, [222])
    assert "GROUP-A-SECRET" in a and "A's running joke" in a


def test_the_owners_dm_may_read_every_public_line_with_the_tags_stripped(tmp_path):
    seed_people(tmp_path)
    text = every_prompt_text(Home(tmp_path), "owner_dm", OWNER, [222, OWNER])
    assert "GROUP-A-SECRET" in text and "GROUP-B-ONLY" in text and "{chat" not in text


async def test_a_group_pass_never_sees_lines_tagged_to_another_chat_and_cannot_reach_them(make_app, tmp_path):
    app = make_app(contents=(ops(
        {"file": "people/222.md", "op": "replace", "text": "x", "old": "GROUP-A-SECRET"},
        add("people/222.md", "Likes jazz"),
    ),))
    seed_people(tmp_path)
    await approve(app, OTHER)
    await say(app, OTHER, 1, "hello")
    await MemoryPass(app).run(OTHER)
    assert "GROUP-A-SECRET" not in request_text(app) and "Name is Fran" in request_text(app)
    kept = read(tmp_path, "memory/people/222.md")
    assert "GROUP-A-SECRET" in kept and "Told group A" in kept  # a guessed `old` cannot touch another chat's line
    assert "- Likes jazz {chat -3003}" in kept


def test_migrating_a_group_retags_lines_to_the_new_chat_id(tmp_path):
    seed_people(tmp_path)
    Home(tmp_path).move_chat_notes(-1001, -1009)
    assert "{chat -1009}" in read(tmp_path, "memory/chats/-1009.md") and "{chat -1001}" not in read(tmp_path, "memory/chats/-1009.md")
    assert "GROUP-A-SECRET (" not in read(tmp_path, "memory/people/222.md")
    assert "GROUP-A-SECRET {chat -1009}" in read(tmp_path, "memory/people/222.md")


# --- pins and core memories ----------------------------------------------------------------------------------------

def test_a_line_the_owner_adds_or_rewords_is_pinned_and_untouched_lines_are_not(tmp_path):
    write(tmp_path, "memory/people/7.md", "- Likes tea {any}\n- Has a cat\n")
    home = Home(tmp_path)
    current = read(tmp_path, "memory/people/7.md")
    edited = "- Likes tea {any}\n- Has two cats\n- Is vegetarian\n- Moved the tag {chat -5}\n"
    assert home.save_memory("memory/people/7.md", edited, notes_version(current), today=TODAY).ok
    assert read(tmp_path, "memory/people/7.md").splitlines() == [
        "- Likes tea {any}", "- Has two cats {core owner 2026-03-01}", "- Is vegetarian {core owner 2026-03-01}",
        "- Moved the tag {chat -5} {core owner 2026-03-01}",
    ]
    again = read(tmp_path, "memory/people/7.md")
    retagged = again.replace("Likes tea {any}", "Likes tea {chat -5}")  # only a tag changed: no pin
    assert home.save_memory("memory/people/7.md", retagged, notes_version(again), today=TODAY).ok
    assert read(tmp_path, "memory/people/7.md").splitlines()[0] == "- Likes tea {chat -5}"


def test_save_without_pin_is_how_approved_proposals_stay_unpinned(tmp_path):
    home = Home(tmp_path)
    assert home.save_memory("memory/MEMORY.md", "- Dom is learning piano", notes_version(""), pin=False).ok
    assert read(tmp_path, "memory/MEMORY.md") == "- Dom is learning piano"


def test_the_pass_cannot_rewrite_or_drop_a_core_line_but_can_still_edit_others(tmp_path):
    write(tmp_path, "memory/people/7.md", "- Mother's name is Ada {core owner 2026-01-02}\n- Likes tea\n")
    home = Home(tmp_path)
    for op, text in (("replace", "Mother is Eve"), ("remove", "")):
        res = home.memory_write("memory/people/7.md", op, text, "Mother's name is Ada", source="7")
        assert not res.ok and "core memory" in res.error
    assert home.memory_write("memory/people/7.md", "replace", "Likes coffee", "Likes tea", source="7").ok
    assert "Ada {core owner 2026-01-02}" in read(tmp_path, "memory/people/7.md")


def test_dom_pins_with_a_date_and_reason_up_to_the_cap_and_cannot_unpin_the_owners(tmp_path):
    write(tmp_path, "memory/MEMORY.md", "- I started writing letters\n- I fear the sea\n- Owner's pick {core owner 2026-01-02}\n")
    home = Home(tmp_path)
    assert home.pin_line("memory/MEMORY.md", "writing letters", "it started everything", 2, TODAY).ok
    assert "- I started writing letters {core dom 2026-03-01: it started everything}" in read(tmp_path, "memory/MEMORY.md")
    full = home.pin_line("memory/MEMORY.md", "fear the sea", "why not", 2, TODAY)
    assert not full.ok and "full (2)" in full.error
    assert not home.unpin_line("memory/MEMORY.md", "Owner's pick").ok  # the owner's pin is not his to drop
    assert home.unpin_line("memory/MEMORY.md", "writing letters").ok
    assert home.pin_line("memory/MEMORY.md", "fear the sea", "now there is room", 2, TODAY).ok
    assert not home.pin_line("memory/MEMORY.md", "fear the sea", "twice", 9, TODAY).ok
    assert [c["by"] for c in home.core_memories()] == ["dom", "owner"]


async def test_the_memory_pass_pins_and_unpins_through_ops_and_tells_the_model_the_count(make_app, tmp_path):
    app = make_app(contents=(ops(
        {"file": "MEMORY.md", "op": "pin", "old": "fear the sea", "text": "it shaped me"},
        {"file": "MEMORY.md", "op": "unpin", "old": "Owner's pick"},
        {"file": "MEMORY.md", "op": "pin", "old": "nothing like this", "text": "x"},
    ),))
    write(tmp_path, "memory/MEMORY.md", "- I fear the sea\n- Owner's pick {core owner 2026-01-02}\n")
    await approve(app, OTHER)
    await say(app, OTHER, 1, "hello")
    await MemoryPass(app).run(OTHER)
    text = read(tmp_path, "memory/MEMORY.md")
    assert "{core dom " in text and "it shaped me}" in text and "Owner's pick {core owner 2026-01-02}" in text
    assert "Core memories now: 1 of 30." in request_text(app)


async def test_the_core_memory_api_lists_every_core_memory_with_the_cap(make_app, browser, tmp_path):
    app = make_app()
    write(tmp_path, "memory/MEMORY.md", "- I fear the sea {core dom 2026-02-03: it shaped me}\n- plain\n")
    write(tmp_path, "private/USER.md", "- Owner is afraid of heights {core owner 2026-01-02}\n")
    async with browser(app) as client:
        body = (await client.get("/api/memory/core")).json()
    assert body["cap"] == 30
    assert body["items"] == [
        {"file": "memory/MEMORY.md", "text": "- I fear the sea", "source": "any", "by": "dom", "day": "2026-02-03", "why": "it shaped me"},
        {"file": "private/USER.md", "text": "- Owner is afraid of heights", "source": "any", "by": "owner", "day": "2026-01-02", "why": ""},
    ]


# --- recency in search ---------------------------------------------------------------------------------------------

def test_recency_weight_halves_the_fade_every_sixty_days():
    assert recency_weight(0) == 1 and recency_weight(60) == pytest.approx(0.75) and recency_weight(10_000) == pytest.approx(0.5, abs=1e-3)


async def test_search_ranks_recent_hits_up_unless_a_core_memory_was_pinned_that_day(make_app):
    app = make_app()
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for msg_id, text, age in ((1, "tea tea tea tea", 400), (2, "tea", 1)):
        await app.store.record_new_message(StoredMessage(
            chat_id=str(FRIEND), msg_id=msg_id, sender_user_id=FRIEND, sender_name="Fran", role="user", is_bot=False,
            visibility_scope=app.store.visibility_for(FRIEND), text=text, created_at=(now - timedelta(days=age)).isoformat(),
        ))
    plain = await app.store.search_chat_history("tea", str(FRIEND), 5, now=now)
    assert [h["msg_id"] for h in plain] == [2, 1]
    day = (now - timedelta(days=400)).date().isoformat()
    kept = await app.store.search_chat_history("tea", str(FRIEND), 5, frozenset({day}), now=now)
    assert [h["msg_id"] for h in kept] == [1, 2]


# --- rolling summaries ---------------------------------------------------------------------------------------------

def archive_december(root, *, private=False):
    base = "private/journal" if private else "memory/journal"
    entries = [f"## 2025-12-{d:02d}\nmood: calm\nentry: DAY-{d}\nnudges: {{}}" for d in (2, 3, 9, 10, 28)]
    write(root, f"{base}/2025-12.md", "\n\n".join(["# Journal 2025-12", *entries]) + "\n")


async def test_weeks_months_and_years_roll_up_once_and_the_raw_entries_never_change(make_app, tmp_path):
    app = make_app(contents=("WEEK-ONE", "WEEK-TWO", "WEEK-LAST", "MONTH-DEC", "YEAR-2025"))
    archive_december(tmp_path)
    raw = read(tmp_path, "memory/journal/2025-12.md")
    write(tmp_path, "memory/people/7.md", "- Mother is Ada {core dom 2025-12-03: she matters}\n")
    await journal_archive.run(app)

    month = read(tmp_path, "memory/journal/2025-12.md")
    lines = month.splitlines()
    assert lines[0] == "# Journal 2025-12"
    assert "Week 2025-W49: WEEK-ONE" in lines and "Week 2025-W50: WEEK-TWO" in lines and "Week 2025-W52: WEEK-LAST" in lines
    assert "Core 2025-W49: Mother is Ada" in lines and "Month 2025-12: MONTH-DEC" in lines and "Core 2025-12: Mother is Ada" in lines
    assert month[month.index("## 2025-12-02"):] == raw[raw.index("## 2025-12-02"):]  # the entries are untouched
    year = read(tmp_path, "memory/journal/2025.md")
    assert "Year 2025: YEAR-2025" in year and "Core 2025: Mother is Ada" in year
    prompts = [json.dumps(r["messages"]) for r in app.script.requests]
    assert "DAY-2" in prompts[0] and "DAY-9" not in prompts[0] and "nudges" not in prompts[0]
    assert "WEEK-ONE" in prompts[3] and "DAY-" not in prompts[3]  # a month is rolled up from its weeks
    assert "MONTH-DEC" in prompts[4] and "WEEK-ONE" not in prompts[4]

    before = len(app.script.requests)
    await journal_archive.run(app)
    assert len(app.script.requests) == before  # nothing is summarised twice


async def test_the_private_archive_is_summarised_apart_and_never_links_a_private_note_publicly(make_app, tmp_path):
    app = make_app(contents=("W1", "W2", "W3", "M", "Y", "PW1", "PW2", "PW3", "PM", "PY"))
    archive_december(tmp_path)
    archive_december(tmp_path, private=True)
    write(tmp_path, "private/NOTES.md", "- Owner fears heights {core owner 2025-12-03}\n")
    await journal_archive.run(app)
    assert "heights" not in read(tmp_path, "memory/journal/2025-12.md")
    assert "Core 2025-W49: Owner fears heights" in read(tmp_path, "private/journal/2025-12.md")
    public_prompts = " ".join(json.dumps(r["messages"]) for r in app.script.requests[:5])
    assert "PW1" not in public_prompts


async def test_summaries_pause_at_the_spend_cap_and_keep_nothing_half_written(make_app, tmp_path, monkeypatch):
    app = make_app(contents=("W1",))
    archive_december(tmp_path)
    raw = read(tmp_path, "memory/journal/2025-12.md")

    async def paused(chat_id):
        return False

    monkeypatch.setattr(app.dispatch, "model_allowed", paused)
    await journal_archive.run(app)
    assert read(tmp_path, "memory/journal/2025-12.md") == raw and not app.script.requests


async def test_a_week_still_overlapping_the_live_journal_is_not_summarised_yet(make_app, tmp_path):
    app = make_app(contents=("W1",))
    archive_december(tmp_path)
    write(tmp_path, "memory/JOURNAL.md", "## 2025-12-12\nentry: live\n")  # the live window starts mid December
    await journal_archive.run(app)
    month = read(tmp_path, "memory/journal/2025-12.md")
    assert "Week 2025-W49: W1" in month and "Week 2025-W50" not in month and "Month 2025-12" not in month


async def test_recent_life_reaches_only_the_owners_dm_after_the_window(make_app, tmp_path):
    app = make_app(contents=("WEEK-ONE", "WEEK-TWO", "WEEK-LAST", "MONTH-DEC", "YEAR-2025"))
    archive_december(tmp_path)
    await journal_archive.run(app)
    block = read(tmp_path, "memory/journal/RECENT.md")
    assert "Weeks:\n- 2025-W49: WEEK-ONE" in block and "Months:\n- 2025-12: MONTH-DEC" in block and "Years:\n- 2025: YEAR-2025" in block
    owner = build_messages(app.home, "owner_dm", OWNER, [OWNER], "<chat_window/>", "note")
    assert "MONTH-DEC" not in owner[0]["content"]  # never in the shared system prompt
    assert owner[1]["content"].index("<chat_window/>") < owner[1]["content"].index("MONTH-DEC") < owner[1]["content"].index("note")
    for scope, chat_id in (("group", GROUP), ("friend_dm", FRIEND)):
        other = build_messages(app.home, scope, chat_id, [FRIEND], "<chat_window/>", "note")
        assert "MONTH-DEC" not in json.dumps(other)
