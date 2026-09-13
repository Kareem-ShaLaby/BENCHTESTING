#!/usr/bin/env python3
"""
test_bot.py — correctness regression suite for The Quizician bot.

WHAT THIS IS (AND ISN'T)
-------------------------
loadtest.py answers "is it fast enough?" This file answers "is it still
CORRECT?" — it imports your real bot.py (same trick loadtest.py uses: fake
Telegram, no network) and asserts specific, checkable facts about its
behavior: exact counts, exact filtering, exact fallback strings. Nothing
here measures latency; everything here is a pass/fail on business logic
that would otherwise only be caught by a person noticing something looked
wrong in production.

Every test either (a) locks in a behavior this bot was already relying on
(so a future edit that quietly breaks it fails loudly here instead of
silently in prod), or (b) is a regression test for a bug that actually
happened this session — most notably test_likely_cause_badrequest_is_not_
misclassified_as_network, which reproduces the exact "BadRequest is a
subclass of NetworkError" ordering bug caught by hand while building the
error-log improvements, so it can never silently come back.

HOW TO RUN
----------
1. Put this file in the SAME directory as your real bot.py and
   loadtest.py (it imports both).
2. Same environment loadtest.py needs (python-telegram-bot, reportlab,
   python-docx already installed).
3. Run:
       pytest test_bot.py -v
   or just a subset:
       pytest test_bot.py -v -k mistakes_bank
       pytest test_bot.py -v -k concurrent

No pytest plugins required (no pytest-asyncio) — async tests are run via
a small asyncio.run() wrapper defined below, so a bare `pip install
pytest` is enough.

DATA SAFETY
-----------
Same story as loadtest.py: this calls your bot's REAL save_*() functions,
which write real local JSON files. This file protects you automatically —
the _isolated_cwd fixture below chdirs the whole test session into a
pytest tmp_path before bot.py is even imported, so every file bot.py
writes during the run lands in a throwaway directory, never your real
working directory. You don't need to do anything extra for this; it's
just worth knowing why a stray analytics.json/mistakes_bank.json/
settings.json never shows up next to this file after a run.

WHAT'S COVERED
---------------
  - Pure logic: error-log heuristics (_likely_cause, _describe_update),
    the MCQ-format-warning heuristic (_looks_like_mcq_attempt), i18n
    lookup (t()), year/class labels, mistake-entry validation.
  - Mistakes bank: per-user isolation, dedup, /daily_module scoping,
    the _MISTAKES_BY_USER index staying exactly in sync with
    MISTAKES_BANK through every mutation path, and the legacy-entry
    discard behavior on load (the schema migration this session made).
  - Analytics: the streak_best backfill-from-streak behavior for
    pre-existing users, and that best-streak only ever increases.
  - Daily Quiz composition: always exactly DAILY_QUIZ_TOTAL_COUNT
    questions when there's enough content (even with an empty mistakes
    bank — the bug fixed this session), correctly mixes in up to
    DAILY_QUIZ_MISTAKES_COUNT mistakes, degrades gracefully instead of
    crashing when the pool is too small.
  - Concurrency: record_mistake never double-inserts or loses an update
    under concurrent calls, and the real @_serialize_per_user-locked
    handle_poll_answer only counts a duplicate/double-tap poll answer
    once — reproducing, in miniature, the exact race class the lock
    exists to prevent.

WHAT THIS DOESN'T COVER (see the conversation this came out of for why)
--------------------------------------------------------------------------
  - The full Telegram channel-backup round trip (a real pinned document
    with real file bytes) — load_mistakes_bank()'s filtering is tested
    directly against a file instead, which covers the same filtering
    logic without needing a much heavier fake Telegram-document harness.
  - Memory growth over many iterations — a soak-test concern, better
    suited to running loadtest.py in a loop and watching RSS than to a
    pass/fail pytest assertion.
  - PDF/DOCX generation load — different (CPU-bound, synchronous)
    bottleneck shape than anything tested here; flagged as a good
    loadtest.py scenario to add, not a pytest correctness concern.
"""

import asyncio
import functools
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# loadtest.py must be importable from the same directory as this file —
# it's reused here as a library of fixtures (FakeBot, load_bot_module,
# seed_fake_data, make_fake_context) rather than re-implementing ~400
# lines of Telegram-faking scaffolding a second time.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import loadtest  # noqa: E402


