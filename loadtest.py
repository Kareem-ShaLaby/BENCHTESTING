#!/usr/bin/env python3
"""
loadtest.py — hot-path latency AND correctness test for The Quizician bot.

WHAT THIS DOES
---------------
Imports your actual bot.py (with every Telegram network call replaced by
an in-memory fake that responds instantly, or after a configurable fake
delay) and exercises it two different ways:

  1. PERFORMANCE scenarios (poll_answer, daily_quiz) — fire N simulated
     concurrent "students" at the hot-path functions and report
     p50/p95/p99 latency. Unchanged in spirit from the original version
     of this script.

  2. CORRECTNESS scenarios (full_lecture, full_daily_quiz, achievements,
     race, sessions) — drive complete, realistic user journeys through
     the REAL entrypoints (handle_poll_answer, not the internal
     _advance_* functions directly) and then check the resulting state
     against independently-recomputed expectations: XP/level
     consistency, whether each achievement tier is exactly the one the
     underlying stat should have unlocked (no skipped tier, no
     over-unlock), the achievement_collector meta-tier + XP multiplier,
     same-user double-tap deduplication (@_serialize_per_user), and the
     LECTURE_SESSIONS/DAILY_QUIZ_SESSIONS/MISTAKES_RETAKE_SESSIONS
     snapshot/restore round trip added for -1004499530524's persistence.

     These don't just check "did it crash" — they check "did it produce
     the RIGHT numbers," which a pure latency test can't tell you. A
     function that silently double-counts XP or skips an achievement
     tier still returns fast.

It does NOT touch Telegram, Railway, or your real channels — everything
that would normally be a network call becomes a fast in-memory stand-in.
That means this measures YOUR CODE'S behavior and latency under
concurrency, not Telegram's API latency or network conditions. That's
deliberate: your code is the thing you can actually change, and it's
usually the actual bottleneck — Telegram's rate limiter (AIORateLimiter,
already in your ApplicationBuilder) governs the network side separately,
and no amount of load-testing here changes what Telegram allows per
second.

HOW TO RUN
----------
1. Copy this file into the SAME directory as your real bot.py (it needs
   to import it, and it needs your bot.py's other files — quiz_index_*,
   analytics.json, etc. — to either exist or be safely creatable; see
   "DATA SAFETY" below).
2. Make sure your normal Python environment (the one with
   python-telegram-bot, reportlab, python-docx already installed) is
   active — this script imports bot.py directly, so it needs everything
   bot.py needs.
3. Run:
       python3 loadtest.py --scenario poll_answer   --users 700
       python3 loadtest.py --scenario daily_quiz    --users 700
       python3 loadtest.py --scenario full_lecture  --users 200
       python3 loadtest.py --scenario full_daily_quiz --users 200
       python3 loadtest.py --scenario achievements  --users 50
       python3 loadtest.py --scenario race          --users 200
       python3 loadtest.py --scenario sessions      --users 50
       python3 loadtest.py --scenario session_abuse
       python3 loadtest.py --scenario mistakes_bank_abuse
       python3 loadtest.py --scenario perf          # both perf scenarios
       python3 loadtest.py --scenario correctness   # every per-user correctness scenario
       python3 loadtest.py --scenario abuse         # both abuse scenarios
       python3 loadtest.py --scenario all           # literally everything (default)
4. Read the p50/p95/p99 numbers for perf scenarios, and the PASS/FAIL +
   violation list for correctness scenarios. See "READING THE RESULTS"
   at the bottom of this file for what to actually do with either.

DATA SAFETY — READ THIS BEFORE RUNNING AGAINST YOUR REAL DATA DIRECTORY
------------------------------------------------------------------------
This script calls your bot's REAL functions, which means it calls the
REAL save_*() functions too (save_analytics, save_settings, etc.) — those
write real local JSON files. It does NOT call any backup_*_to_channel
function for real (those are network calls and get stubbed out), so your
Telegram channel backups are never touched. But your LOCAL .json files
in the working directory WILL be modified with fake load-test data
(fake user IDs, fake XP, fake mistakes-bank entries, fake sessions.json)
unless you run this in an isolated directory.

Recommended: copy bot.py + this script into a throwaway folder with NO
existing .json files, so the bot starts with empty state and everything
it creates is disposable test data. That is what the instructions above
assume. Do NOT run this directly in your production bot's working
directory unless you have a backup and are fine with test data mixing
into your real files.

WHAT GETS FAKED
----------------
- BOT_TOKEN: set to a dummy value so bot.py's os.environ["BOT_TOKEN"]
  doesn't crash on import.
- Application.run_polling: monkeypatched to a no-op BEFORE importing
  bot.py, so importing the file doesn't hang forever trying to connect
  to Telegram (bot.py calls this unconditionally at the bottom of the
  file, with no `if __name__ == "__main__"` guard).
- app.bot: replaced with a FakeBot whose send_message/send_poll/
  send_photo/set_message_reaction/send_document/pin_chat_message/
  stop_poll/copy_messages/etc. all return quickly (optionally with a
  simulated network delay via --fake-latency-ms) instead of calling
  Telegram. FakeBot's __getattr__ catch-all makes any bot.<method>() it
  doesn't explicitly fake raise a loud, named AttributeError instead of
  a confusing failure deep in python-telegram-bot — if a scenario hits
  this, it means bot.py added a new Telegram call this script's FakeBot
  needs a fake for; that's a real finding, not a bug in your bot.
- Channel restore/backup calls (restore_*_from_channel,
  backup_*_to_channel): these still run as real code, but since app.bot
  is fake, every Telegram call inside them hits the FakeBot instead —
  they'll no-op harmlessly (FakeBot.get_chat returns an object with no
  pinned_message, so every restore function sees "nothing to restore"
  and returns immediately, same as a brand-new empty channel would).

WHAT DOESN'T GET FAKED (this is still your real code running)
----------------------------------------------------------------
- All scoring/XP/streak/achievement/level logic
- The mistakes-bank recording and lookup
- The spaced-repetition re-ask logic (disabled per-user in the
  correctness scenarios below, deliberately — see run_full_lecture's
  docstring for why)
- @_serialize_per_user's per-user asyncio.Lock (exercised for real by
  the "race" scenario, which calls handle_poll_answer directly instead
  of the internal _advance_* functions)
- LECTURE_SESSIONS/DAILY_QUIZ_SESSIONS/MISTAKES_RETAKE_SESSIONS
  snapshot/restore, if your bot.py has the SESSION PERSISTENCE section
  (the "sessions" scenario exercises the real functions; it prints a
  skip notice, not a failure, if your bot.py predates that feature)
- The O(1) poll_status_by_mid indexing
- JSON file writes via _atomic_write_json (real disk I/O, on your
  machine — this is deliberate: disk I/O latency under concurrent load
  is a real thing worth measuring, not something to fake away)

HOW ERRORS ARE DETECTED (read this if a run says PASS but you're not sure)
---------------------------------------------------------------------------
Three independent layers, because a bug can hide from any one of them:
  1. Uncaught exceptions propagating out of a scenario — the obvious case.
  2. Printed diagnostics — a lot of this bot's own error handling is
     "print a message and swallow the exception" (by design, so one
     user's bad data can't break another's request). That's the right
     production behavior, but it means a real bug can be completely
     invisible to a test that only watches for raised exceptions. This
     script captures stdout during every scenario and flags any line
     containing "error", "failed", "traceback", or "exception"
     (case-insensitive) as a Printed Diagnostic — these are shown
     separately from hard failures since a few are load-test artifacts
     (e.g. FakeBot.get_file's deliberate RuntimeError, which only fires
     if a restore path actually finds a pinned document — it never
     should here, since FakeChat.pinned_message is always None), but
     most are worth reading.
  3. Invariant violations — independent recomputation of what SHOULD be
     true (XP/level consistency, achievement tier correctness, answered
     == correct + incorrect, no leaked sessions) checked against what
     actually ended up in ANALYTICS/LECTURE_SESSIONS/etc. This is what
     catches "it didn't crash, but the numbers are wrong."
"""

import argparse
import ast
import asyncio
import contextlib
import io
import json
import os
import random
import statistics
import sys
import tempfile
import time
import types
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────
# STEP 1 — prepare the environment BEFORE importing bot.py
# ─────────────────────────────────────────────────────────────────
os.environ.setdefault("BOT_TOKEN", "0000000000:LOADTEST-DUMMY-TOKEN-NOT-REAL")

# Monkeypatch Application.run_polling to a no-op BEFORE bot.py is
# imported, since bot.py calls app.run_polling() unconditionally at
# import time (no __main__ guard). Without this, importing bot.py would
# hang forever trying to long-poll Telegram with a fake token.
import telegram.ext as _tg_ext  # noqa: E402


def _noop_run_polling(self, *a, **k):
    print("[loadtest] app.run_polling() called during import — no-op'd, as expected.")


_tg_ext.Application.run_polling = _noop_run_polling


# ─────────────────────────────────────────────────────────────────
# STEP 2 — the fake Telegram surface
# ─────────────────────────────────────────────────────────────────
def _extract_input_file_bytes(document) -> bytes | None:
    """Best-effort extraction of the raw bytes behind a python-telegram-bot
    InputFile (as constructed by every backup_*_to_channel function via
    InputFile(BytesIO(data), filename=...)). Tries the documented
    attribute first, then falls back to reading the wrapped stream
    directly — PTB's exact internal attribute name has moved between
    versions, so this degrades to None (document tracked, content not)
    rather than raising, if neither works against whatever version is
    actually installed."""
    val = getattr(document, "input_file_content", None)
    if isinstance(val, (bytes, bytearray)):
        return bytes(val)
    obj = getattr(document, "obj", None) or getattr(document, "_obj", None)
    if obj is not None:
        try:
            if hasattr(obj, "seek"):
                obj.seek(0)
            data = obj.read()
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
        except Exception:
            pass
    return None


class _FakeMessage:
    """Stands in for the Message object python-telegram-bot normally
    returns from send_message/send_poll/send_document. Has just enough
    shape for the bot's own code to work with it (.message_id, .poll.id,
    .chat_id, .document/.caption for a message that WAS a document (so
    get_chat().pinned_message can carry real backup content — see
    _FakeChat), and an async .edit_text/.edit_message_reply_markup so the
    "message" parameter some functions accept — e.g. start_daily_quiz's
    edit-in-place path — also works)."""

    _next_id = 1000

    def __init__(self, chat_id, is_poll=False, document=None, caption=None, file_id=None):
        _FakeMessage._next_id += 1
        self.message_id = _FakeMessage._next_id
        self.chat_id = chat_id
        if is_poll:
            self.poll = types.SimpleNamespace(id=f"fakepoll-{self.message_id}")
        if document is not None:
            self.document = types.SimpleNamespace(file_id=file_id, file_name=getattr(document, "filename", None))
            self.caption = caption

    async def edit_text(self, *a, **k):
        return self

    async def edit_message_text(self, *a, **k):
        return self

    async def edit_message_reply_markup(self, *a, **k):
        return self


class _FakeFile:
    """Stands in for the File object bot.get_file() returns — just enough
    for restore_*_from_channel's own `await tg_file.download_as_bytearray()`
    call to work against real stored bytes."""

    def __init__(self, data: bytes):
        self._data = data

    async def download_as_bytearray(self):
        return bytearray(self._data)


class _FakeChat:
    """Stands in for Chat, as returned by bot.get_chat(). Now backed by
    StatefulFakeBot's real per-chat bookkeeping: pinned_message is the
    actual last-pinned _FakeMessage for that chat_id (with real
    .document/.caption), not always None — so restore_*_from_channel
    functions can genuinely find something to restore, the same way they
    would against a real Telegram channel that already has a pinned
    backup. A chat nothing has ever pinned in still correctly reports
    pinned_message=None, matching a brand-new empty channel."""

    def __init__(self, chat_id, pinned_message=None):
        self.id = chat_id
        self.pinned_message = pinned_message