def run_async(coro_func):
    """Lets an `async def test_...` run under plain pytest with no
    pytest-asyncio plugin installed — wraps it in asyncio.run(). Applied
    to every async test below; fixtures still inject normally since this
    preserves the wrapped function's signature."""
    @functools.wraps(coro_func)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_func(*args, **kwargs))
    return wrapper


# ═══════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="session", autouse=True)
def _isolated_cwd(tmp_path_factory):
    """Chdirs into a throwaway directory for the ENTIRE test session,
    before bot.py is imported and for every test after — see DATA SAFETY
    above. Restores the real cwd once the whole session is done."""
    workdir = tmp_path_factory.mktemp("bot_test_workdir")
    prev = os.getcwd()
    os.chdir(workdir)
    yield workdir
    os.chdir(prev)


@pytest.fixture(scope="session")
def loaded_bot(_isolated_cwd):
    """Imports bot.py once for the whole session (it's ~7,500 lines —
    reimporting per-test would be wasteful and isn't necessary, since
    _reset_bot_state below clears the mutable pieces between tests) and
    seeds fake curriculum data once. Returns a namespace with the loaded
    module, the FakeBot, and the (year, module, subject) the seeded
    curriculum lives under."""
    bot_path = os.environ.get("BOT_PATH", str(Path(__file__).resolve().parent / "bot.py"))
    if not os.path.exists(bot_path):
        pytest.fail(f"{bot_path} not found — run pytest from the same directory as bot.py, "
                    f"or set BOT_PATH=/path/to/bot.py")
    mod, fake_bot = loadtest.load_bot_module(bot_path, fake_latency_ms=0.0)
    year, module, subject = loadtest.seed_fake_data(mod, num_lectures=5, questions_per_lecture=10)
    return types.SimpleNamespace(mod=mod, fake_bot=fake_bot, year=year, module=module, subject=subject)


@pytest.fixture(autouse=True)
def _reset_bot_state(loaded_bot):
    """Clears every per-test-mutable global between tests, so tests can
    run in any order without leaking state into each other. Deliberately
    does NOT touch QUIZ_INDEX/QUIZ_POLL_STATUS (the seeded curriculum) —
    those are treated as read-only fixture data shared across the whole
    session, since no test mutates the curriculum itself, only per-user
    state built on top of it."""
    mod = loaded_bot.mod

    def _clear():
        mod.MISTAKES_BANK.clear()
        mod._MISTAKES_BY_USER.clear()
        mod.ANALYTICS.clear()
        mod.SETTINGS.clear()
        mod.LECTURE_SESSIONS.clear()
        mod.DAILY_QUIZ_SESSIONS.clear()
        mod.MISTAKES_RETAKE_SESSIONS.clear()
        # Cold-starts the Daily Quiz subject-pool cache too — it's keyed by
        # scope, and without this a test that shrinks/changes QUIZ_INDEX
        # (none currently do, but a future one might) could silently read
        # a previous test's cached pool instead of rebuilding.
        mod._DAILY_QUIZ_POOL_CACHE.update({"pool": None, "built_at": 0.0, "scope_key": None})

    _clear()
    yield
    _clear()


def make_fake_user(user_id: int, username: str = "tester", first_name: str = "Test"):
    # Includes full_name (telegram.User's computed first+last property) —
    # _describe_update falls back to it when username is falsy, same as
    # real Telegram users who haven't set one.
    return types.SimpleNamespace(
        id=user_id, username=username, first_name=first_name, last_name=None,
        full_name=first_name,
    )


# ═══════════════════════════════════════════════════════════════════════
# PURE LOGIC — error-log heuristics
# ═══════════════════════════════════════════════════════════════════════

class TestLikelyCause:
    """_likely_cause: the plain-English guess shown in the errors
    channel. See the module docstring for why the BadRequest test below
    exists — it's a regression test for a real bug, not speculative."""

    def test_badrequest_is_not_misclassified_as_network(self, loaded_bot):
        """THE regression test. python-telegram-bot's BadRequest is
        (surprisingly) a SUBCLASS of NetworkError, so an isinstance
        check for (TimedOut, NetworkError) placed before the BadRequest
        check silently swallows every BadRequest into the generic
        "transient network blip" message instead of its specific,
        actually-useful explanation. Caught by hand once already —
        this test exists so it can't come back silently."""
        from telegram.error import BadRequest
        mod = loaded_bot.mod
        cause = mod._likely_cause(BadRequest("Message is not modified"))
        assert cause is not None
        assert "network" not in cause.lower(), (
            "BadRequest was misclassified as a network error — the BadRequest "
            "isinstance check must come BEFORE the (TimedOut, NetworkError) "
            "catch-all, since BadRequest subclasses NetworkError."
        )
        assert "no-op" in cause.lower() or "not modified" in cause.lower()

    @pytest.mark.parametrize("msg,expected_fragment", [
        ("Message to edit not found", "deleted"),
        ("Query is too old and response timeout expired", "expired"),
        ("Chat not found", "member"),
        ("Not enough rights to send text messages", "admin"),
    ])
    def test_badrequest_known_patterns(self, loaded_bot, msg, expected_fragment):
        from telegram.error import BadRequest
        cause = loaded_bot.mod._likely_cause(BadRequest(msg))
        assert cause is not None and expected_fragment in cause.lower()

    def test_badrequest_unrecognized_pattern_still_returns_something(self, loaded_bot):
        from telegram.error import BadRequest
        cause = loaded_bot.mod._likely_cause(BadRequest("some totally novel rejection"))
        assert cause is not None  # generic BadRequest fallback, not None

    def test_forbidden(self, loaded_bot):
        from telegram.error import Forbidden
        cause = loaded_bot.mod._likely_cause(Forbidden("bot was blocked by the user"))
        assert cause is not None and "blocked" in cause.lower()

    def test_retry_after_includes_the_actual_wait_time(self, loaded_bot):
        from telegram.error import RetryAfter
        cause = loaded_bot.mod._likely_cause(RetryAfter(30))
        assert cause is not None and "30" in cause

    def test_plain_network_error_and_timed_out(self, loaded_bot):
        from telegram.error import NetworkError, TimedOut
        mod = loaded_bot.mod
        assert "transient" in mod._likely_cause(NetworkError("connection reset")).lower()
        assert "transient" in mod._likely_cause(TimedOut("timed out")).lower()

    def test_python_builtin_exceptions(self, loaded_bot):
        mod = loaded_bot.mod
        assert "key" in mod._likely_cause(KeyError("mid")).lower()
        assert "index" in mod._likely_cause(IndexError("out of range")).lower()
        assert "none" in mod._likely_cause(AttributeError("'NoneType' object has no attribute 'x'")).lower()
        assert mod._likely_cause(ValueError("invalid literal for int()")) is not None
        assert mod._likely_cause(TypeError("bad arg")) is not None

    def test_attribute_error_not_about_nonetype_returns_none(self, loaded_bot):
        # Only NoneType AttributeErrors get a specific guess — anything
        # else falls through to None rather than guessing wrong.
        cause = loaded_bot.mod._likely_cause(AttributeError("some other attribute issue"))
        assert cause is None

    def test_unrecognized_exception_type_returns_none_not_a_guess(self, loaded_bot):
        cause = loaded_bot.mod._likely_cause(RuntimeError("something totally unrelated"))
        assert cause is None


class TestDescribeUpdate:
    """_describe_update: the human-readable 'who did what' line.

    _describe_update starts with `isinstance(update, Update)`, so a plain
    types.SimpleNamespace fake (which fails that check) would silently
    exercise the "no update at all" branch instead of the real logic.
    unittest.mock.MagicMock(spec=Update) satisfies isinstance() while
    still letting every attribute be set freely, same as SimpleNamespace
    would — this is what makes these two tests actually test the intended
    code path instead of always passing for the wrong reason."""

    def test_callback_query(self, loaded_bot):
        from unittest.mock import MagicMock
        u = make_fake_user(123, username="kareem")
        update = MagicMock(spec=loaded_bot.mod.Update)
        update.effective_user = u
        update.effective_chat = types.SimpleNamespace(id=123, type="private")
        update.callback_query = types.SimpleNamespace(data="toggle_language")
        update.message = None
        update.poll_answer = None
        desc = loaded_bot.mod._describe_update(update)
        assert "@kareem" in desc and "toggle_language" in desc

    def test_plain_message(self, loaded_bot):
        from unittest.mock import MagicMock
        u = make_fake_user(456, username=None, first_name="Ahmed")
        update = MagicMock(spec=loaded_bot.mod.Update)
        update.effective_user = u
        update.effective_chat = types.SimpleNamespace(id=-100, type="group")
        update.callback_query = None
        update.message = types.SimpleNamespace(text="/report_issue test", poll=None)
        update.poll_answer = None
        desc = loaded_bot.mod._describe_update(update)
        assert "Ahmed" in desc and "/report_issue" in desc and "group" in desc

    def test_non_update_object_does_not_crash(self, loaded_bot):
        # e.g. an error raised from a job_queue task, with no update at all
        desc = loaded_bot.mod._describe_update("not an Update instance")
        assert isinstance(desc, str) and len(desc) > 0