class FakeBot:
    """Replaces app.bot. Every method Telegram would normally serve over
    the network is implemented here as a fast in-memory stand-in, with an
    optional artificial delay (--fake-latency-ms) to approximate real
    network round-trip time if you want more realistic absolute numbers.
    Relative numbers (how latency changes as concurrency increases) are
    meaningful even at zero fake latency, since they isolate your code's
    own scaling behavior from network variance.

    Also tracks real state per chat_id — sent messages, open/closed
    polls, pinned documents (with actual byte content, extracted from the
    InputFile every backup_*_to_channel function builds) — so
    restore_*_from_channel functions and other assertions can check
    against what ACTUALLY got sent/pinned, not just a call count. This is
    what makes the backup_restore scenario possible: previously
    get_chat().pinned_message was always None, so every restore function
    only ever exercised its "nothing to restore" early return.

    Fault injection: pass fail_every=N and/or fail_rate=p to make calls
    periodically/randomly raise a realistic Telegram error instead of
    succeeding (see FAULT_EXCEPTIONS below) — used by the fault_injection
    scenario to check the bot survives real-world flakiness instead of
    only ever seeing a perfectly cooperative network."""

    FAULT_EXCEPTIONS = None   # populated lazily — see _load_fault_exceptions()

    def __init__(self, fake_latency_ms: float = 0.0, fail_every: int | None = None,
                 fail_rate: float = 0.0, fail_methods: set | None = None):
        self.fake_latency_ms = fake_latency_ms
        self.call_counts: dict = {}
        self.fail_every = fail_every        # every Nth call (global, across all methods) raises
        self.fail_rate = fail_rate          # additionally, each call independently has this probability of raising
        self.fail_methods = fail_methods    # None = any method is eligible; else a set of method names
        self._fault_cycle = 0
        # Per-chat state, keyed by chat_id:
        self.sent_messages: dict = {}       # chat_id -> [ {text, kind}, ... ]
        self.polls: dict = {}               # poll_id -> {chat_id, message_id, closed}
        self._pinned_message: dict = {}     # chat_id -> _FakeMessage (the currently pinned one, or absent)
        self._file_store: dict = {}         # file_id -> bytes
        self._messages_by_id: dict = {}     # (chat_id, message_id) -> _FakeMessage, for pin_chat_message to resolve

    def _load_fault_exceptions(self):
        if FakeBot.FAULT_EXCEPTIONS is None:
            from telegram.error import RetryAfter, TimedOut, NetworkError, Forbidden, BadRequest
            FakeBot.FAULT_EXCEPTIONS = [
                lambda: RetryAfter(1),
                lambda: TimedOut(),
                lambda: NetworkError("simulated network error"),
                lambda: Forbidden("simulated — user blocked the bot"),
                lambda: BadRequest("simulated bad request"),
            ]
        return FakeBot.FAULT_EXCEPTIONS

    async def _delay(self, name: str = ""):
        self.call_counts["_total"] = self.call_counts.get("_total", 0) + 1
        self._fault_cycle += 1
        should_fault = False
        if self.fail_methods is None or name in (self.fail_methods or ()):
            if self.fail_every and self._fault_cycle % self.fail_every == 0:
                should_fault = True
            elif self.fail_rate and random.random() < self.fail_rate:
                should_fault = True
        if self.fake_latency_ms:
            await asyncio.sleep(self.fake_latency_ms / 1000)
        if should_fault:
            factory = random.choice(self._load_fault_exceptions())
            raise factory()

    def _count(self, name):
        self.call_counts[name] = self.call_counts.get(name, 0) + 1

    async def send_message(self, chat_id, text=None, **kwargs):
        self._count("send_message")
        await self._delay("send_message")
        self.sent_messages.setdefault(chat_id, []).append({"text": text, "kind": "message"})
        return _FakeMessage(chat_id)

    async def send_poll(self, chat_id, question, options, **kwargs):
        self._count("send_poll")
        await self._delay("send_poll")
        msg = _FakeMessage(chat_id, is_poll=True)
        self.polls[msg.poll.id] = {"chat_id": chat_id, "message_id": msg.message_id, "closed": False}
        return msg

    async def send_photo(self, chat_id, photo=None, **kwargs):
        self._count("send_photo")
        await self._delay("send_photo")
        return _FakeMessage(chat_id)

    async def send_document(self, chat_id, document=None, caption=None, **kwargs):
        self._count("send_document")
        await self._delay("send_document")
        content = _extract_input_file_bytes(document) if document is not None else None
        file_id = f"fakefile-{_FakeMessage._next_id + 1}"
        msg = _FakeMessage(chat_id, document=document, caption=caption, file_id=file_id)
        if content is not None:
            self._file_store[file_id] = content
        self._messages_by_id[(chat_id, msg.message_id)] = msg
        return msg

    async def set_message_reaction(self, chat_id, message_id, **kwargs):
        self._count("set_message_reaction")
        await self._delay("set_message_reaction")
        return True

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self._count("pin_chat_message")
        await self._delay("pin_chat_message")
        msg = self._messages_by_id.get((chat_id, message_id))
        if msg is not None:
            self._pinned_message[chat_id] = msg
        return True

    async def unpin_chat_message(self, chat_id, **kwargs):
        self._count("unpin_chat_message")
        await self._delay("unpin_chat_message")
        self._pinned_message.pop(chat_id, None)
        return True

    async def delete_message(self, chat_id, message_id, **kwargs):
        self._count("delete_message")
        await self._delay("delete_message")
        pinned = self._pinned_message.get(chat_id)
        if pinned is not None and pinned.message_id == message_id:
            self._pinned_message.pop(chat_id, None)
        return True

    async def stop_poll(self, chat_id, message_id, **kwargs):
        self._count("stop_poll")
        await self._delay("stop_poll")
        for pid, p in self.polls.items():
            if p["chat_id"] == chat_id and p["message_id"] == message_id:
                p["closed"] = True
        return types.SimpleNamespace(id=f"fakepoll-stopped-{message_id}")

    async def copy_messages(self, chat_id, from_chat_id, message_ids, **kwargs):
        self._count("copy_messages")
        await self._delay("copy_messages")
        return [types.SimpleNamespace(message_id=m) for m in message_ids]

    async def edit_message_text(self, chat_id, message_id, text=None, **kwargs):
        self._count("edit_message_text")
        await self._delay("edit_message_text")
        return _FakeMessage(chat_id)

    async def edit_message_reply_markup(self, chat_id, message_id, **kwargs):
        self._count("edit_message_reply_markup")
        await self._delay("edit_message_reply_markup")
        return _FakeMessage(chat_id)

    async def get_chat(self, chat_id, **kwargs):
        self._count("get_chat")
        await self._delay("get_chat")
        return _FakeChat(chat_id, pinned_message=self._pinned_message.get(chat_id))

    async def get_file(self, file_id, **kwargs):
        self._count("get_file")
        await self._delay("get_file")
        data = self._file_store.get(file_id)
        if data is None:
            raise RuntimeError(f"FakeBot.get_file: no stored content for file_id={file_id!r}")
        return _FakeFile(data)

    async def forward_message(self, *a, **kwargs):
        self._count("forward_message")
        await self._delay("forward_message")
        return _FakeMessage(kwargs.get("chat_id"))

    # Catch-all so a bot-code path calling some Telegram method this
    # fake doesn't explicitly implement fails LOUDLY and NAMED, instead
    # of a confusing AttributeError deep in python-telegram-bot's own
    # code. If you hit this, add the method above following the same
    # pattern as the others — and treat hitting it at all as a genuine
    # finding: it means this script's FakeBot coverage has fallen behind
    # bot.py's actual Telegram usage.
    def __getattr__(self, name):
        raise AttributeError(
            f"FakeBot has no fake implementation of bot.{name}() yet — "
            f"add one in loadtest.py's FakeBot class, following the pattern "
            f"of the other methods there."
        )


# ─────────────────────────────────────────────────────────────────
# STEP 2b — fake Update/PollAnswer objects, for driving REAL entrypoints
# ─────────────────────────────────────────────────────────────────
# The original version of this script called _advance_lecture_session /
# _advance_daily_quiz_session directly — real logic, but bypassing
# handle_poll_answer's own routing (current_poll_id / pending_polls
# matching, the "no session — did the bot restart?" fallback, spaced
# repetition dispatch) AND @_serialize_per_user's per-user lock entirely.
# A race-condition bug in either of those would be invisible to a test
# that never goes through handle_poll_answer. The correctness scenarios
# below go through it for real; the original perf scenarios are kept
# calling the internal functions directly, since that's still the right
# choice for isolating raw per-answer latency from routing overhead.
class _FakePollAnswerObj:
    def __init__(self, poll_id: str, user_id: int, option_ids: list):
        self.poll_id = poll_id
        self.user = types.SimpleNamespace(
            id=user_id, first_name="Load", last_name="Test", username=f"loadtest{user_id}",
        )
        self.option_ids = option_ids


class _FakeUpdate:
    """Minimal stand-in for telegram.Update — just enough shape for
    handle_poll_answer and @_serialize_per_user (update.effective_user,
    update.poll_answer)."""

    def __init__(self, poll_id: str, user_id: int, option_ids: list):
        self.poll_answer = _FakePollAnswerObj(poll_id, user_id, option_ids)
        self.effective_user = self.poll_answer.user


async def answer_poll(mod, ctx, user_id: int, poll_id, option_id) -> None:
    """Simulates a real Telegram PollAnswerHandler firing: builds a fake
    Update and calls the bot's REAL handle_poll_answer, lock and all."""
    update = _FakeUpdate(poll_id, user_id, [option_id] if option_id is not None else [])
    await mod.handle_poll_answer(update, ctx)


# ─────────────────────────────────────────────────────────────────
# STEP 2c — capture stdout so swallowed-exception prints aren't invisible
# ─────────────────────────────────────────────────────────────────
_SUSPICIOUS_MARKERS = ("error", "failed", "traceback", "exception")


@contextlib.contextmanager
def capture_diagnostics():
    """Captures everything printed during the `with` block and returns
    (after the block exits) the subset of lines that look like an error
    the bot swallowed internally rather than raised. Doesn't touch
    sys.stderr — uncaught exceptions still propagate and fail the
    scenario normally; this is purely for the "printed, not raised"
    class of bug this bot's many `except Exception: print(...)` blocks
    can otherwise hide from a test."""
    buf = io.StringIO()
    holder = {"lines": []}
    with contextlib.redirect_stdout(buf):
        yield holder
    for line in buf.getvalue().splitlines():
        if line.startswith("[loadtest]"):
            continue  # our own progress/status lines, not the bot's
        if any(marker in line.lower() for marker in _SUSPICIOUS_MARKERS):
            holder["lines"].append(line)


# ─────────────────────────────────────────────────────────────────
# STEP 3 — import the real bot module with the fakes in place
# ─────────────────────────────────────────────────────────────────
def load_bot_module(bot_path: str, fake_latency_ms: float):
    """Imports bot.py as a module named 'quizician_bot_under_test', with
    app.bot swapped for a FakeBot right after import (import itself
    triggers app = ApplicationBuilder()...build(), which is safe — no
    network call happens until run_polling, which we already no-op'd
    above)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("quizician_bot_under_test", bot_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["quizician_bot_under_test"] = mod
    spec.loader.exec_module(mod)  # this line runs your entire bot.py, top to bottom

    fake_bot = FakeBot(fake_latency_ms=fake_latency_ms)
    mod.app.bot = fake_bot
    return mod, fake_bot


# ─────────────────────────────────────────────────────────────────
# STEP 4 — seed enough fake curriculum data for the scenarios to run
# ─────────────────────────────────────────────────────────────────
def seed_fake_data(mod, num_lectures: int = 5, questions_per_lecture: int = 10):
    """Populates QUIZ_INDEX/QUIZ_POLL_STATUS for year 'y1' with fake
    closed lectures + fake poll content, so build_daily_quiz_questions
    and the lecture-delivery path have real (if fabricated) data to work
    with — without this, every scenario would just find an empty pool
    and return instantly, which measures nothing.

    Every fake question has exactly 4 options (Option A-D) with a random
    correct_option_id — scenarios that need a deliberately-wrong answer
    pick (correct_option_id + 1) % 4, which is always a valid, distinct,
    definitely-wrong option given that shape."""
    year = "y1"
    module = next(iter(mod.year_modules(year)), None)
    if module is None:
        raise RuntimeError(
            "No modules configured for year 'y1' in bot.py's YEARS dict — "
            "the load test needs at least one real module/subject name from "
            "your actual curriculum config to build fake lectures under. "
            "Check bot.py's YEARS setup near the top of the file."
        )
    subject = next(iter(mod.year_modules(year)[module]), None)
    if subject is None:
        raise RuntimeError(f"Module {module!r} in year 'y1' has no subjects configured.")

    mid_counter = 1
    for lec_num in range(1, num_lectures + 1):
        lecture_key = f"{subject} Lecture {lec_num}"
        ids = []
        for q_num in range(questions_per_lecture):
            mid = mid_counter
            mid_counter += 1
            ids.append(mid)
            poll_id = f"fakepoll-seed-{mid}"
            mod.QUIZ_POLL_STATUS[year][poll_id] = {
                "message_id": mid,
                "lecture": lecture_key,
                "closed": True,
                "question": f"Fake question {mid} for {subject} Lecture {lec_num}?",
                "options": ["Option A", "Option B", "Option C", "Option D"],
                "correct_option_id": random.randint(0, 3),
                "explanation": "Fake explanation text.",
            }
        mod.QUIZ_INDEX[year][lecture_key] = {
            "ids": ids,
            "closed": True,
            "module": module,
            "subject": subject,
            "lecture_number": str(lec_num),
            "name": f"Fake Lecture {lec_num}",
        }
    print(f"[loadtest] Seeded {num_lectures} fake lectures "
          f"({num_lectures * questions_per_lecture} questions) under "
          f"year={year} module={module!r} subject={subject!r}.")
    return year, module, subject


def make_fake_context(mod, fake_bot):
    """A minimal stand-in for ContextTypes.DEFAULT_TYPE. Every function
    under test only ever touches context.bot and (in a couple of places)
    context.application — both provided here."""
    ctx = types.SimpleNamespace()
    ctx.bot = fake_bot
    ctx.application = mod.app
    return ctx


def _prep_user_settings(mod, user_id: int, *, auto_next: bool = True, spaced_repetition: bool = False) -> None:
    """Common setup every correctness scenario needs: a valid year_class
    (WITHOUT this, start_daily_quiz just prompts for one and never builds
    a session at all — see the "THE YEAR_CLASS BUG" note in
    scenario_daily_quiz below, which is exactly this mistake), and
    spaced_repetition OFF by default so a deterministic-looking driving
    loop (answer current_poll_id, expect the next one) isn't quietly
    interrupted by an unrelated re-ask poll. Spaced repetition has its
    own real code and isn't disabled anywhere except in these test
    fixtures — this is a test-harness simplification, not a claim that
    SR itself doesn't need its own testing."""
    entry = mod._get_settings_entry(user_id)
    entry["year_class"] = "y1"
    entry["auto_next"] = auto_next
    entry["spaced_repetition"] = spaced_repetition


# ─────────────────────────────────────────────────────────────────
# STEP 5 — PERFORMANCE scenarios (unchanged in spirit from the original
# version of this script — internal functions called directly, to
# isolate per-answer hot-path latency from routing/lock overhead)
# ─────────────────────────────────────────────────────────────────
@dataclass
class RunResult:
    latencies_ms: list = field(default_factory=list)
    errors: list = field(default_factory=list)


async def scenario_poll_answer(mod, fake_bot, user_id: int, year: str, module: str, subject: str) -> float:
    """One simulated student answering ONE lecture question, start to
    finish: builds a real LECTURE_SESSIONS entry (same shape
    start_lecture_cmd would create), delivers the first question, then
    answers it — running the REAL _advance_lecture_session code path,
    including XP, mistakes-bank recording on a wrong answer, and the
    spaced-repetition check. Returns the latency (ms) of the answer step
    specifically (the part a real user actually waits on after tapping a
    poll option), not the initial question-delivery step."""
    ctx = make_fake_context(mod, fake_bot)
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
    if not sent:
        raise RuntimeError("Seeded lecture had no deliverable questions — check seed_fake_data.")

    mid = session.get("current_mid")
    is_correct = random.random() < 0.7  # ~30% wrong, so mistakes-bank/spaced-rep code paths get exercised

    t0 = time.perf_counter()
    await mod._advance_lecture_session(
        ctx, user_id, session, is_correct,
        session.get("current_message_id"), mid,
    )
    t1 = time.perf_counter()

    mod.LECTURE_SESSIONS.pop(user_id, None)
    return (t1 - t0) * 1000


async def scenario_daily_quiz(mod, fake_bot, user_id: int) -> float:
    """One simulated student tapping the 💥Daily Quiz💥 button: runs the
    REAL build_daily_quiz_questions plus start_daily_quiz's setup work.

    THE YEAR_CLASS BUG (fixed here): a fake user's SETTINGS entry starts
    with year_class=None (see _blank_settings_entry), and start_daily_quiz
    checks that FIRST — if it's not set, it just sends the "pick your
    year/class" prompt and returns immediately, WITHOUT ever calling
    get_daily_quiz_questions/build_daily_quiz_questions. The previous
    version of this scenario never set year_class, so every "daily_quiz"
    perf run was silently timing that instant prompt-and-return path, not
    the expensive question-build path the docstring claimed to measure —
    it would never have caught a regression in build_daily_quiz_questions
    at all. _prep_user_settings fixes this by setting year_class="y1"
    before every run."""
    ctx = make_fake_context(mod, fake_bot)
    _prep_user_settings(mod, user_id)
    # Reset this user's daily-quiz gate so every simulated tap actually
    # runs the full build, instead of hitting the "already did it today"
    # short-circuit after the first call.
    entry = mod._get_settings_entry(user_id)
    entry["daily_quiz_last_date"] = None

    t0 = time.perf_counter()
    await mod.start_daily_quiz(ctx, user_id)
    t1 = time.perf_counter()

    if user_id not in mod.DAILY_QUIZ_SESSIONS:
        raise RuntimeError(
            "start_daily_quiz did not create a session — check that seed_fake_data "
            "actually populated ready questions for year 'y1', and that year_class "
            "setup above still matches how your bot.py gates this."
        )
    mod.DAILY_QUIZ_SESSIONS.pop(user_id, None)
    return (t1 - t0) * 1000