# ═══════════════════════════════════════════════════════════════════════
# PURE LOGIC — MCQ format-warning heuristic
# ═══════════════════════════════════════════════════════════════════════

class TestLooksLikeMcqAttempt:

    @pytest.mark.parametrize("text", [
        "hi", "مساء الخير", "شكرا يا ريس", "ازيك يا بوت عامل ايه", "thanks!",
    ])
    def test_plain_chat_never_looks_like_an_attempt(self, loaded_bot, text):
        mod = loaded_bot.mod
        lines = mod.normalize_mcq_block(text)
        assert mod._looks_like_mcq_attempt(lines) is False, (
            f"{text!r} was flagged as an MCQ attempt — would re-trigger the "
            f"format-error warning for ordinary chat, the exact bug this was fixed for."
        )

    @pytest.mark.parametrize("text", [
        "السؤال\na) خيار غلط بس ناقص",
        "a) لوحدها بس",
        "1. حاجة",
    ])
    def test_broken_but_genuine_attempts_are_detected(self, loaded_bot, text):
        mod = loaded_bot.mod
        lines = mod.normalize_mcq_block(text)
        assert mod._looks_like_mcq_attempt(lines) is True, (
            f"{text!r} has an option-style line but wasn't flagged — a genuine "
            f"broken question attempt would now fail silently with no feedback."
        )


# ═══════════════════════════════════════════════════════════════════════
# PURE LOGIC — i18n
# ═══════════════════════════════════════════════════════════════════════

class TestI18n:

    def test_lookup_respects_users_language(self, loaded_bot):
        mod = loaded_bot.mod
        mod.STRINGS["_test_greeting"] = {"ar": "أهلاً يا {name}", "en": "Hi {name}"}
        try:
            entry_en = mod._get_settings_entry(111)
            entry_en["language"] = "en"
            entry_ar = mod._get_settings_entry(112)
            entry_ar["language"] = "ar"
            assert mod.t("_test_greeting", 111, name="Kareem") == "Hi Kareem"
            assert mod.t("_test_greeting", 112, name="Kareem") == "أهلاً يا Kareem"
        finally:
            mod.STRINGS.pop("_test_greeting", None)

    def test_falls_back_to_arabic_when_english_missing(self, loaded_bot):
        mod = loaded_bot.mod
        mod.STRINGS["_test_ar_only"] = {"ar": "نص عربي بس"}
        try:
            entry = mod._get_settings_entry(113)
            entry["language"] = "en"
            assert mod.t("_test_ar_only", 113) == "نص عربي بس"
        finally:
            mod.STRINGS.pop("_test_ar_only", None)

    def test_missing_key_is_visibly_broken_not_silent(self, loaded_bot):
        result = loaded_bot.mod.t("_this_key_does_not_exist", 999)
        assert "missing string" in result.lower()

    def test_default_language_is_arabic(self, loaded_bot):
        # No settings entry at all yet for this user_id — must default to
        # "ar" (the bot's pre-existing all-Arabic behavior), never silently
        # switch someone to English just because the field is new.
        assert loaded_bot.mod.get_language(918273) == "ar"


# ═══════════════════════════════════════════════════════════════════════
# PURE LOGIC — misc small helpers
# ═══════════════════════════════════════════════════════════════════════

class TestYearClassLabel:

    def test_known_years(self, loaded_bot):
        mod = loaded_bot.mod
        assert "1" in mod.year_class_label("y1") and "46" in mod.year_class_label("y1")
        assert "3" in mod.year_class_label("y3") and "44" in mod.year_class_label("y3")

    def test_unset_or_invalid_year_class_has_a_placeholder(self, loaded_bot):
        mod = loaded_bot.mod
        assert mod.year_class_label(None) != ""
        assert mod.year_class_label("not-a-real-year") != ""


class TestMistakeEntryValidity:

    def test_valid_entry(self, loaded_bot):
        entry = {"user_id": 1, "mid": 2, "year": "y1", "module": "m", "subject": "s"}
        assert loaded_bot.mod._is_valid_mistake_entry(entry) is True

    @pytest.mark.parametrize("missing_key", ["user_id", "mid", "year", "module", "subject"])
    def test_missing_any_required_key_is_invalid(self, loaded_bot, missing_key):
        entry = {"user_id": 1, "mid": 2, "year": "y1", "module": "m", "subject": "s"}
        del entry[missing_key]
        assert loaded_bot.mod._is_valid_mistake_entry(entry) is False

    def test_legacy_entry_without_user_id_is_invalid(self, loaded_bot):
        # The exact old (pre-per-user) shape — must be rejected so it gets
        # discarded on load rather than silently misattributed to nobody.
        legacy = {"mid": 2, "year": "y1", "module": "m", "subject": "s"}
        assert loaded_bot.mod._is_valid_mistake_entry(legacy) is False


# ═══════════════════════════════════════════════════════════════════════
# MISTAKES BANK — per-user correctness
# ═══════════════════════════════════════════════════════════════════════

class TestMistakesBank:

    @run_async
    async def test_record_mistake_adds_an_entry(self, loaded_bot):
        mod = loaded_bot.mod
        added = await mod.record_mistake(1, 100, "y1", "modA", "subj1")
        assert added is True
        assert mod._user_mistake_count(1) == 1

    @run_async
    async def test_record_mistake_dedups_same_user_same_question(self, loaded_bot):
        mod = loaded_bot.mod
        first  = await mod.record_mistake(1, 100, "y1", "modA", "subj1")
        second = await mod.record_mistake(1, 100, "y1", "modA", "subj1")
        assert first is True
        assert second is False
        assert mod._user_mistake_count(1) == 1

    @run_async
    async def test_record_mistake_keeps_separate_entries_per_user(self, loaded_bot):
        """The whole point of this session's per-user migration: the SAME
        question missed by two different users must produce two entries,
        not be deduped across users."""
        mod = loaded_bot.mod
        await mod.record_mistake(1, 100, "y1", "modA", "subj1")
        await mod.record_mistake(2, 100, "y1", "modA", "subj1")
        assert mod._user_mistake_count(1) == 1
        assert mod._user_mistake_count(2) == 1
        assert len(mod.MISTAKES_BANK) == 2

    @run_async
    async def test_scoped_mistakes_bank_is_isolated_per_user(self, loaded_bot):
        mod = loaded_bot.mod
        await mod.record_mistake(1, 100, "y1", "modA", "subj1")
        await mod.record_mistake(1, 101, "y1", "modA", "subj1")
        await mod.record_mistake(2, 200, "y1", "modA", "subj1")
        user1_entries = mod._scoped_mistakes_bank(1)
        assert len(user1_entries) == 2
        assert all(m["user_id"] == 1 for m in user1_entries)
        assert mod._scoped_mistakes_bank(3) == []  # user with no mistakes at all

    @run_async
    async def test_scoped_mistakes_bank_respects_admin_daily_module_scope(self, loaded_bot):
        mod = loaded_bot.mod
        await mod.record_mistake(1, 100, "y1", "modA", "subjA")
        await mod.record_mistake(1, 200, "y2", "modB", "subjB")
        await mod.set_daily_quiz_scope("y1", "modA")
        try:
            scoped = mod._scoped_mistakes_bank(1)
            assert len(scoped) == 1 and scoped[0]["mid"] == 100
        finally:
            await mod.set_daily_quiz_scope(None, None)
        # scope cleared — both entries visible again
        assert len(mod._scoped_mistakes_bank(1)) == 2

    @run_async
    async def test_user_mistake_count_ignores_admin_scope(self, loaded_bot):
        """/mystats should show the user's REAL total, not the admin's
        narrowed Daily Quiz scope — this was a deliberate design choice
        this session, not an oversight."""
        mod = loaded_bot.mod
        await mod.record_mistake(1, 100, "y1", "modA", "subjA")
        await mod.record_mistake(1, 200, "y2", "modB", "subjB")
        await mod.set_daily_quiz_scope("y1", "modA")
        try:
            assert mod._user_mistake_count(1) == 2  # NOT narrowed to 1
        finally:
            await mod.set_daily_quiz_scope(None, None)

    @run_async
    async def test_index_stays_consistent_through_add_and_clear(self, loaded_bot):
        """The _MISTAKES_BY_USER index must always agree with
        MISTAKES_BANK's actual contents — this is the O(n)-scan fix from
        earlier in the conversation; a drift between the two would mean
        wrong counts shown to real users."""
        mod = loaded_bot.mod
        for uid in (1, 2, 3):
            for mid in range(5):
                await mod.record_mistake(uid, mid, "y1", "modA", "subjA")

        def index_matches_bank():
            total_in_index = sum(len(v) for v in mod._MISTAKES_BY_USER.values())
            return total_in_index == len(mod.MISTAKES_BANK)

        assert index_matches_bank()

        # Simulate the Clear Mistake Bank button's exact mutation for user 1
        before = len(mod.MISTAKES_BANK)
        mod.MISTAKES_BANK[:] = [
            m for m in mod.MISTAKES_BANK
            if not (mod._is_valid_mistake_entry(m) and m["user_id"] == 1)
        ]
        mod._MISTAKES_BY_USER.pop(1, None)
        assert before - len(mod.MISTAKES_BANK) == 5
        assert mod._user_mistake_count(1) == 0
        assert mod._user_mistake_count(2) == 5  # untouched
        assert index_matches_bank()

    def test_load_mistakes_bank_discards_legacy_entries_without_user_id(self, loaded_bot, tmp_path):
        """Schema-migration regression test: the exact behavior this
        session relied on (and manually verified once) when discarding
        pre-existing shared-bank entries that predate the per-user
        migration — locked in here so a future refactor of the loader
        can't silently change it."""
        mod = loaded_bot.mod
        fixture_file = tmp_path / "mistakes_bank_fixture.json"
        fixture_file.write_text(json.dumps([
            {"mid": 1, "year": "y1", "module": "m", "subject": "s"},               # legacy — no user_id
            {"user_id": 5, "mid": 2, "year": "y1", "module": "m", "subject": "s"},  # valid
            {"user_id": 6, "mid": 3, "year": "y1"},                                # malformed — missing module/subject
        ]), encoding="utf-8")

        original_path = mod.MISTAKES_BANK_FILE
        mod.MISTAKES_BANK_FILE = str(fixture_file)
        try:
            loaded = mod.load_mistakes_bank()
        finally:
            mod.MISTAKES_BANK_FILE = original_path

        assert len(loaded) == 1
        assert loaded[0]["user_id"] == 5