# ─────────────────────────────────────────────────────────────────
# STEP 5b — CORRECTNESS scenarios
# ─────────────────────────────────────────────────────────────────
@dataclass
class CorrectnessResult:
    violations: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)   # suspicious prints, not necessarily fatal
    errors: list = field(default_factory=list)         # uncaught exceptions
    runs: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations and not self.errors


def verify_analytics_invariants(mod, user_id: int) -> list:
    """Recomputes what each ACHIEVEMENTS tier and the achievement_collector
    meta-tier SHOULD be, purely from the entry's own stat fields (reading
    the bot's own ACHIEVEMENTS/ACHIEVEMENT_STAT_FIELD threshold tables,
    not re-deriving the trigger logic), and flags any mismatch against
    what's actually stored — plus a handful of structural invariants that
    should hold for ANY user's entry no matter what path produced it."""
    violations = []
    entry = mod.ANALYTICS.get(str(user_id))
    if entry is None:
        return [f"user {user_id}: no analytics entry found"]

    answered  = entry.get("lecture_questions_answered", 0)
    correct   = entry.get("lecture_questions_correct", 0)
    incorrect = entry.get("lecture_questions_incorrect", 0)
    if answered != correct + incorrect:
        violations.append(
            f"user {user_id}: lecture_questions_answered ({answered}) != "
            f"lecture_questions_correct ({correct}) + lecture_questions_incorrect ({incorrect})"
        )

    xp, level = entry.get("xp", 0), entry.get("level", 0)
    if xp < 0:
        violations.append(f"user {user_id}: negative xp ({xp})")
    expected_level = mod._xp_to_level(xp)
    if level != expected_level:
        violations.append(
            f"user {user_id}: level ({level}) != _xp_to_level(xp) ({expected_level}) for xp={xp}"
        )

    ach = entry.get("achievements", {})
    for stat_key, tiers in mod.ACHIEVEMENTS.items():
        if stat_key == "achievement_collector":
            continue   # meta-category, checked separately below
        field_name = mod.ACHIEVEMENT_STAT_FIELD.get(stat_key, stat_key)
        actual_tier = ach.get(stat_key, 0)
        expected_tier = 0
        for i, tier_def in enumerate(tiers):
            threshold = tier_def[0]
            tier = i + 1
            value = xp if (stat_key == "xp_levels" and i == 0) else entry.get(field_name, 0)
            if value >= threshold:
                expected_tier = tier
            else:
                break
        if actual_tier != expected_tier:
            check_value = xp if stat_key == "xp_levels" else entry.get(field_name, 0)
            violations.append(
                f"user {user_id}: '{stat_key}' achievement tier is {actual_tier}, "
                f"expected {expected_tier} (stat value: {check_value})"
            )
        if actual_tier > len(tiers):
            violations.append(
                f"user {user_id}: '{stat_key}' tier {actual_tier} exceeds its {len(tiers)} defined tiers"
            )

    total_unlocked = mod._total_achievements_unlocked(entry)
    ac_tiers = mod.ACHIEVEMENTS["achievement_collector"]
    expected_ac_tier, expected_multiplier = 0, 1.0
    for i, tier_def in enumerate(ac_tiers):
        threshold, multiplier = tier_def[0], tier_def[2]
        if total_unlocked >= threshold:
            expected_ac_tier, expected_multiplier = i + 1, multiplier
        else:
            break
    actual_ac_tier = ach.get("achievement_collector", 0)
    if actual_ac_tier != expected_ac_tier:
        violations.append(
            f"user {user_id}: achievement_collector tier is {actual_ac_tier}, "
            f"expected {expected_ac_tier} (total other achievements unlocked: {total_unlocked})"
        )
    actual_multiplier = entry.get("xp_multiplier", 1.0)
    if actual_ac_tier > 0 and actual_multiplier != expected_multiplier:
        violations.append(
            f"user {user_id}: xp_multiplier is {actual_multiplier}, expected {expected_multiplier} "
            f"for achievement_collector tier {actual_ac_tier}"
        )
    elif actual_ac_tier == 0 and actual_multiplier != 1.0:
        violations.append(
            f"user {user_id}: xp_multiplier is {actual_multiplier} but no achievement_collector "
            f"tier is unlocked — multiplier should still be the default 1.0"
        )

    return violations


def verify_no_leaked_sessions(mod, user_id: int) -> list:
    """A user who FINISHED a lecture/daily-quiz/retake session shouldn't
    still have an entry in the corresponding in-memory dict — that's
    exactly the kind of slow leak STALE_SESSION_IDLE_SECONDS' own comment
    warns is possible if something forgets to pop one."""
    violations = []
    for name, sessions in (
        ("LECTURE_SESSIONS", mod.LECTURE_SESSIONS),
        ("DAILY_QUIZ_SESSIONS", mod.DAILY_QUIZ_SESSIONS),
        ("MISTAKES_RETAKE_SESSIONS", mod.MISTAKES_RETAKE_SESSIONS),
    ):
        if user_id in sessions:
            violations.append(f"user {user_id}: still has a leftover entry in {name} after finishing")
    return violations


async def run_full_lecture(mod, fake_bot, user_id: int, year: str, module: str, subject: str,
                            lecture_key: str, correct_rate: float = 0.85, auto_next: bool = True) -> dict:
    """Drives ONE complete lecture, start to finish, through the REAL
    handle_poll_answer entrypoint (not _advance_lecture_session directly)
    for every question — so routing, current_poll_id/pending_polls
    matching, and @_serialize_per_user all run for real, same as an
    actual student tapping through a quiz. Returns what the driver itself
    expects (answered/correct counts), for the caller to check against
    what ANALYTICS actually ended up recording.

    Spaced repetition is turned OFF for this session (see
    _prep_user_settings) specifically so this loop's "answer
    current_poll_id, expect exactly the next fresh question" structure
    stays valid — a re-ask would otherwise inject an extra poll this
    driver doesn't know how to route without also duplicating SR's own
    trigger logic here. Auto-next mode drives one question at a time;
    batch mode (auto_next=False) sends the whole lecture up front, then
    answers every pending poll in the order they were delivered."""
    ctx = make_fake_context(mod, fake_bot)
    _prep_user_settings(mod, user_id, auto_next=auto_next, spaced_repetition=False)

    lecture_entry = mod.QUIZ_INDEX[year][lecture_key]
    ready_ids = list(lecture_entry["ids"])
    poll_status_by_mid = {
        v["message_id"]: v for v in mod.QUIZ_POLL_STATUS[year].values() if v["lecture"] == lecture_key
    }
    already_attempted = str(user_id) in mod._get_lecture_results(mod._lr_key(year, lecture_key))
    session = {
        "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
        "queue": ready_ids, "current_poll_id": None, "current_correct_id": None,
        "total": len(ready_ids), "answered": 0, "correct": 0,
        "mode": "auto" if auto_next else "batch",
        "pending_polls": {}, "award_xp": not already_attempted,
        "poll_status_by_mid": poll_status_by_mid, "xp_earned": 0,
        "started_at": time.time(),
    }
    mod.LECTURE_SESSIONS[user_id] = session

    expected_answered = expected_correct = 0
    last_poll_id = None

    if auto_next:
        sent = await mod._deliver_next_lecture_question(ctx, user_id, session)
        while sent:
            live = mod.LECTURE_SESSIONS.get(user_id)
            if live is None:
                break   # finished already (shouldn't happen right after a successful delivery, but be defensive)
            poll_id, correct_opt = live["current_poll_id"], live["current_correct_id"]
            is_correct = random.random() < correct_rate
            chosen = correct_opt if is_correct else (correct_opt + 1) % 4
            expected_answered += 1
            expected_correct += 1 if is_correct else 0
            last_poll_id = poll_id
            await answer_poll(mod, ctx, user_id, poll_id, chosen)
            live = mod.LECTURE_SESSIONS.get(user_id)
            sent = live is not None and live.get("current_poll_id") is not None
    else:
        await mod._deliver_all_lecture_questions(ctx, user_id, session)
        pending_snapshot = list(session["pending_polls"].items())
        for poll_id, (correct_opt, _message_id, _mid, _delivered_at) in pending_snapshot:
            live = mod.LECTURE_SESSIONS.get(user_id)
            if live is None or poll_id not in live.get("pending_polls", {}):
                continue   # already resolved somehow — be defensive rather than raise mid-loop
            is_correct = random.random() < correct_rate
            chosen = correct_opt if is_correct else (correct_opt + 1) % 4
            expected_answered += 1
            expected_correct += 1 if is_correct else 0
            last_poll_id = poll_id
            await answer_poll(mod, ctx, user_id, poll_id, chosen)

    return {
        "expected_answered": expected_answered,
        "expected_correct": expected_correct,
        "finished": user_id not in mod.LECTURE_SESSIONS,
        "last_poll_id": last_poll_id,   # the REAL final poll_id — see scenario_session_abuse's replay check
    }


async def scenario_full_lecture(mod, fake_bot, user_id: int, year: str, module: str, subject: str) -> CorrectnessResult:
    """Correctness check for ONE full lecture: drives it to completion,
    then verifies the resulting ANALYTICS deltas match what was actually
    fed in, plus every achievement-tier/XP/level invariant."""
    result = CorrectnessResult(runs=1)
    lecture_key = f"{subject} Lecture 1"
    before_entry = dict(mod._get_entry(user_id))

    try:
        with capture_diagnostics() as diag:
            outcome = await run_full_lecture(mod, fake_bot, user_id, year, module, subject, lecture_key)
        result.diagnostics.extend(diag["lines"])
    except Exception as e:
        result.errors.append(f"user {user_id}: {type(e).__name__}: {e}")
        return result

    if not outcome["finished"]:
        result.violations.append(f"user {user_id}: lecture never finished (session still open)")

    after_entry = mod._get_entry(user_id)
    delta_answered = after_entry.get("lecture_questions_answered", 0) - before_entry.get("lecture_questions_answered", 0)
    delta_correct  = after_entry.get("lecture_questions_correct", 0) - before_entry.get("lecture_questions_correct", 0)
    if delta_answered != outcome["expected_answered"]:
        result.violations.append(
            f"user {user_id}: lecture_questions_answered increased by {delta_answered}, "
            f"expected {outcome['expected_answered']}"
        )
    if delta_correct != outcome["expected_correct"]:
        result.violations.append(
            f"user {user_id}: lecture_questions_correct increased by {delta_correct}, "
            f"expected {outcome['expected_correct']}"
        )
    if after_entry.get("lectures_completed", 0) - before_entry.get("lectures_completed", 0) != 1:
        result.violations.append(f"user {user_id}: lectures_completed didn't increase by exactly 1")

    result.violations.extend(verify_analytics_invariants(mod, user_id))
    result.violations.extend(verify_no_leaked_sessions(mod, user_id))
    return result


async def scenario_full_daily_quiz(mod, fake_bot, user_id: int) -> CorrectnessResult:
    """Correctness check for ONE full Daily Quiz run, driven through the
    REAL handle_poll_answer entrypoint for every question, checked the
    same way as scenario_full_lecture."""
    result = CorrectnessResult(runs=1)
    ctx = make_fake_context(mod, fake_bot)
    _prep_user_settings(mod, user_id)
    entry = mod._get_settings_entry(user_id)
    entry["daily_quiz_last_date"] = None
    before_entry = dict(mod._get_entry(user_id))
    before_completed = before_entry.get("daily_quizzes_completed", 0)
    expected_answered = expected_correct = 0

    try:
        with capture_diagnostics() as diag:
            await mod.start_daily_quiz(ctx, user_id)
            if user_id not in mod.DAILY_QUIZ_SESSIONS:
                result.errors.append(
                    f"user {user_id}: start_daily_quiz did not create a session — "
                    f"no ready questions for year 'y1'? check seed_fake_data."
                )
                return result
            session = mod.DAILY_QUIZ_SESSIONS[user_id]
            sent = session.get("current_poll_id") is not None
            while sent:
                live = mod.DAILY_QUIZ_SESSIONS.get(user_id)
                if live is None:
                    break
                poll_id, correct_opt = live["current_poll_id"], live["current_correct_id"]
                is_correct = random.random() < 0.9
                chosen = correct_opt if is_correct else (correct_opt + 1) % 4
                expected_answered += 1
                expected_correct += 1 if is_correct else 0
                await answer_poll(mod, ctx, user_id, poll_id, chosen)
                live = mod.DAILY_QUIZ_SESSIONS.get(user_id)
                sent = live is not None and live.get("current_poll_id") is not None
        result.diagnostics.extend(diag["lines"])
    except Exception as e:
        result.errors.append(f"user {user_id}: {type(e).__name__}: {e}")
        return result

    if user_id in mod.DAILY_QUIZ_SESSIONS:
        result.violations.append(f"user {user_id}: Daily Quiz session never finished (still open)")

    after_entry = mod._get_entry(user_id)
    if after_entry.get("daily_quizzes_completed", 0) - before_completed != 1:
        result.violations.append(f"user {user_id}: daily_quizzes_completed didn't increase by exactly 1")
    delta_answered = after_entry.get("lecture_questions_answered", 0) - before_entry.get("lecture_questions_answered", 0)
    if delta_answered != expected_answered:
        result.violations.append(
            f"user {user_id}: lecture_questions_answered increased by {delta_answered} from the "
            f"Daily Quiz run, expected {expected_answered} (Daily Quiz questions count toward the "
            f"same stat — see _advance_daily_quiz_session's own comment on this)"
        )

    result.violations.extend(verify_analytics_invariants(mod, user_id))
    result.violations.extend(verify_no_leaked_sessions(mod, user_id))
    return result


async def scenario_achievements(mod, fake_bot, user_id: int, year: str, module: str, subject: str,
                                 num_lectures: int) -> CorrectnessResult:
    """Deliberately pushes ONE user through many real lectures plus a
    Daily Quiz, at a high correct rate, specifically to unlock several
    real achievement tiers (not just check the zero-achievement steady
    state) — questions_answered, correct_streak, lectures_completed,
    xp_levels, daily_quiz, and achievement_collector should all have
    fired at least their first tier by the end of this, given enough
    seeded lectures (num_lectures * questions_per_lecture needs to clear
    100 for the first questions_answered tier — see seed_fake_data's
    defaults and this scenario's own --questions-per-lecture/--lectures
    requirements printed if the run comes up short)."""
    result = CorrectnessResult(runs=1)

    try:
        with capture_diagnostics() as diag:
            for lec_num in range(1, num_lectures + 1):
                lecture_key = f"{subject} Lecture {lec_num}"
                if lecture_key not in mod.QUIZ_INDEX[year]:
                    break   # ran out of seeded lectures — use what we got
                outcome = await run_full_lecture(
                    mod, fake_bot, user_id, year, module, subject, lecture_key, correct_rate=0.95,
                )
                if not outcome["finished"]:
                    result.violations.append(f"user {user_id}: lecture {lecture_key} never finished")
            # Also run one Daily Quiz — cheap, and exercises the daily_quiz
            # achievement category, which nothing above touches.
            daily_result = await scenario_full_daily_quiz(mod, fake_bot, user_id)
            result.diagnostics.extend(daily_result.diagnostics)
            result.violations.extend(daily_result.violations)
            result.errors.extend(daily_result.errors)
        result.diagnostics.extend(diag["lines"])
    except Exception as e:
        result.errors.append(f"user {user_id}: {type(e).__name__}: {e}")
        return result

    entry = mod._get_entry(user_id)
    ach = entry.get("achievements", {})
    unlocked_something = any(v for k, v in ach.items() if k != "extras") or any(ach.get("extras", {}).values())
    if not unlocked_something:
        result.violations.append(
            f"user {user_id}: completed {num_lectures} lecture(s) + a Daily Quiz at 95% correct "
            f"and unlocked NOTHING — either the seeded volume is too low to clear tier-1 thresholds "
            f"(check ACHIEVEMENTS' first-tier numbers against num_lectures * questions_per_lecture) "
            f"or achievement-unlocking itself is broken"
        )

    result.violations.extend(verify_analytics_invariants(mod, user_id))
    result.violations.extend(verify_no_leaked_sessions(mod, user_id))
    return result


async def scenario_same_user_race(mod, fake_bot, user_id: int, year: str, module: str, subject: str) -> CorrectnessResult:
    """Fires two concurrent handle_poll_answer calls for the SAME live
    poll from the SAME user — simulating a fast double-tap or a duplicate
    Telegram update. @_serialize_per_user's per-user lock should force
    these to run one after the other; by the time the second acquires the
    lock, current_poll_id has already moved on, so it should see a
    mismatch and no-op. This is checking your EXISTING protection still
    works, not something expected to fail — but it's exactly the kind of
    thing that silently breaks if a future refactor changes which
    functions the decorator wraps, or how the session dict is looked up."""
    result = CorrectnessResult(runs=1)
    ctx = make_fake_context(mod, fake_bot)
    _prep_user_settings(mod, user_id, auto_next=True, spaced_repetition=False)
    lecture_key = f"{subject} Lecture 1"

    try:
        lecture_entry = mod.QUIZ_INDEX[year][lecture_key]
        poll_status_by_mid = {
            v["message_id"]: v for v in mod.QUIZ_POLL_STATUS[year].values() if v["lecture"] == lecture_key
        }
        session = {
            "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
            "queue": list(lecture_entry["ids"]), "current_poll_id": None, "current_correct_id": None,
            "total": len(lecture_entry["ids"]), "answered": 0, "correct": 0,
            "mode": "auto", "pending_polls": {}, "award_xp": True,
            "poll_status_by_mid": poll_status_by_mid, "xp_earned": 0, "started_at": time.time(),
        }
        mod.LECTURE_SESSIONS[user_id] = session

        with capture_diagnostics() as diag:
            await mod._deliver_next_lecture_question(ctx, user_id, session)
            poll_id, correct_opt = session["current_poll_id"], session["current_correct_id"]
            before_answered = mod._get_entry(user_id).get("lecture_questions_answered", 0)

            # Both "taps" fire concurrently on the same event loop — the
            # lock inside handle_poll_answer should serialize them, not
            # let them interleave mid-handler.
            await asyncio.gather(
                answer_poll(mod, ctx, user_id, poll_id, correct_opt),
                answer_poll(mod, ctx, user_id, poll_id, correct_opt),
            )
        result.diagnostics.extend(diag["lines"])

        after_answered = mod._get_entry(user_id).get("lecture_questions_answered", 0)
        delta = after_answered - before_answered
        if delta != 1:
            result.violations.append(
                f"user {user_id}: a double-tap on the same poll advanced "
                f"lecture_questions_answered by {delta}, expected exactly 1 — "
                f"@_serialize_per_user may not be protecting handle_poll_answer correctly"
            )
    except Exception as e:
        result.errors.append(f"user {user_id}: {type(e).__name__}: {e}")
    finally:
        mod.LECTURE_SESSIONS.pop(user_id, None)

    return result


async def scenario_session_persistence(mod, fake_bot, user_id: int, year: str, module: str, subject: str) -> CorrectnessResult:
    """Exercises the SESSION PERSISTENCE feature (LECTURE_SESSIONS /
    DAILY_QUIZ_SESSIONS / MISTAKES_RETAKE_SESSIONS snapshot + restore,
    added for the -1004499530524 channel): builds a live session,
    snapshots it, wipes the in-memory dicts (simulating a restart), and
    checks the restored session round-trips correctly — including int
    keys that had to become strings for JSON and back. Also checks the
    48h staleness cutoff actually drops an artificially-old session
    instead of reviving it, without dropping a fresh one alongside it.

    Skips (not fails) if the loaded bot.py predates this feature, so this
    script stays usable against an older bot.py too."""
    result = CorrectnessResult(runs=1)
    if not hasattr(mod, "_sessions_snapshot"):
        result.diagnostics.append(
            "SESSION PERSISTENCE not present in this bot.py (no _sessions_snapshot) — skipping."
        )
        return result

    lecture_key = f"{subject} Lecture 1"
    lecture_entry = mod.QUIZ_INDEX[year][lecture_key]
    poll_status_by_mid = {
        v["message_id"]: v for v in mod.QUIZ_POLL_STATUS[year].values() if v["lecture"] == lecture_key
    }
    session = {
        "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
        "queue": list(lecture_entry["ids"])[3:], "current_poll_id": "fake-poll-xyz",
        "current_correct_id": 2, "total": len(lecture_entry["ids"]), "answered": 3, "correct": 2,
        "mode": "auto", "pending_polls": {}, "award_xp": True,
        "poll_status_by_mid": poll_status_by_mid, "xp_earned": 45, "started_at": time.time(),
    }
    mod.LECTURE_SESSIONS[user_id] = session
    stale_user_id = user_id + 1

    try:
        snapshot = mod._sessions_snapshot()
        if str(user_id) not in snapshot["lecture_sessions"]:
            result.violations.append(f"user {user_id}: session missing from _sessions_snapshot() output")
            return result

        # Simulate a restart: wipe the live dicts, restore from the snapshot.
        mod.LECTURE_SESSIONS.clear()
        mod.DAILY_QUIZ_SESSIONS.clear()
        mod.MISTAKES_RETAKE_SESSIONS.clear()
        mod._restore_sessions_dict(snapshot)

        restored = mod.LECTURE_SESSIONS.get(user_id)
        if restored is None:
            result.violations.append(f"user {user_id}: session did not survive the snapshot/restore round trip")
            return result

        for key in ("year", "module", "subject", "lecture_key", "current_poll_id",
                    "current_correct_id", "answered", "correct", "xp_earned", "mode", "queue"):
            if restored.get(key) != session.get(key):
                result.violations.append(
                    f"user {user_id}: restored session field {key!r} = {restored.get(key)!r}, "
                    f"expected {session.get(key)!r}"
                )

        restored_keys = set(restored.get("poll_status_by_mid", {}).keys())
        original_keys = set(poll_status_by_mid.keys())
        if restored_keys != original_keys or not all(isinstance(k, int) for k in restored_keys):
            result.violations.append(
                f"user {user_id}: poll_status_by_mid keys didn't round-trip back to int correctly "
                f"(got {restored_keys!r}, expected {original_keys!r} all as int)"
            )

        # Staleness cutoff: an old session should be dropped, a fresh one should not.
        stale_session = dict(session)
        stale_session["started_at"] = time.time() - mod.SESSIONS_MAX_AGE_SECONDS - 3600  # 1h past the cutoff
        mod.LECTURE_SESSIONS[stale_user_id] = stale_session
        stale_snapshot = mod._sessions_snapshot()
        mod.LECTURE_SESSIONS.clear()
        mod._restore_sessions_dict(stale_snapshot)

        if stale_user_id in mod.LECTURE_SESSIONS:
            result.violations.append(
                f"user {stale_user_id}: a session older than SESSIONS_MAX_AGE_SECONDS "
                f"({mod.SESSIONS_MAX_AGE_SECONDS}s) was restored instead of dropped"
            )
        if user_id not in mod.LECTURE_SESSIONS:
            result.violations.append(
                f"user {user_id}: a fresh (non-stale) session was dropped alongside the stale one "
                f"— the staleness check is too aggressive"
            )
    except Exception as e:
        result.errors.append(f"user {user_id}: {type(e).__name__}: {e}")
    finally:
        mod.LECTURE_SESSIONS.pop(user_id, None)
        mod.LECTURE_SESSIONS.pop(stale_user_id, None)

    return result


# ─────────────────────────────────────────────────────────────────
# STEP 5c — ABUSE scenarios
# ─────────────────────────────────────────────────────────────────
# Unlike the correctness scenarios above (one clean, realistic journey
# per simulated user), these deliberately try to break shared state:
# spam, garbage input, duplicate/replayed events, and many users hammering
# the exact same resource at once. Each of these runs ONCE per invocation
# (not once per --correctness-users, the way the scenarios above do) —
# num_users here controls how many simulated attackers/users the abuse
# pattern itself uses, since "abuse" is inherently about many actors
# hitting shared state together, not N independent repeats of one story.

async def scenario_session_abuse(mod, fake_bot, base_user_id: int, num_users: int,
                                  year: str, module: str, subject: str) -> CorrectnessResult:
    """Session-handling abuse: non-JSON-serializable session content (the
    real bug this found — see the SESSION PERSISTENCE fix this prompted),
    high-fanout duplicate-tap spam, garbage poll_id spam against a live
    session, a replayed answer after the session already finished, and a
    soft diagnostic on _user_locks' growth."""
    result = CorrectnessResult(runs=1)
    ctx = make_fake_context(mod, fake_bot)
    lecture_key = f"{subject} Lecture 1"
    victim = base_user_id

    # ── 1. Non-JSON-serializable session content (sr_asked is a real
    # Python set — see _maybe_deliver_spaced_repetition) ───────────────
    if hasattr(mod, "_sessions_snapshot"):
        _prep_user_settings(mod, victim, auto_next=True, spaced_repetition=True)
        lecture_entry = mod.QUIZ_INDEX[year][lecture_key]
        poll_status_by_mid = {
            v["message_id"]: v for v in mod.QUIZ_POLL_STATUS[year].values() if v["lecture"] == lecture_key
        }
        dirty_session = {
            "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
            "queue": list(lecture_entry["ids"]), "current_poll_id": "fake", "current_correct_id": 0,
            "total": len(lecture_entry["ids"]), "answered": 5, "correct": 3,
            "mode": "auto", "pending_polls": {}, "award_xp": True,
            "poll_status_by_mid": poll_status_by_mid, "xp_earned": 60, "started_at": time.time(),
            # The actual shapes _maybe_deliver_spaced_repetition produces:
            "wrong_mids": [lecture_entry["ids"][0], lecture_entry["ids"][1]],
            "sr_pool": [lecture_entry["ids"][1]],
            "sr_asked": {lecture_entry["ids"][0]},   # a real set, same as production
            "sr_counter": 2, "sr_next_threshold": 6,
        }
        mod.LECTURE_SESSIONS[victim] = dirty_session
        try:
            snapshot = mod._sessions_snapshot()
            json.dumps(snapshot)   # this alone reproduces the bug if it's still there
            mod.LECTURE_SESSIONS.clear()
            mod._restore_sessions_dict(snapshot)
            restored = mod.LECTURE_SESSIONS.get(victim)
            if restored is None:
                result.violations.append(f"user {victim}: session with sr_asked (a set) vanished across snapshot/restore")
            elif set(restored.get("sr_asked", [])) != dirty_session["sr_asked"]:
                result.violations.append(
                    f"user {victim}: sr_asked didn't round-trip correctly "
                    f"(got {restored.get('sr_asked')!r}, expected {dirty_session['sr_asked']!r})"
                )
            elif not isinstance(restored.get("sr_asked"), set):
                result.violations.append(
                    f"user {victim}: sr_asked restored as {type(restored.get('sr_asked')).__name__}, "
                    f"expected a set (spaced-repetition code checks membership/adds to it as a set)"
                )
        except TypeError as e:
            result.violations.append(
                f"user {victim}: _sessions_snapshot()/json.dumps raised {e!r} on a session that had used "
                f"spaced repetition — sr_asked is a real Python set (see _maybe_deliver_spaced_repetition), "
                f"which json.dumps cannot serialize. This would fire in production for ANY active user who's "
                f"had a spaced-repetition re-ask, silently breaking session persistence's 30s backup tick."
            )
        except Exception as e:
            result.errors.append(f"user {victim}: {type(e).__name__}: {e}")
        finally:
            mod.LECTURE_SESSIONS.pop(victim, None)
    else:
        result.diagnostics.append("SESSION PERSISTENCE not present in this bot.py — skipping the sr_asked check.")

    # ── 2. High-fanout duplicate-tap spam (same user, same poll, many
    # concurrent copies — a stress version of the 2-way "race" scenario) ──
    _prep_user_settings(mod, victim, auto_next=True, spaced_repetition=False)
    lecture_entry = mod.QUIZ_INDEX[year][lecture_key]
    poll_status_by_mid = {
        v["message_id"]: v for v in mod.QUIZ_POLL_STATUS[year].values() if v["lecture"] == lecture_key
    }
    session = {
        "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
        "queue": list(lecture_entry["ids"]), "current_poll_id": None, "current_correct_id": None,
        "total": len(lecture_entry["ids"]), "answered": 0, "correct": 0,
        "mode": "auto", "pending_polls": {}, "award_xp": True,
        "poll_status_by_mid": poll_status_by_mid, "xp_earned": 0, "started_at": time.time(),
    }
    mod.LECTURE_SESSIONS[victim] = session
    try:
        with capture_diagnostics() as diag:
            await mod._deliver_next_lecture_question(ctx, victim, session)
            poll_id, correct_opt = session["current_poll_id"], session["current_correct_id"]
            before_answered = mod._get_entry(victim).get("lecture_questions_answered", 0)
            fanout = max(5, min(num_users, 30))
            await asyncio.gather(*(
                answer_poll(mod, ctx, victim, poll_id, correct_opt) for _ in range(fanout)
            ))
        result.diagnostics.extend(diag["lines"])
        delta = mod._get_entry(victim).get("lecture_questions_answered", 0) - before_answered
        if delta != 1:
            result.violations.append(
                f"user {victim}: {fanout} concurrent identical taps on one poll advanced "
                f"lecture_questions_answered by {delta}, expected exactly 1"
            )
    except Exception as e:
        result.errors.append(f"user {victim}: {type(e).__name__}: {e}")

    # ── 3. Garbage poll_id spam against a live session ──────────────────
    try:
        live = mod.LECTURE_SESSIONS.get(victim)
        if live is not None:
            real_poll_id = live.get("current_poll_id")
            before_answered = mod._get_entry(victim).get("lecture_questions_answered", 0)
            with capture_diagnostics() as diag:
                await asyncio.gather(*(
                    answer_poll(mod, ctx, victim, f"garbage-poll-{i}-{random.random()}", random.randint(0, 3))
                    for i in range(20)
                ))
            result.diagnostics.extend(diag["lines"])
            after_answered = mod._get_entry(victim).get("lecture_questions_answered", 0)
            if after_answered != before_answered:
                result.violations.append(
                    f"user {victim}: 20 garbage/unknown poll_ids against a live session changed "
                    f"lecture_questions_answered ({before_answered} -> {after_answered}) — should have been "
                    f"ignored entirely (see handle_poll_answer's current_poll_id match)"
                )
            still_live = mod.LECTURE_SESSIONS.get(victim)
            if still_live is None or still_live.get("current_poll_id") != real_poll_id:
                result.violations.append(
                    f"user {victim}: the real in-flight question was disturbed by unrelated garbage "
                    f"poll_id spam (current_poll_id changed from {real_poll_id!r} to "
                    f"{still_live.get('current_poll_id') if still_live else '<session gone>'!r})"
                )
    except Exception as e:
        result.errors.append(f"user {victim}: {type(e).__name__}: {e}")
    finally:
        mod.LECTURE_SESSIONS.pop(victim, None)

    # ── 4. Replayed/late answer for a poll from a session that already
    # finished (e.g. a duplicate Telegram update arriving after the
    # lecture's summary was already sent) ───────────────────────────────
    try:
        outcome = await run_full_lecture(mod, fake_bot, victim, year, module, subject, lecture_key)
        last_poll_id = outcome["last_poll_id"]
        if last_poll_id is None:
            result.errors.append(
                f"user {victim}: run_full_lecture returned no last_poll_id to replay — "
                f"can't exercise this check (the lecture may not have delivered any questions)"
            )
        else:
            # Replay the EXACT poll_id the real final question used — a
            # made-up poll_id would trivially hit the "unknown poll_id"
            # branch and prove nothing about the "session already gone"
            # branch specifically. Re-answering the bot's own real,
            # already-resolved poll_id is what an actual duplicate
            # Telegram update would look like.
            before_completed = mod._get_entry(victim).get("lectures_completed", 0)
            with capture_diagnostics() as diag:
                await answer_poll(mod, ctx, victim, last_poll_id, 0)
            result.diagnostics.extend(diag["lines"])
            after_completed = mod._get_entry(victim).get("lectures_completed", 0)
            if after_completed != before_completed:
                result.violations.append(
                    f"user {victim}: replaying the lecture's real final poll_id after it already "
                    f"finished changed lectures_completed ({before_completed} -> {after_completed}) — "
                    f"should have hit the 'no session' fallback and done nothing"
                )
            if victim in mod.LECTURE_SESSIONS:
                result.violations.append(f"user {victim}: a replayed post-finish answer resurrected a session")
    except Exception as e:
        result.errors.append(f"user {victim}: {type(e).__name__}: {e}")
    finally:
        mod.LECTURE_SESSIONS.pop(victim, None)

    # ── 5. _user_locks growth — soft diagnostic, not a hard violation.
    # Bounded by real distinct users who've ever answered a poll since
    # the process started, not by request volume, so this is cheap even
    # at real-world scale (thousands of students ≈ thousands of tiny
    # Lock objects) — reported for visibility, not flagged as a bug.
    if hasattr(mod, "_user_locks"):
        for i in range(num_users):
            await answer_poll(mod, ctx, base_user_id + 1000 + i, "irrelevant", 0)
        result.diagnostics.append(
            f"_user_locks holds {len(mod._user_locks)} entries after exercising {num_users} distinct "
            f"user ids (informational only — this dict is never pruned, but is bounded by real distinct "
            f"users, not by request volume)."
        )

    return result