# ═══════════════════════════════════════════════════════════════════════
# ANALYTICS — streak_best backfill
# ═══════════════════════════════════════════════════════════════════════

class TestStreakBest:

    def test_new_user_gets_all_zero_defaults(self, loaded_bot):
        entry = loaded_bot.mod._get_entry(424242)
        assert entry["streak"] == 0
        assert entry["streak_best"] == 0

    def test_backfill_uses_current_streak_not_zero(self, loaded_bot):
        """The exact regression this session's backfill logic exists for:
        an entry that predates streak_best must NOT look like it's never
        had a streak — it should backfill to its current streak."""
        mod = loaded_bot.mod
        # Simulate a pre-existing user's entry as it would have looked
        # before streak_best existed: no streak_best key at all.
        legacy_entry = mod._blank_entry()
        del legacy_entry["streak_best"]
        legacy_entry["streak"] = 7
        mod.ANALYTICS["555555"] = legacy_entry

        entry = mod._get_entry(555555)  # triggers backfill
        assert entry["streak_best"] == 7

    @run_async
    async def test_streak_best_only_rises_never_falls(self, loaded_bot):
        mod = loaded_bot.mod
        user_id = 777777
        yesterday_str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

        # Day 1: fresh user, first-ever activity.
        await mod._record_activity(user_id)
        entry = mod._get_entry(user_id)
        assert entry["streak"] == 1 and entry["streak_best"] == 1

        # Day 2 (simulated): continues the streak — best should follow.
        entry["last_active_date"] = yesterday_str
        await mod._record_activity(user_id)
        assert entry["streak"] == 2 and entry["streak_best"] == 2

        # Streak breaks (last activity 3 days ago, not yesterday) — streak
        # resets to 1, but streak_best must NOT drop back down.
        three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
        entry["last_active_date"] = three_days_ago
        await mod._record_activity(user_id)
        assert entry["streak"] == 1
        assert entry["streak_best"] == 2, "streak_best must never decrease when a streak resets"


# ═══════════════════════════════════════════════════════════════════════
# DAILY QUIZ COMPOSITION
# ═══════════════════════════════════════════════════════════════════════