async def scenario_mistakes_bank_abuse(mod, fake_bot, base_user_id: int, num_users: int,
                                        year: str, module: str, subject: str) -> CorrectnessResult:
    """Mistakes-bank abuse: repeated-miss dedup under concurrent stress
    (same user, same question, fired many times at once), many different
    users concurrently missing the exact same question (checks
    _MISTAKES_BY_USER doesn't cross-contaminate), a malformed entry
    injected directly into MISTAKES_BANK (bypassing record_mistake
    entirely, the way a bad restore or manual edit could), and a full
    retake driven to completion afterward to make sure none of the above
    left the bank in a state that crashes real usage."""
    result = CorrectnessResult(runs=1)
    ctx = make_fake_context(mod, fake_bot)
    lecture_entry = mod.QUIZ_INDEX[year][f"{subject} Lecture 1"]
    shared_mid = lecture_entry["ids"][0]
    victim = base_user_id
    other_users = [base_user_id + 1 + i for i in range(max(5, min(num_users, 40)))]

    # mistakes_bank.json is loaded from local disk at import time and
    # persists across SEPARATE runs of this script in the same folder —
    # unlike the perf/correctness scenarios, this one uses a fixed
    # user-id range (base_user_id), so a second run in the same directory
    # would otherwise find these ids' entries already recorded from last
    # time, and every dedup check below would correctly (but confusingly)
    # report "0 new entries" — not because dedup is broken, but because
    # there was nothing new left to record. Purging this scenario's own
    # fixed id range up front makes it self-contained regardless of what
    # a previous run in this folder left behind.
    abuse_ids = {victim, *other_users}
    mod.MISTAKES_BANK[:] = [m for m in mod.MISTAKES_BANK if m.get("user_id") not in abuse_ids]
    for uid in abuse_ids:
        mod._MISTAKES_BY_USER.pop(uid, None)

    # ── 1. Same user, same question, fired concurrently many times ──────
    before_count = len(mod._MISTAKES_BY_USER.get(victim, []))
    try:
        with capture_diagnostics() as diag:
            await asyncio.gather(*(
                mod.record_mistake(victim, shared_mid, year, module, subject) for _ in range(25)
            ))
        result.diagnostics.extend(diag["lines"])
    except Exception as e:
        result.errors.append(f"user {victim}: {type(e).__name__}: {e}")
    after_count = len(mod._MISTAKES_BY_USER.get(victim, []))
    if after_count - before_count != 1:
        result.violations.append(
            f"user {victim}: 25 concurrent record_mistake() calls for the SAME question produced "
            f"{after_count - before_count} bank entries, expected exactly 1 (dedup by user_id+mid+year "
            f"should hold — see record_mistake's own docstring)"
        )

    # ── 2. Many DIFFERENT users concurrently missing the SAME question ──
    bank_len_before = len(mod.MISTAKES_BANK)
    try:
        with capture_diagnostics() as diag:
            await asyncio.gather(*(
                mod.record_mistake(u, shared_mid, year, module, subject) for u in other_users
            ))
        result.diagnostics.extend(diag["lines"])
    except Exception as e:
        result.errors.append(f"batch record_mistake: {type(e).__name__}: {e}")
    bank_len_after = len(mod.MISTAKES_BANK)
    if bank_len_after - bank_len_before != len(other_users):
        result.violations.append(
            f"{len(other_users)} different users concurrently missing the same question added "
            f"{bank_len_after - bank_len_before} bank entries total, expected exactly {len(other_users)} "
            f"(one each) — possible lost update or cross-user duplication under concurrency"
        )
    for u in other_users:
        entries = mod._MISTAKES_BY_USER.get(u, [])
        if len(entries) != 1 or entries[0].get("mid") != shared_mid:
            result.violations.append(
                f"user {u}: expected exactly 1 mistakes-bank entry for mid {shared_mid}, "
                f"got {entries!r} — _MISTAKES_BY_USER may have cross-contaminated between users"
            )

    # ── 3. A malformed entry injected directly (missing required keys) —
    # simulates a bad manual edit or a channel restore that let something
    # through _clean_analytics_dict-style filtering ─────────────────────
    malformed = {"user_id": victim, "mid": 999_999_999, "year": year}   # missing module + subject
    mod.MISTAKES_BANK.append(malformed)
    mod._mistakes_index_add(malformed)
    try:
        with capture_diagnostics() as diag:
            await mod.start_mistakes_retake(ctx, victim)
        result.diagnostics.extend(diag["lines"])
    except Exception as e:
        result.errors.append(f"user {victim}: start_mistakes_retake crashed on a malformed bank entry: {type(e).__name__}: {e}")
    if malformed in mod.MISTAKES_BANK:
        result.violations.append(
            f"user {victim}: a malformed mistakes-bank entry (missing module/subject) survived "
            f"start_mistakes_retake instead of being pruned by _resolve_mistake"
        )
    valid_entries_left = [m for m in mod._MISTAKES_BY_USER.get(victim, []) if mod._is_valid_mistake_entry(m)]
    if not valid_entries_left and after_count > before_count:
        result.violations.append(
            f"user {victim}: the earlier valid mistake (mid {shared_mid}) disappeared after the "
            f"malformed-entry retake attempt — pruning may be too aggressive"
        )

    # ── 4. Full retake driven to real completion, after all the abuse
    # above, to make sure nothing above left the bank unusable ──────────
    try:
        with capture_diagnostics() as diag:
            if victim in mod.MISTAKES_RETAKE_SESSIONS:
                live = mod.MISTAKES_RETAKE_SESSIONS[victim]
                sent = live.get("current_poll_id") is not None
                while sent:
                    poll_id, correct_opt = live["current_poll_id"], live["current_correct_id"]
                    await answer_poll(mod, ctx, victim, poll_id, correct_opt)
                    live = mod.MISTAKES_RETAKE_SESSIONS.get(victim)
                    sent = live is not None and live.get("current_poll_id") is not None
        result.diagnostics.extend(diag["lines"])
    except Exception as e:
        result.errors.append(f"user {victim}: retake-to-completion crashed: {type(e).__name__}: {e}")
    if victim in mod.MISTAKES_RETAKE_SESSIONS:
        result.violations.append(f"user {victim}: mistakes-retake session never finished after the abuse above")

    # ── 5. Index/list consistency: every entry in _MISTAKES_BY_USER[victim]
    # must be the SAME object present in MISTAKES_BANK, and vice versa ───
    bank_ids_for_victim = {id(m) for m in mod.MISTAKES_BANK if m.get("user_id") == victim}
    index_ids_for_victim = {id(m) for m in mod._MISTAKES_BY_USER.get(victim, [])}
    if bank_ids_for_victim != index_ids_for_victim:
        result.violations.append(
            f"user {victim}: _MISTAKES_BY_USER and MISTAKES_BANK disagree on this user's entries after "
            f"the abuse above (index has {len(index_ids_for_victim)}, bank has {len(bank_ids_for_victim)}) "
            f"— the derived index has drifted from its source of truth"
        )

    return result


# ─────────────────────────────────────────────────────────────────
# STEP 5d — DEEP AUDIT scenarios (fault injection, corrupt state,
# real backup/restore, stale-task races, boundary/fuzz, leak detection)
# ─────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def leak_sentinel(label: str, violations_out: list):
    """Snapshots background asyncio tasks and the temp directory before
    and after the wrapped block, flagging anything left behind. Must be
    used INSIDE a running event loop (i.e. inside the coroutine passed to
    asyncio.run, not around the asyncio.run call itself) — a task orphaned
    by a torn-down loop is simply gone, not observable from outside it.
    Only catches tasks still ALIVE when the block exits; a leaked task
    that already finished (but was never awaited, so any exception in it
    was silently dropped) won't show up here — that class of bug needs a
    real asyncio debug-mode run, which is outside this script's scope."""
    current = asyncio.current_task()
    tasks_before = {t for t in asyncio.all_tasks() if t is not current}
    tmp_before = set(os.listdir(tempfile.gettempdir()))
    yield
    tasks_after = {t for t in asyncio.all_tasks() if t is not current}
    leaked_tasks = [t for t in (tasks_after - tasks_before) if not t.done()]
    if leaked_tasks:
        violations_out.append(
            f"{label}: {len(leaked_tasks)} background task(s) still running after the scenario finished "
            f"(names: {[t.get_name() for t in leaked_tasks][:5]}) — likely an asyncio.create_task() call "
            f"whose result is never awaited or cancelled"
        )
    try:
        tmp_after = set(os.listdir(tempfile.gettempdir()))
        leaked_files = tmp_after - tmp_before
        if leaked_files:
            violations_out.append(
                f"{label}: {len(leaked_files)} new file(s) left behind in the temp directory "
                f"(e.g. {sorted(leaked_files)[:3]}) — check any code path here that uses "
                f"tempfile.NamedTemporaryFile/mkstemp without cleaning up on every exit path"
            )
    except Exception:
        pass   # tempdir listing is best-effort; never fail the scenario over it


async def scenario_daily_quiz_race(mod, fake_bot, user_id: int) -> CorrectnessResult:
    """Same idea as the lecture 'race' scenario, for the Daily Quiz path:
    two concurrent identical taps on the same live Daily Quiz poll from
    the same user should be deduped by @_serialize_per_user exactly like
    the lecture path — this exercises _advance_daily_quiz_session's
    routing instead of _advance_lecture_session's."""
    result = CorrectnessResult(runs=1)
    ctx = make_fake_context(mod, fake_bot)
    _prep_user_settings(mod, user_id)
    entry = mod._get_settings_entry(user_id)
    entry["daily_quiz_last_date"] = None

    violations = []
    with leak_sentinel(f"daily_quiz_race user {user_id}", violations):
        try:
            await mod.start_daily_quiz(ctx, user_id)
            live = mod.DAILY_QUIZ_SESSIONS.get(user_id)
            if live is None or live.get("current_poll_id") is None:
                result.errors.append(f"user {user_id}: start_daily_quiz produced no answerable session")
            else:
                poll_id, correct_opt = live["current_poll_id"], live["current_correct_id"]
                before = mod._get_entry(user_id).get("lecture_questions_answered", 0)
                with capture_diagnostics() as diag:
                    await asyncio.gather(
                        answer_poll(mod, ctx, user_id, poll_id, correct_opt),
                        answer_poll(mod, ctx, user_id, poll_id, correct_opt),
                    )
                result.diagnostics.extend(diag["lines"])
                after = mod._get_entry(user_id).get("lecture_questions_answered", 0)
                if after - before != 1:
                    result.violations.append(
                        f"user {user_id}: a double-tap on the same Daily Quiz poll advanced "
                        f"lecture_questions_answered by {after - before}, expected exactly 1"
                    )
        except Exception as e:
            result.errors.append(f"user {user_id}: {type(e).__name__}: {e}")
        finally:
            mod.DAILY_QUIZ_SESSIONS.pop(user_id, None)
    result.violations.extend(violations)
    return result