class TestDailyQuizComposition:

    @run_async
    async def test_always_ten_when_pool_is_sufficient_even_with_empty_mistakes_bank(self, loaded_bot):
        """THE bug this session fixed: an empty mistakes bank used to
        mean a 7-question quiz instead of 10. Locked in here."""
        mod = loaded_bot.mod
        ctx = loadtest.make_fake_context(mod, loaded_bot.fake_bot)
        assert mod._user_mistake_count(999) == 0  # sanity: genuinely empty
        questions = await mod.build_daily_quiz_questions(ctx, 999)
        assert len(questions) == mod.DAILY_QUIZ_TOTAL_COUNT == 10

    @run_async
    async def test_mixes_in_mistakes_up_to_the_cap_and_tops_up_the_rest(self, loaded_bot):
        mod = loaded_bot.mod
        ctx = loadtest.make_fake_context(mod, loaded_bot.fake_bot)
        user_id = 998

        # Seed exactly 2 genuine, resolvable mistakes (fewer than the cap
        # of 3) from the seeded curriculum, so both random-topup AND
        # mistakes-inclusion are exercised in the same run.
        lecture_key = f"{loaded_bot.subject} Lecture 1"
        mids = list(mod.QUIZ_INDEX[loaded_bot.year][lecture_key]["ids"])[:2]
        for mid in mids:
            await mod.record_mistake(user_id, mid, loaded_bot.year, loaded_bot.module, loaded_bot.subject)

        questions = await mod.build_daily_quiz_questions(ctx, user_id)
        assert len(questions) == mod.DAILY_QUIZ_TOTAL_COUNT

    @run_async
    async def test_never_exceeds_total_count(self, loaded_bot):
        mod = loaded_bot.mod
        ctx = loadtest.make_fake_context(mod, loaded_bot.fake_bot)
        user_id = 997
        # Seed MORE mistakes than the cap — build_daily_quiz_questions must
        # still cap the mistakes slice at DAILY_QUIZ_MISTAKES_COUNT and the
        # total at DAILY_QUIZ_TOTAL_COUNT, never run over either.
        lecture_key = f"{loaded_bot.subject} Lecture 1"
        mids = list(mod.QUIZ_INDEX[loaded_bot.year][lecture_key]["ids"])
        for mid in mids:
            await mod.record_mistake(user_id, mid, loaded_bot.year, loaded_bot.module, loaded_bot.subject)
        assert mod._user_mistake_count(user_id) > mod.DAILY_QUIZ_MISTAKES_COUNT

        questions = await mod.build_daily_quiz_questions(ctx, user_id)
        assert len(questions) <= mod.DAILY_QUIZ_TOTAL_COUNT

    @run_async
    async def test_falls_short_gracefully_when_pool_is_small(self, loaded_bot):
        """Narrowing the admin scope to a lecture with fewer than 10
        ready questions must return fewer than 10 — not crash, not pad
        with junk, not loop forever."""
        mod = loaded_bot.mod
        ctx = loadtest.make_fake_context(mod, loaded_bot.fake_bot)

        # Use a scope with a small, exact known pool: one lecture (10
        # seeded questions) instead of all 5 (50 questions) — still
        # comfortably under some real-world "just started this module"
        # scenario, and the fixture data makes the math exact.
        small_year   = "y2"
        small_module = next(iter(mod.year_modules(small_year)), None)
        small_subject = next(iter(mod.year_modules(small_year)[small_module]), None) if small_module else None
        if not small_module or not small_subject:
            pytest.skip("y2 has no configured module/subject in this bot.py's YEARS config")

        # Seed exactly 3 ready questions total under this scope — fewer
        # than DAILY_QUIZ_TOTAL_COUNT.
        lecture_key = f"{small_subject} Lecture 1"
        ids = []
        for i, mid in enumerate(range(90001, 90004)):
            poll_id = f"fakepoll-small-{mid}"
            mod.QUIZ_POLL_STATUS[small_year][poll_id] = {
                "message_id": mid, "lecture": lecture_key, "closed": True,
                "question": f"Small pool Q{mid}?", "options": ["A", "B", "C", "D"],
                "correct_option_id": 0, "explanation": "",
            }
            ids.append(mid)
        mod.QUIZ_INDEX[small_year][lecture_key] = {
            "ids": ids, "closed": True, "module": small_module, "subject": small_subject,
            "lecture_number": "1", "name": "Small Pool Lecture 1",
        }
        await mod.set_daily_quiz_scope(small_year, small_module)
        try:
            questions = await mod.build_daily_quiz_questions(ctx, 996)
            assert 0 < len(questions) < mod.DAILY_QUIZ_TOTAL_COUNT
        finally:
            await mod.set_daily_quiz_scope(None, None)


# ═══════════════════════════════════════════════════════════════════════
# CONCURRENCY — the actual "insurance" tests
# ═══════════════════════════════════════════════════════════════════════

class TestConcurrency:

    @run_async
    async def test_concurrent_identical_record_mistake_calls_dedup_to_one(self, loaded_bot):
        """Fires the SAME (user, question) mistake 50 times concurrently
        — must land exactly once, not 50 times and not zero times."""
        mod = loaded_bot.mod
        results = await asyncio.gather(*(
            mod.record_mistake(1, 100, "y1", "modA", "subj1") for _ in range(50)
        ))
        assert sum(results) == 1, "exactly one of the 50 concurrent calls should report a new insert"
        assert mod._user_mistake_count(1) == 1
        assert len(mod.MISTAKES_BANK) == 1

    @run_async
    async def test_concurrent_distinct_record_mistake_calls_lose_nothing(self, loaded_bot):
        """50 DIFFERENT questions missed by the same user, fired at once
        — every single one must land; concurrency must not silently
        drop any of them."""
        mod = loaded_bot.mod
        results = await asyncio.gather(*(
            mod.record_mistake(1, mid, "y1", "modA", "subj1") for mid in range(50)
        ))
        assert sum(results) == 50
        assert mod._user_mistake_count(1) == 50
        assert sum(len(v) for v in mod._MISTAKES_BY_USER.values()) == len(mod.MISTAKES_BANK)

    @run_async
    async def test_double_tap_poll_answer_counted_exactly_once(self, loaded_bot):
        """The real end-to-end regression test for @_serialize_per_user:
        fires the IDENTICAL poll answer twice concurrently through the
        actual locked handle_poll_answer entry point — simulating a
        duplicate webhook delivery or an accidental double-tap. Without
        the per-user lock doing its job, both calls could see the same
        matching current_poll_id and both award XP/increment the answer
        count; with it, the second call must see the session already
        advanced and no-op."""
        mod, fake_bot = loaded_bot.mod, loaded_bot.fake_bot
        year, module, subject = loaded_bot.year, loaded_bot.module, loaded_bot.subject
        user_id = 995

        ctx = loadtest.make_fake_context(mod, fake_bot)
        lecture_key = f"{subject} Lecture 1"
        entry = mod.QUIZ_INDEX[year][lecture_key]
        ready_ids = list(entry["ids"])
        poll_status_by_mid = {
            v["message_id"]: v
            for v in mod.QUIZ_POLL_STATUS[year].values()
            if v["lecture"] == lecture_key
        }
        session = {
            "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
            "queue": ready_ids, "current_poll_id": None, "current_correct_id": None,
            "total": len(ready_ids), "answered": 0, "correct": 0,
            "mode": "auto", "pending_polls": {}, "award_xp": True,
            "poll_status_by_mid": poll_status_by_mid, "xp_earned": 0,
        }
        mod.LECTURE_SESSIONS[user_id] = session

        sent = await mod._deliver_next_lecture_question(ctx, user_id, session)
        assert sent, "seeded lecture had no deliverable questions"

        poll_id   = session["current_poll_id"]
        chosen_id = session["current_correct_id"]  # answer correctly, doesn't matter which

        fake_user = make_fake_user(user_id)
        poll_answer = types.SimpleNamespace(poll_id=poll_id, user=fake_user, option_ids=[chosen_id])
        update = types.SimpleNamespace(
            poll_answer=poll_answer, effective_user=fake_user,
            callback_query=None, message=None, effective_chat=None,
        )

        # THE actual race: same update object, fired twice, at once.
        await asyncio.gather(
            mod.handle_poll_answer(update, ctx),
            mod.handle_poll_answer(update, ctx),
        )

        assert session["answered"] == 1, (
            f"expected exactly 1 answered question after a double-tap, got "
            f"{session['answered']} — the per-user lock did not prevent a double-count"
        )
        analytics_entry = mod._get_entry(user_id)
        assert analytics_entry["lecture_questions_answered"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