async def scenario_stale_task_race(mod, fake_bot, user_id: int, year: str, module: str, subject: str) -> CorrectnessResult:
    """A real answer landing at the exact moment _cleanup_stale_sessions_job
    reclaims that SAME session — e.g. a student's answer that was in
    flight right as the 6-hour idle sweep runs. Backdates the session's
    current_delivered_at past STALE_SESSION_IDLE_SECONDS so the real
    cleanup job targets it, then fires the cleanup job and a real answer
    concurrently. Either outcome (the answer lands first and completes
    cleanly, or cleanup wins and the answer hits the 'no session'
    fallback) is acceptable — what's NOT acceptable is a crash, a
    double-processed answer, or a session that survives in a half-
    cleaned-up state (removed from LECTURE_SESSIONS but its poll never
    stopped, or vice versa)."""
    result = CorrectnessResult(runs=1)
    ctx = make_fake_context(mod, fake_bot)
    _prep_user_settings(mod, user_id, auto_next=True, spaced_repetition=False)
    lecture_key = f"{subject} Lecture 1"

    if not hasattr(mod, "_cleanup_stale_sessions_job") or not hasattr(mod, "STALE_SESSION_IDLE_SECONDS"):
        result.diagnostics.append("_cleanup_stale_sessions_job/STALE_SESSION_IDLE_SECONDS not found — skipping.")
        return result

    violations = []
    with leak_sentinel(f"stale_task_race user {user_id}", violations):
        try:
            lecture_entry = mod.QUIZ_INDEX[year][lecture_key]
            poll_status_by_mid = {
                v["message_id"]: v for v in mod.QUIZ_POLL_STATUS[year].values() if v["lecture"] == lecture_key
            }
            session = {
                "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
                "queue": list(lecture_entry["ids"]), "current_poll_id": None, "current_correct_id": None,
                "total": len(lecture_entry["ids"]), "answered": 0, "correct": 0,
                "mode": "auto", "pending_polls": {}, "award_xp": True,
                "poll_status_by_mid": poll_status_by_mid, "xp_earned": 0, "started_at": time.time(),
            }
            mod.LECTURE_SESSIONS[user_id] = session
            await mod._deliver_next_lecture_question(ctx, user_id, session)
            poll_id, correct_opt = session["current_poll_id"], session["current_correct_id"]
            # Backdate past the staleness cutoff so the real cleanup job
            # judges this session eligible for reclaiming right now.
            session["current_delivered_at"] = time.time() - mod.STALE_SESSION_IDLE_SECONDS - 1

            with capture_diagnostics() as diag:
                gathered = await asyncio.gather(
                    mod._cleanup_stale_sessions_job(ctx),
                    answer_poll(mod, ctx, user_id, poll_id, correct_opt),
                    return_exceptions=True,
                )
            result.diagnostics.extend(diag["lines"])
            for outcome in gathered:
                if isinstance(outcome, Exception):
                    result.errors.append(
                        f"user {user_id}: stale-task race raised {type(outcome).__name__}: {outcome}"
                    )
            still_there = mod.LECTURE_SESSIONS.get(user_id)
            if still_there is not None and still_there.get("current_poll_id") is None and not still_there.get("queue"):
                result.violations.append(
                    f"user {user_id}: session left in a dead state (no current question, no queue left) "
                    f"but never actually removed from LECTURE_SESSIONS"
                )
        except Exception as e:
            result.errors.append(f"user {user_id}: {type(e).__name__}: {e}")
        finally:
            mod.LECTURE_SESSIONS.pop(user_id, None)
    result.violations.extend(violations)
    return result


async def scenario_fault_injection(mod, fake_bot, base_user_id: int, num_users: int,
                                    year: str, module: str, subject: str) -> CorrectnessResult:
    """Runs a full lecture + a Daily Quiz + a backup call against a
    FakeBot configured to randomly raise realistic Telegram errors
    (RetryAfter/TimedOut/NetworkError/Forbidden/BadRequest) instead of
    always succeeding. This checks SURVIVAL, not perfect recovery: a
    failed send_poll might legitimately mean a question never reaches
    the user, and this scenario doesn't try to model every possible
    correct response to that. What it does check: no fault anywhere
    causes an uncaught exception to escape, and whatever DOES get
    recorded in ANALYTICS stays internally consistent (no partial XP
    award, no answered/correct split that stops adding up) — i.e. a
    send failure should never corrupt state, even if it reasonably
    aborts that particular step.

    Temporarily swaps mod.app.bot for a fault-injecting instance and
    restores the original afterward, so this doesn't affect any other
    scenario's behavior."""
    result = CorrectnessResult(runs=1)
    original_bot = mod.app.bot
    faulty_bot = FakeBot(fake_latency_ms=fake_bot.fake_latency_ms, fail_rate=0.15)
    mod.app.bot = faulty_bot
    violations = []
    try:
        with leak_sentinel("fault_injection", violations):
            victim = base_user_id
            lecture_key = f"{subject} Lecture 1"
            with capture_diagnostics() as diag:
                try:
                    await run_full_lecture(mod, faulty_bot, victim, year, module, subject, lecture_key)
                except Exception as e:
                    result.errors.append(f"user {victim}: full_lecture under fault injection raised "
                                          f"{type(e).__name__}: {e} — this should have been caught internally, "
                                          f"same as it would need to be against a real flaky network")
                mod.LECTURE_SESSIONS.pop(victim, None)   # in case a fault left it dangling mid-lecture

                try:
                    daily_result = await scenario_full_daily_quiz(mod, faulty_bot, victim)
                    # Only structural violations matter here (answered ==
                    # correct+incorrect, xp/level consistency, etc.) — the
                    # exact completion counts are allowed to differ from a
                    # fault-free run, since some sends legitimately failed.
                    for v in daily_result.violations:
                        if "achievement" not in v and "daily_quizzes_completed" not in v:
                            result.violations.append(f"[fault injection] {v}")
                    result.errors.extend(f"[fault injection] {e}" for e in daily_result.errors)
                except Exception as e:
                    result.errors.append(f"user {victim}: full_daily_quiz under fault injection raised "
                                          f"{type(e).__name__}: {e}")
                mod.DAILY_QUIZ_SESSIONS.pop(victim, None)

                # A backup cycle under fault injection: should never
                # raise, and should never leave RESTORE_OK/dirty flags in
                # a state that blocks all future backups just because one
                # attempt hit a simulated network error.
                if hasattr(mod, "backup_analytics_to_channel"):
                    ctx = make_fake_context(mod, faulty_bot)
                    try:
                        await mod.backup_analytics_to_channel(ctx)
                    except Exception as e:
                        result.errors.append(f"backup_analytics_to_channel under fault injection raised "
                                              f"{type(e).__name__}: {e} — a failed Telegram call here should "
                                              f"be caught internally (see every other backup_*_to_channel's "
                                              f"own try/except around send_document), not propagate")
            result.diagnostics.extend(diag["lines"])
            result.violations.extend(verify_analytics_invariants(mod, victim))
    finally:
        mod.app.bot = original_bot
    result.violations.extend(violations)
    return result


async def scenario_corrupt_state(mod, fake_bot, base_user_id: int, num_users: int,
                                  year: str, module: str, subject: str) -> CorrectnessResult:
    """Feeds each load_*() function a corrupted version of its own JSON
    file (truncated mid-object, syntactically invalid, and valid-JSON-
    but-wrong-shape) and confirms it degrades to a safe empty default
    instead of raising — this is what actually happens if the process
    gets killed mid-write despite _atomic_write_json's temp-file+replace
    protection, or after a bad manual edit to a file restored from
    backup. Runs each load function in a throwaway temp directory so it
    never touches this process's real working-directory JSON files."""
    result = CorrectnessResult(runs=1)
    targets = []
    for file_attr, loader_name in (
        ("ANALYTICS_FILE", "load_analytics"),
        ("SETTINGS_FILE", "load_settings"),
        ("MISTAKES_BANK_FILE", "load_mistakes_bank"),
        ("SESSIONS_FILE", "load_sessions"),
    ):
        if hasattr(mod, file_attr) and hasattr(mod, loader_name):
            targets.append((getattr(mod, file_attr), getattr(mod, loader_name), loader_name))

    if not targets:
        result.diagnostics.append("No load_*()/*_FILE pairs found to test — skipping corrupt_state.")
        return result

    corrupt_payloads = {
        "truncated":       lambda good: good[: max(1, len(good) // 2)],           # cut off mid-object
        "invalid_syntax":  lambda good: "{not: valid, json,,,",
        "wrong_top_level": lambda good: json.dumps("just a string, not a dict/list"),
        "empty_file":      lambda good: "",
    }

    orig_cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="loadtest_corrupt_") as tmp_dir:
        os.chdir(tmp_dir)
        try:
            for filename, loader, loader_name in targets:
                try:
                    good_snapshot = loader()
                    good_json = json.dumps(good_snapshot, default=str)
                except Exception:
                    good_json = "{}"
                for kind, make_bad in corrupt_payloads.items():
                    bad_content = make_bad(good_json)
                    with open(filename, "w", encoding="utf-8") as f:
                        f.write(bad_content)
                    try:
                        with capture_diagnostics() as diag:
                            loaded = loader()
                        result.diagnostics.extend(diag["lines"])
                        if loaded is None and loader_name != "load_sessions":
                            result.violations.append(
                                f"{loader_name}() returned None for a {kind} {filename} — callers "
                                f"almost certainly expect a dict/list back, not None"
                            )
                        elif not isinstance(loaded, (dict, list, type(None))):
                            result.violations.append(
                                f"{loader_name}() returned {type(loaded).__name__} for a {kind} "
                                f"{filename} — expected dict/list"
                            )
                    except Exception as e:
                        result.violations.append(
                            f"{loader_name}() raised {type(e).__name__}: {e} on a {kind} {filename} "
                            f"instead of degrading to an empty default — this would crash the whole bot "
                            f"at import time (these loaders run at module level) if this file were ever "
                            f"corrupted on disk"
                        )
                    finally:
                        try:
                            os.remove(filename)
                        except OSError:
                            pass
        finally:
            os.chdir(orig_cwd)

    return result


async def scenario_backup_restore(mod, fake_bot, base_user_id: int, num_users: int,
                                   year: str, module: str, subject: str) -> CorrectnessResult:
    """The first real end-to-end test of backup_*_to_channel /
    restore_*_from_channel: previously get_chat().pinned_message was
    always None, so every restore function only ever exercised its
    'nothing pinned yet' early return — the actual restore logic (read
    the pin, download the file, parse it, repopulate the in-memory
    store) has never been exercised by this script until FakeBot started
    tracking real per-chat pinned documents with real byte content.

    For each of analytics/settings/mistakes_bank/sessions: seed one
    fake user's worth of real data, back it up for real, wipe the
    in-memory store, restore it for real, and check the data actually
    came back. Bypasses each system's own throttle by zeroing its
    _last_*_backup_at timestamp first, so the backup isn't silently
    skipped as 'too soon since the last one'."""
    result = CorrectnessResult(runs=1)
    ctx = make_fake_context(mod, fake_bot)
    victim = base_user_id

    async def _round_trip(name, backup_fn, restore_fn, throttle_attr, seed, wipe, extract, backup_kwargs=None):
        if not (hasattr(mod, backup_fn) and hasattr(mod, restore_fn)):
            result.diagnostics.append(f"{backup_fn}/{restore_fn} not found — skipping.")
            return
        if throttle_attr and hasattr(mod, throttle_attr):
            setattr(mod, throttle_attr, 0.0)
        seed()
        before = extract()
        try:
            with capture_diagnostics() as diag:
                await getattr(mod, backup_fn)(ctx, **(backup_kwargs or {}))
            result.diagnostics.extend(diag["lines"])
        except Exception as e:
            result.errors.append(f"{backup_fn} raised {type(e).__name__}: {e}")
            return
        wipe()
        try:
            with capture_diagnostics() as diag:
                await getattr(mod, restore_fn)(mod.app)
            result.diagnostics.extend(diag["lines"])
        except Exception as e:
            result.errors.append(f"{restore_fn} raised {type(e).__name__}: {e}")
            return
        after = extract()
        if after != before:
            result.violations.append(
                f"{name}: state after backup+wipe+restore doesn't match what was backed up "
                f"(this means the restore path is lossy or broken, not just 'no backup existed yet')"
            )

    def seed_analytics():
        entry = mod._get_entry(victim)
        entry["xp"] = entry.get("xp", 0) + 137
    await _round_trip(
        "analytics", "backup_analytics_to_channel", "restore_analytics_from_channel",
        "_last_analytics_backup_at", seed_analytics,
        lambda: mod.ANALYTICS.clear(),
        lambda: dict(mod.ANALYTICS),
    )

    def seed_settings():
        mod._get_settings_entry(victim)["year_class"] = "y1"
    await _round_trip(
        "settings", "backup_settings_to_channel", "restore_settings_from_channel",
        "_last_settings_backup_at", seed_settings,
        lambda: mod.SETTINGS.clear(),
        lambda: dict(mod.SETTINGS),
    )

    def seed_mistakes():
        lecture_entry = mod.QUIZ_INDEX[year][f"{subject} Lecture 1"]
        mod.MISTAKES_BANK[:] = [m for m in mod.MISTAKES_BANK if m.get("user_id") != victim]
        mod._MISTAKES_BY_USER.pop(victim, None)
        entry = {"user_id": victim, "mid": lecture_entry["ids"][0], "year": year, "module": module, "subject": subject}
        mod.MISTAKES_BANK.append(entry)
        mod._mistakes_index_add(entry)
    def wipe_mistakes():
        mod.MISTAKES_BANK.clear()
        mod._MISTAKES_BY_USER.clear()
    await _round_trip(
        "mistakes_bank", "backup_mistakes_bank_to_channel", "restore_mistakes_bank_from_channel",
        "_last_mistakes_bank_backup_at", seed_mistakes, wipe_mistakes,
        lambda: [m for m in mod.MISTAKES_BANK if m.get("user_id") == victim],
    )

    if hasattr(mod, "backup_sessions_to_channel"):
        def seed_sessions():
            lecture_entry = mod.QUIZ_INDEX[year][f"{subject} Lecture 1"]
            mod.LECTURE_SESSIONS[victim] = {
                "year": year, "module": module, "subject": subject,
                "lecture_key": f"{subject} Lecture 1", "queue": list(lecture_entry["ids"]),
                "current_poll_id": "fake", "current_correct_id": 0,
                "total": len(lecture_entry["ids"]), "answered": 1, "correct": 1,
                "mode": "auto", "pending_polls": {}, "award_xp": True,
                "poll_status_by_mid": {}, "xp_earned": 10, "started_at": time.time(),
            }
        await _round_trip(
            "sessions", "backup_sessions_to_channel", "restore_sessions_from_channel",
            "_last_sessions_backup_at", seed_sessions,
            lambda: mod.LECTURE_SESSIONS.clear(),
            lambda: dict(mod.LECTURE_SESSIONS.get(victim, {})),
            backup_kwargs={"force": True},   # bypasses both the timestamp throttle AND the
                                              # "identical to last upload" content-dedup gate
        )
        mod.LECTURE_SESSIONS.pop(victim, None)

    return result


async def scenario_boundary_fuzz(mod, fake_bot, base_user_id: int, num_users: int,
                                  year: str, module: str, subject: str) -> CorrectnessResult:
    """Targeted adversarial inputs rather than a general-purpose fuzzer:
    an empty option_ids list (Telegram sends this when a user RETRACTS
    their poll answer, which is a real, common event this bot must
    handle, not a hypothetical), an out-of-range option index, and
    extreme user_ids — each checked for 'no crash, no state change,'
    since none of these represent a legitimate new answer."""
    result = CorrectnessResult(runs=1)
    ctx = make_fake_context(mod, fake_bot)
    lecture_key = f"{subject} Lecture 1"
    victim = base_user_id
    _prep_user_settings(mod, victim, auto_next=True, spaced_repetition=False)

    lecture_entry = mod.QUIZ_INDEX[year][lecture_key]
    poll_status_by_mid = {
        v["message_id"]: v for v in mod.QUIZ_POLL_STATUS[year].values() if v["lecture"] == lecture_key
    }
    session = {
        "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
        "queue": list(lecture_entry["ids"]), "current_poll_id": None, "current_correct_id": None,
        "total": len(lecture_entry["ids"]), "answered": 0, "correct": 0,
        "mode": "auto", "pending_polls": {}, "award_xp": True,
        "poll_status_by_mid": poll_status_by_mid, "xp_earned": 0, "started_at": time.time(),
    }
    mod.LECTURE_SESSIONS[victim] = session
    try:
        await mod._deliver_next_lecture_question(ctx, victim, session)
        poll_id = session["current_poll_id"]
        before_answered = mod._get_entry(victim).get("lecture_questions_answered", 0)

        adversarial_cases = [
            ("empty option_ids (a real Telegram retraction event)", []),
            ("out-of-range option index", [97]),
            ("negative option index", [-1]),
        ]
        with capture_diagnostics() as diag:
            for label, option_ids in adversarial_cases:
                update = _FakeUpdate(poll_id, victim, option_ids)
                try:
                    await mod.handle_poll_answer(update, ctx)
                except Exception as e:
                    result.errors.append(f"user {victim}: {label} raised {type(e).__name__}: {e}")
        result.diagnostics.extend(diag["lines"])
        after_answered = mod._get_entry(victim).get("lecture_questions_answered", 0)
        if after_answered != before_answered:
            result.violations.append(
                f"user {victim}: one of the adversarial option_ids inputs changed "
                f"lecture_questions_answered ({before_answered} -> {after_answered}) — none of "
                f"empty/out-of-range/negative option_ids should count as a real answer"
            )
        if victim not in mod.LECTURE_SESSIONS or mod.LECTURE_SESSIONS[victim].get("current_poll_id") != poll_id:
            result.violations.append(
                f"user {victim}: the live question was disturbed by adversarial option_ids input "
                f"(session gone or current_poll_id changed unexpectedly)"
            )
    except Exception as e:
        result.errors.append(f"user {victim}: {type(e).__name__}: {e}")
    finally:
        mod.LECTURE_SESSIONS.pop(victim, None)

    for extreme_uid in (0, -1, 2**62):
        try:
            mod._get_settings_entry(extreme_uid)
            mod._get_entry(extreme_uid)
        except Exception as e:
            result.errors.append(f"user_id={extreme_uid}: {type(e).__name__}: {e}")

    return result


# ─────────────────────────────────────────────────────────────────
# STEP 5e — SELF-AUDIT: static (AST) + runtime coverage inventory
# ─────────────────────────────────────────────────────────────────
# This does NOT gate pass/fail on its own (except the FakeBot-coverage
# gaps, which are a precise, mechanical check, not a heuristic) — it's a
# "did we forget to test something new" tripwire, meant to be re-run
# every time bot.py grows a new command, handler, job, or Telegram call.
# The "possibly untested" flags are a crude proxy (does this name appear
# anywhere in loadtest.py's own source text) — false positives (a name
# that happens to appear in a comment) and false negatives (a scenario
# that exercises the code without ever spelling out its name) are both
# expected. Treat this as a prompt to go look, not a verdict.

_TELEGRAM_BOT_METHODS = {
    "send_message", "send_poll", "send_photo", "send_document", "send_video",
    "send_animation", "send_media_group", "set_message_reaction", "pin_chat_message",
    "unpin_chat_message", "delete_message", "delete_messages", "stop_poll",
    "copy_messages", "copy_message", "edit_message_text", "edit_message_caption",
    "edit_message_reply_markup", "edit_message_media", "get_chat", "get_file",
    "forward_message", "forward_messages", "answer_callback_query", "get_chat_member",
    "ban_chat_member", "unban_chat_member", "restrict_chat_member", "get_me",
}


class _SelfAuditVisitor(ast.NodeVisitor):
    def __init__(self):
        self.bot_calls: dict = {}          # method name -> count
        self.create_task_sites: list = []  # (lineno, enclosing_func)
        self.raw_open_sites: list = []     # (lineno, enclosing_func)
        self.job_registrations: list = []  # (job_type, callback_name, lineno)
        self.handler_registrations: list = []  # (handler_type, detail, lineno)
        self._func_stack: list = []

    def _enclosing(self) -> str:
        return self._func_stack[-1] if self._func_stack else "<module level>"

    def visit_FunctionDef(self, node):
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in _TELEGRAM_BOT_METHODS:
                self.bot_calls[func.attr] = self.bot_calls.get(func.attr, 0) + 1
            elif func.attr in ("create_task", "ensure_future"):
                self.create_task_sites.append((node.lineno, self._enclosing()))
            elif func.attr in ("run_repeating", "run_once", "run_daily") and node.args:
                cb = node.args[0]
                cb_name = cb.id if isinstance(cb, ast.Name) else ast.dump(cb)[:40]
                self.job_registrations.append((func.attr, cb_name, node.lineno))
            elif func.attr == "add_handler" and node.args:
                inner = node.args[0]
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                    htype = inner.func.id
                    detail = None
                    if htype == "CommandHandler" and inner.args and isinstance(inner.args[0], ast.Constant):
                        detail = inner.args[0].value
                    self.handler_registrations.append((htype, detail, node.lineno))
        elif isinstance(func, ast.Name):
            if func.id == "open" and self._enclosing() != "_atomic_write_json" \
                    and "atomic" not in self._enclosing().lower():
                mode = "r"
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                is_write = any(c in str(mode) for c in "wax+")
                if is_write:
                    self.raw_open_sites.append((node.lineno, self._enclosing()))
        self.generic_visit(node)


def run_self_audit(bot_path: str, mod) -> dict:
    """Static (AST) scan of bot_path plus runtime introspection of the
    actually-registered handlers on mod.app. Returns a summary dict;
    print_self_audit_report renders it."""
    with open(bot_path, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=bot_path)
    visitor = _SelfAuditVisitor()
    visitor.visit(tree)

    # Precise check: does FakeBot actually implement every bot method
    # bot.py calls? (excludes the __getattr__ catch-all itself)
    implemented = {
        name for name in vars(FakeBot)
        if not name.startswith("_") and callable(getattr(FakeBot, name))
    }
    missing_fakebot_methods = sorted(set(visitor.bot_calls) - implemented)

    # Heuristic check: does this script's own source mention the name at
    # all? Read fresh from disk (not __file__, which may be frozen/zipped
    # in some environments) — falls back to "unknown" if unreadable.
    try:
        with open(__file__, encoding="utf-8") as f:
            own_source = f.read()
    except Exception:
        own_source = None

    def _mentioned(name) -> bool | None:
        if own_source is None or not name:
            return None
        return name in own_source

    commands_found = [(t, d, l) for (t, d, l) in visitor.handler_registrations if t == "CommandHandler"]
    other_handlers_found = [(t, d, l) for (t, d, l) in visitor.handler_registrations if t != "CommandHandler"]

    # Runtime, authoritative handler inventory (independent of the AST
    # guess above) — this is what's ACTUALLY registered right now.
    runtime_handlers = []
    try:
        for group, handlers in mod.app.handlers.items():
            for h in handlers:
                htype = type(h).__name__
                detail = None
                if hasattr(h, "commands"):
                    detail = sorted(h.commands)
                elif hasattr(h, "pattern") and h.pattern is not None:
                    detail = getattr(h.pattern, "pattern", str(h.pattern))
                runtime_handlers.append((group, htype, detail))
    except Exception as e:
        runtime_handlers = None
        runtime_handlers_error = str(e)
    else:
        runtime_handlers_error = None

    return {
        "missing_fakebot_methods": missing_fakebot_methods,
        "bot_calls": visitor.bot_calls,
        "create_task_sites": [(ln, fn, _mentioned(fn)) for ln, fn in visitor.create_task_sites],
        "raw_open_sites": [(ln, fn, _mentioned(fn)) for ln, fn in visitor.raw_open_sites],
        "job_registrations": [(t, cb, ln, _mentioned(cb)) for t, cb, ln in visitor.job_registrations],
        "commands_found": [(d, ln, _mentioned(d if isinstance(d, str) else None)) for t, d, ln in commands_found],
        "other_handlers_found": other_handlers_found,
        "runtime_handlers": runtime_handlers,
        "runtime_handlers_error": runtime_handlers_error,
    }


def print_self_audit_report(audit: dict):
    print(f"\n{'=' * 60}")
    print("  [SELF-AUDIT] Static + runtime coverage inventory")
    print(f"{'=' * 60}")

    if audit["missing_fakebot_methods"]:
        print(f"  ❌ FakeBot is missing {len(audit['missing_fakebot_methods'])} method(s) bot.py actually calls:")
        for m in audit["missing_fakebot_methods"]:
            print(f"      - bot.{m}() — add this to FakeBot before trusting any scenario that hits it")
    else:
        print(f"  ✅ FakeBot implements every Telegram Bot method found in bot.py "
              f"({len(audit['bot_calls'])} distinct method(s) called).")

    print(f"\n  Commands registered (AST scan): {len(audit['commands_found'])}")
    unmentioned_cmds = [d for (d, ln, m) in audit["commands_found"] if m is False]
    if unmentioned_cmds:
        print(f"  ⚠️  possibly untested (command name not found anywhere in loadtest.py's own source):")
        for d in unmentioned_cmds:
            print(f"      - /{d}")

    if audit["runtime_handlers"] is not None:
        by_type = {}
        for group, htype, detail in audit["runtime_handlers"]:
            by_type.setdefault(htype, 0)
            by_type[htype] += 1
        print(f"\n  Runtime handler inventory (authoritative — {len(audit['runtime_handlers'])} total):")
        for htype, count in sorted(by_type.items()):
            print(f"      - {htype}: {count}")
    else:
        print(f"\n  Runtime handler introspection failed ({audit['runtime_handlers_error']}) — "
              f"falling back to the AST-only numbers above.")

    if audit["create_task_sites"]:
        print(f"\n  asyncio.create_task()/ensure_future() call sites: {len(audit['create_task_sites'])}")
        for ln, fn, mentioned in audit["create_task_sites"]:
            flag = "" if mentioned else "  ⚠️  possibly untested"
            print(f"      - line {ln}, in {fn}(){flag}")

    if audit["raw_open_sites"]:
        print(f"\n  Raw open() calls outside _atomic_write_json: {len(audit['raw_open_sites'])}")
        for ln, fn, mentioned in audit["raw_open_sites"]:
            print(f"      - line {ln}, in {fn}() — confirm this has its own crash-safety, or route it "
                  f"through _atomic_write_json's temp-file+replace pattern instead")

    if audit["job_registrations"]:
        print(f"\n  Scheduled jobs (job_queue.run_*): {len(audit['job_registrations'])}")
        for jtype, cb, ln, mentioned in audit["job_registrations"]:
            flag = "" if mentioned else "  ⚠️  possibly untested"
            print(f"      - {jtype}({cb}) at line {ln}{flag}")

    print(f"{'=' * 60}\n")



PERF_SCENARIOS = {"poll_answer", "daily_quiz"}
CORRECTNESS_SCENARIOS = {"full_lecture", "full_daily_quiz", "achievements", "race", "sessions"}
ABUSE_SCENARIOS = {"session_abuse", "mistakes_bank_abuse"}
AUDIT_SCENARIOS = {
    "fault_injection", "corrupt_state", "backup_restore",
    "stale_task_race", "daily_quiz_race", "boundary_fuzz",
}


async def run_perf_scenario(mod, fake_bot, scenario: str, num_users: int, year, module, subject) -> RunResult:
    result = RunResult()
    base_user_id = 900_000_000  # far outside any real Telegram user id range

    async def _one(i):
        user_id = base_user_id + i
        try:
            if scenario == "poll_answer":
                ms = await scenario_poll_answer(mod, fake_bot, user_id, year, module, subject)
            elif scenario == "daily_quiz":
                ms = await scenario_daily_quiz(mod, fake_bot, user_id)
            else:
                raise ValueError(scenario)
            result.latencies_ms.append(ms)
        except Exception as e:
            result.errors.append(f"user {user_id}: {type(e).__name__}: {e}")

    # All N "users" fire at once — asyncio.gather schedules every task
    # concurrently on this one event loop, same as PTB's own
    # concurrent_updates would interleave many real users' updates. This
    # does NOT use multiple OS threads/processes, so it measures
    # concurrency the way your actual bot experiences it (one process,
    # cooperative multitasking), not raw multi-core throughput.
    await asyncio.gather(*(_one(i) for i in range(num_users)))
    return result


async def run_correctness_scenario(mod, fake_bot, scenario: str, num_users: int, year, module, subject,
                                    num_lectures: int) -> CorrectnessResult:
    combined = CorrectnessResult()
    base_user_id = 910_000_000  # separate id range from perf scenarios, so runs never collide

    async def _one(i):
        user_id = base_user_id + i
        if scenario == "full_lecture":
            return await scenario_full_lecture(mod, fake_bot, user_id, year, module, subject)
        elif scenario == "full_daily_quiz":
            return await scenario_full_daily_quiz(mod, fake_bot, user_id)
        elif scenario == "achievements":
            return await scenario_achievements(mod, fake_bot, user_id, year, module, subject, num_lectures)
        elif scenario == "race":
            return await scenario_same_user_race(mod, fake_bot, user_id, year, module, subject)
        elif scenario == "sessions":
            return await scenario_session_persistence(mod, fake_bot, user_id, year, module, subject)
        else:
            raise ValueError(scenario)

    per_user_results = await asyncio.gather(*(_one(i) for i in range(num_users)))
    for r in per_user_results:
        combined.violations.extend(r.violations)
        combined.diagnostics.extend(r.diagnostics)
        combined.errors.extend(r.errors)
        combined.runs += r.runs
    return combined


async def run_abuse_scenario(mod, fake_bot, scenario: str, num_users: int, year, module, subject) -> CorrectnessResult:
    """Abuse scenarios run ONCE per invocation (not once per simulated
    user) — num_users controls how many attacker/user identities the
    abuse pattern itself uses internally. Uses its own user-id range so
    it never collides with perf or correctness scenarios' fake users."""
    base_user_id = 920_000_000
    if scenario == "session_abuse":
        return await scenario_session_abuse(mod, fake_bot, base_user_id, num_users, year, module, subject)
    elif scenario == "mistakes_bank_abuse":
        return await scenario_mistakes_bank_abuse(mod, fake_bot, base_user_id, num_users, year, module, subject)
    else:
        raise ValueError(scenario)


async def run_audit_scenario(mod, fake_bot, scenario: str, num_users: int, year, module, subject) -> CorrectnessResult:
    """Deep-audit scenarios each run ONCE per invocation, in their own
    user-id range (930_000_000+) so they never collide with any other
    category's fake users."""
    base_user_id = 930_000_000
    if scenario == "fault_injection":
        return await scenario_fault_injection(mod, fake_bot, base_user_id, num_users, year, module, subject)
    elif scenario == "corrupt_state":
        return await scenario_corrupt_state(mod, fake_bot, base_user_id, num_users, year, module, subject)
    elif scenario == "backup_restore":
        return await scenario_backup_restore(mod, fake_bot, base_user_id, num_users, year, module, subject)
    elif scenario == "boundary_fuzz":
        return await scenario_boundary_fuzz(mod, fake_bot, base_user_id, num_users, year, module, subject)
    elif scenario == "stale_task_race":
        return await scenario_stale_task_race(mod, fake_bot, base_user_id, year, module, subject)
    elif scenario == "daily_quiz_race":
        return await scenario_daily_quiz_race(mod, fake_bot, base_user_id)
    else:
        raise ValueError(scenario)


def percentile(data: list, pct: float) -> float:
    if not data:
        return float("nan")
    s = sorted(data)
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def print_perf_report(scenario: str, num_users: int, result: RunResult, fake_bot: FakeBot):
    lat = result.latencies_ms
    print(f"\n{'=' * 60}")
    print(f"  [PERF] Scenario: {scenario}   Simulated concurrent users: {num_users}")
    print(f"{'=' * 60}")
    if result.errors:
        print(f"  ⚠️  {len(result.errors)}/{num_users} runs raised an error:")
        for e in result.errors[:10]:
            print(f"      - {e}")
        if len(result.errors) > 10:
            print(f"      ... and {len(result.errors) - 10} more")
    if lat:
        print(f"  Successful runs: {len(lat)}/{num_users}")
        print(f"  p50 latency: {percentile(lat, 50):8.2f} ms")
        print(f"  p95 latency: {percentile(lat, 95):8.2f} ms")
        print(f"  p99 latency: {percentile(lat, 99):8.2f} ms")
        print(f"  max latency: {max(lat):8.2f} ms")
        print(f"  min latency: {min(lat):8.2f} ms")
    else:
        print("  No successful runs — see errors above.")
    print(f"  Fake Telegram calls made: {fake_bot.call_counts.get('_total', 0)}")
    print(f"{'=' * 60}\n")


def print_correctness_report(scenario: str, num_users: int, result: CorrectnessResult):
    print(f"\n{'=' * 60}")
    status = "✅ PASS" if result.ok else "❌ FAIL"
    print(f"  [CORRECTNESS] Scenario: {scenario}   Users: {num_users}   {status}")
    print(f"{'=' * 60}")
    print(f"  Runs completed: {result.runs}/{num_users}")
    if result.errors:
        print(f"  ⚠️  {len(result.errors)} uncaught exception(s):")
        for e in result.errors[:10]:
            print(f"      - {e}")
        if len(result.errors) > 10:
            print(f"      ... and {len(result.errors) - 10} more")
    if result.violations:
        print(f"  ❌ {len(result.violations)} invariant violation(s):")
        for v in result.violations[:20]:
            print(f"      - {v}")
        if len(result.violations) > 20:
            print(f"      ... and {len(result.violations) - 20} more")
    if result.diagnostics:
        seen = set()
        unique = [d for d in result.diagnostics if not (d in seen or seen.add(d))]
        print(f"  📋 {len(unique)} distinct printed diagnostic(s) captured "
              f"(the bot printed these itself — some may be expected, worth a skim):")
        for d in unique[:15]:
            print(f"      - {d}")
        if len(unique) > 15:
            print(f"      ... and {len(unique) - 15} more distinct line(s)")
    if result.ok and not result.diagnostics:
        print("  Nothing to report — all invariants held, no errors, no suspicious prints.")
    print(f"{'=' * 60}\n")


# ─────────────────────────────────────────────────────────────────
# STEP 7 — CLI
# ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bot-path", default="bot.py", help="Path to your bot.py (default: ./bot.py)")
    parser.add_argument(
        "--scenario",
        choices=["poll_answer", "daily_quiz", "full_lecture", "full_daily_quiz",
                 "achievements", "race", "sessions", "session_abuse", "mistakes_bank_abuse",
                 "fault_injection", "corrupt_state", "backup_restore", "stale_task_race",
                 "daily_quiz_race", "boundary_fuzz",
                 "perf", "correctness", "abuse", "audit", "both", "all"],
        default="all",
        help="'perf'/'both' = the two latency scenarios; 'correctness' = the per-user "
             "correctness scenarios; 'abuse' = the session/mistakes-bank abuse scenarios; "
             "'audit' = the deep-audit scenarios (fault injection, corrupt state, real "
             "backup/restore, stale-task races, boundary/fuzz); 'all' (default) = "
             "literally everything, including the self-audit report.",
    )
    parser.add_argument("--users", type=int, nargs="+", default=[100, 300, 500, 700],
                         help="One or more concurrent-user counts for PERF scenarios, "
                              "e.g. --users 100 500 700. Correctness/abuse/audit scenarios use "
                              "--correctness-users/--abuse-users/--audit-users instead (usually "
                              "much smaller).")
    parser.add_argument("--correctness-users", type=int, default=50,
                         help="Concurrent simulated users for correctness scenarios "
                              "(default 50 — these are about behavior, not raw scale, "
                              "so this rarely needs to be large; a moderate number is "
                              "still worth using so cross-user interference bugs, if any, "
                              "have a chance to show up).")
    parser.add_argument("--abuse-users", type=int, default=30,
                         help="How many attacker/user identities each abuse scenario uses "
                              "internally (default 30) — e.g. how many different fake users "
                              "concurrently miss the same mistakes-bank question, or the "
                              "fanout of a duplicate-tap spam burst. Each abuse scenario runs "
                              "ONCE per invocation regardless of this number — it shapes the "
                              "abuse pattern, not a repeat count.")
    parser.add_argument("--audit-users", type=int, default=20,
                         help="Same idea as --abuse-users, for the deep-audit scenarios that "
                              "use it (fault_injection, corrupt_state, backup_restore, "
                              "boundary_fuzz — stale_task_race and daily_quiz_race ignore this, "
                              "they always use exactly one victim user).")
    parser.add_argument("--fake-latency-ms", type=float, default=0.0,
                         help="Simulated per-Telegram-call network delay, in ms. "
                              "0 (default) isolates pure code latency. Try 50-150 "
                              "to approximate a realistic round trip and see how "
                              "that compounds under concurrency.")
    parser.add_argument("--lectures", type=int, default=5, help="Fake lectures to seed (default 5)")
    parser.add_argument("--questions-per-lecture", type=int, default=10)
    parser.add_argument("--achievements-lectures", type=int, default=15,
                         help="How many of the seeded lectures the 'achievements' scenario "
                              "runs per user (default 15) — needs enough total questions "
                              "(this × --questions-per-lecture) to clear ACHIEVEMENTS' "
                              "first-tier thresholds, or it'll flag nothing-unlocked as a "
                              "violation. Raise --lectures to seed more if needed.")
    parser.add_argument("--no-self-audit", action="store_true",
                         help="Skip the AST + runtime self-audit report that otherwise runs "
                              "first for 'all' and 'audit'.")
    args = parser.parse_args()

    if not os.path.exists(args.bot_path):
        print(f"ERROR: {args.bot_path} not found. Run this from the same folder as your bot.py, "
              f"or pass --bot-path /path/to/bot.py")
        sys.exit(1)

    print(f"[loadtest] Importing {args.bot_path} with Telegram calls stubbed out...")
    mod, fake_bot = load_bot_module(args.bot_path, args.fake_latency_ms)
    print("[loadtest] Import succeeded. app.bot is now a FakeBot.")

    year, module, subject = seed_fake_data(mod, args.lectures, args.questions_per_lecture)

    run_self_audit_report = args.scenario in ("all", "audit") and not args.no_self_audit
    if run_self_audit_report:
        try:
            audit = run_self_audit(args.bot_path, mod)
            print_self_audit_report(audit)
        except Exception as e:
            print(f"[loadtest] Self-audit failed to run ({type(e).__name__}: {e}) — "
                  f"continuing with the scenarios below regardless.")

    if args.scenario in ("perf", "both"):
        scenarios = list(PERF_SCENARIOS)
    elif args.scenario == "correctness":
        scenarios = list(CORRECTNESS_SCENARIOS)
    elif args.scenario == "abuse":
        scenarios = list(ABUSE_SCENARIOS)
    elif args.scenario == "audit":
        scenarios = list(AUDIT_SCENARIOS)
    elif args.scenario == "all":
        scenarios = (list(PERF_SCENARIOS) + list(CORRECTNESS_SCENARIOS)
                     + list(ABUSE_SCENARIOS) + list(AUDIT_SCENARIOS))
    else:
        scenarios = [args.scenario]

    # Tracked for the final consolidated report.
    pass_fail: dict = {}   # scenario name -> True/False (perf scenarios aren't included — no pass/fail concept)

    for scenario in scenarios:
        if scenario in PERF_SCENARIOS:
            for n in args.users:
                fake_bot.call_counts.clear()
                result = asyncio.run(run_perf_scenario(mod, fake_bot, scenario, n, year, module, subject))
                print_perf_report(scenario, n, result, fake_bot)
        elif scenario in ABUSE_SCENARIOS:
            fake_bot.call_counts.clear()
            result = asyncio.run(run_abuse_scenario(
                mod, fake_bot, scenario, args.abuse_users, year, module, subject,
            ))
            print_correctness_report(scenario, args.abuse_users, result)
            pass_fail[scenario] = result.ok
        elif scenario in AUDIT_SCENARIOS:
            fake_bot.call_counts.clear()
            result = asyncio.run(run_audit_scenario(
                mod, fake_bot, scenario, args.audit_users, year, module, subject,
            ))
            print_correctness_report(scenario, args.audit_users, result)
            pass_fail[scenario] = result.ok
        else:
            fake_bot.call_counts.clear()
            result = asyncio.run(run_correctness_scenario(
                mod, fake_bot, scenario, args.correctness_users, year, module, subject,
                args.achievements_lectures,
            ))
            print_correctness_report(scenario, args.correctness_users, result)
            pass_fail[scenario] = result.ok

    if pass_fail:
        print(f"\n{'=' * 60}")
        print("  AUDIT SUMMARY")
        print(f"{'=' * 60}")
        for name, ok in sorted(pass_fail.items()):
            print(f"  {'✅ PASS' if ok else '❌ FAIL':10s} {name}")
        total, failed = len(pass_fail), sum(1 for ok in pass_fail.values() if not ok)
        print(f"{'-' * 60}")
        print(f"  {total - failed}/{total} passed" + (f" — {failed} FAILED" if failed else ""))
        print(f"{'=' * 60}\n")
        sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════
# READING THE RESULTS
# ═══════════════════════════════════════════════════════════════════════
#
# PERF scenarios (poll_answer, daily_quiz):
#
# p50 = typical user's experience. p95 = the experience of the unluckiest
# 1-in-20 users at that concurrency level — this is usually the number
# people mean by "how many users can it handle," since it's the threshold
# where a meaningful minority starts to notice lag.
#
# What to look for as you increase --users:
#
#   - LINEAR-ish growth (p95 roughly doubles when users doubles): the
#     code itself scales reasonably; a slow p95 at 700 mostly means
#     "just add more CPU/instances", not "there's a hidden bug."
#
#   - SUPER-linear growth (p95 quadruples when users doubles, or a cliff
#     appears at some threshold): there's likely still a hidden O(n)-ish
#     bottleneck somewhere in that scenario's path — worth profiling
#     further rather than just adding more vCPU, since more CPU won't
#     fix an algorithmic bottleneck, only delay when it becomes visible.
#
#   - Errors appearing only at higher concurrency (not at low counts):
#     usually a race condition on shared in-memory state. Check whether
#     the function under test is missing an @_serialize_per_user-style
#     guard, or two users are mutating the same dict without one waiting
#     for the other — the "race" correctness scenario below tests exactly
#     this for the one guard that already exists; a NEW hot path without
#     that guard would show up here as errors or (worse, silently) as an
#     invariant violation in the correctness scenarios instead.
#
# CORRECTNESS scenarios (full_lecture, full_daily_quiz, achievements,
# race, sessions):
#
#   - PASS means every invariant this script knows how to check held —
#     it does NOT mean "this bot has no bugs," only "no bug in the
#     specific things checked." Read the printed diagnostics even on a
#     PASS; they're not failures, but they're worth a skim.
#
#   - A violation naming a specific achievement category almost always
#     points at either: (a) the seeded volume being too low to clear
#     that category's tier-1 threshold (raise --lectures /
#     --questions-per-lecture / --achievements-lectures), or (b) an
#     actual bug in _check_achievements / ACHIEVEMENT_STAT_FIELD /
#     the call site that should have triggered it.
#
#   - A "race" scenario failure means @_serialize_per_user (or whatever
#     replaced it) is no longer preventing a double-tap from double-
#     counting — treat this as high-severity, since it means XP/streak
#     numbers become unreliable under real concurrent load, not just in
#     this synthetic test.
#
#   - A "sessions" scenario skip (not failure) just means the bot.py you
#     pointed this at doesn't have the SESSION PERSISTENCE feature yet.
#
# What this DOESN'T tell you (either kind of scenario):
#
#   - Real Telegram API latency or rate-limit behavior (AIORateLimiter
#     is real code but was never exercised here since FakeBot bypasses
#     it entirely — the rate limiter wraps the bot object PTB constructs
#     internally, and this script swaps the whole bot instance).
#   - Railway's actual CPU/memory ceiling at your current plan — this
#     tells you how your CODE scales, which combined with Railway's
#     metrics (CPU % during a real run, from Railway's own dashboard)
#     lets you decide when to size up.
#   - Disk I/O contention exactly as Railway's filesystem would behave —
#     _atomic_write_json's fsync cost depends on the underlying storage,
#     which will differ between your local machine and Railway's volumes.
#   - Cross-user interference bugs that only manifest with real Telegram
#     user IDs, real timing jitter, or real network retries — this script
#     controls timing precisely (everything is either instant or a fixed
#     --fake-latency-ms), which is great for isolating your code's own
#     behavior but won't reproduce bugs that only need real-world jitter
#     to trigger.
