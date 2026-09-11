#!/usr/bin/env python3
"""
loadtest.py — hot-path latency test for The Quizician bot.

WHAT THIS DOES
---------------
Imports your actual bot.py (with every Telegram network call replaced by
an in-memory fake that responds instantly, or after a configurable fake
delay) and fires N simulated concurrent "students" at the real hot-path
functions:

  - poll_answer  : the full _advance_lecture_session flow, same as a real
                   student answering a lecture question (correct/wrong,
                   XP, mistakes-bank recording, spaced repetition, next-
                   question delivery — all the real logic, no shortcuts).
  - daily_quiz   : build_daily_quiz_questions + start_daily_quiz, the
                   exact path that crashed in production once already
                   (the mistakes-bank KeyError) and the most expensive
                   per-tap operation in the bot (see the O(n) fixes
                   earlier in the conversation this script came out of).

It does NOT touch Telegram, Railway, or your real channels — everything
that would normally be a network call becomes a fast in-memory stand-in.
That means this measures YOUR CODE'S latency under concurrency, not
Telegram's API latency or network conditions. That's deliberate: your
code is the thing you can actually change, and it's usually the actual
bottleneck (see the O(n) scans this conversation already found and
fixed) — Telegram's rate limiter (AIORateLimiter, already in your
ApplicationBuilder) governs the network side separately, and no amount
of load-testing here changes what Telegram allows per second.

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
       python3 loadtest.py --scenario poll_answer --users 700
       python3 loadtest.py --scenario daily_quiz  --users 700
       python3 loadtest.py --scenario both        --users 200 500 700
4. Read the p50/p95/p99 numbers it prints. See "READING THE RESULTS"
   at the bottom of this file for what to actually do with them.

DATA SAFETY — READ THIS BEFORE RUNNING AGAINST YOUR REAL DATA DIRECTORY
------------------------------------------------------------------------
This script calls your bot's REAL functions, which means it calls the
REAL save_*() functions too (save_analytics, save_settings, etc.) — those
write real local JSON files. It does NOT call any backup_*_to_channel
function for real (those are network calls and get stubbed out), so your
Telegram channel backups are never touched. But your LOCAL .json files
in the working directory WILL be modified with fake load-test data
(fake user IDs, fake XP, fake mistakes-bank entries) unless you run this
in an isolated directory.

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
  set_message_reaction/send_document/pin_chat_message/etc. all return
  quickly (optionally with a simulated network delay via --fake-latency-ms)
  instead of calling Telegram. This is the actual trick that makes any
  of this possible without a real bot token or real Telegram access.
- Channel restore/backup calls (restore_*_from_channel,
  backup_*_to_channel): these still run as real code, but since app.bot
  is fake, every Telegram call inside them hits the FakeBot instead —
  they'll no-op harmlessly (FakeBot.get_chat returns an object with no
  pinned_message, so every restore function sees "nothing to restore"
  and returns immediately, same as a brand-new empty channel would).

WHAT DOESN'T GET FAKED (this is still your real code running)
----------------------------------------------------------------
- All scoring/XP/streak/achievement logic
- The mistakes-bank recording and lookup (the exact code that had the
  KeyError bug)
- The spaced-repetition re-ask logic
- The O(1) poll_status_by_mid indexing this conversation added
- JSON file writes via _atomic_write_json (real disk I/O, on your
  machine — this is deliberate: disk I/O latency under concurrent load
  is a real thing worth measuring, not something to fake away)
"""

import argparse
import asyncio
import os
import random
import statistics
import sys
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
class _FakeMessage:
    """Stands in for the Message object python-telegram-bot normally
    returns from send_message/send_poll/send_document. Has just enough
    shape for the bot's own code to work with it (.message_id, .poll.id,
    .chat_id, and an async .edit_text/.edit_message_reply_markup so the
    "message" parameter some functions accept — e.g. start_daily_quiz's
    edit-in-place path — also works)."""

    _next_id = 1000

    def __init__(self, chat_id, is_poll=False):
        _FakeMessage._next_id += 1
        self.message_id = _FakeMessage._next_id
        self.chat_id = chat_id
        if is_poll:
            self.poll = types.SimpleNamespace(id=f"fakepoll-{self.message_id}")

    async def edit_text(self, *a, **k):
        return self

    async def edit_message_text(self, *a, **k):
        return self

    async def edit_message_reply_markup(self, *a, **k):
        return self


class _FakeChat:
    """Stands in for Chat, as returned by bot.get_chat(). pinned_message
    is always None — meaning every restore_*_from_channel function this
    bot has will see "nothing pinned yet" and return immediately, exactly
    like a freshly-created empty channel would. That's the right behavior
    for a load test: it should measure hot-path latency, not restore
    logic, and it should never require real channel IDs to work."""

    def __init__(self, chat_id):
        self.id = chat_id
        self.pinned_message = None


class FakeBot:
    """Replaces app.bot. Every method Telegram would normally serve over
    the network is implemented here as a fast in-memory stand-in, with an
    optional artificial delay (--fake-latency-ms) to approximate real
    network round-trip time if you want more realistic absolute numbers.
    Relative numbers (how latency changes as concurrency increases) are
    meaningful even at zero fake latency, since they isolate your code's
    own scaling behavior from network variance."""

    def __init__(self, fake_latency_ms: float = 0.0):
        self.fake_latency_ms = fake_latency_ms
        self.call_counts: dict = {}

    async def _delay(self):
        self.call_counts["_total"] = self.call_counts.get("_total", 0) + 1
        if self.fake_latency_ms:
            await asyncio.sleep(self.fake_latency_ms / 1000)

    def _count(self, name):
        self.call_counts[name] = self.call_counts.get(name, 0) + 1

    async def send_message(self, chat_id, text=None, **kwargs):
        self._count("send_message")
        await self._delay()
        return _FakeMessage(chat_id)

    async def send_poll(self, chat_id, question, options, **kwargs):
        self._count("send_poll")
        await self._delay()
        return _FakeMessage(chat_id, is_poll=True)

    async def send_document(self, chat_id, document=None, **kwargs):
        self._count("send_document")
        await self._delay()
        return _FakeMessage(chat_id)

    async def set_message_reaction(self, chat_id, message_id, **kwargs):
        self._count("set_message_reaction")
        await self._delay()
        return True

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self._count("pin_chat_message")
        await self._delay()
        return True

    async def unpin_chat_message(self, chat_id, **kwargs):
        self._count("unpin_chat_message")
        await self._delay()
        return True

    async def delete_message(self, chat_id, message_id, **kwargs):
        self._count("delete_message")
        await self._delay()
        return True

    async def edit_message_text(self, chat_id, message_id, text=None, **kwargs):
        self._count("edit_message_text")
        await self._delay()
        return _FakeMessage(chat_id)

    async def edit_message_reply_markup(self, chat_id, message_id, **kwargs):
        self._count("edit_message_reply_markup")
        await self._delay()
        return _FakeMessage(chat_id)

    async def get_chat(self, chat_id, **kwargs):
        self._count("get_chat")
        await self._delay()
        return _FakeChat(chat_id)

    async def get_file(self, file_id, **kwargs):
        self._count("get_file")
        await self._delay()
        raise RuntimeError("FakeBot.get_file: no real file backing this in a load test")

    async def forward_message(self, *a, **kwargs):
        self._count("forward_message")
        await self._delay()
        return _FakeMessage(kwargs.get("chat_id"))

    # Catch-all so a bot-code path calling some Telegram method this
    # fake doesn't explicitly implement fails LOUDLY and NAMED, instead
    # of a confusing AttributeError deep in python-telegram-bot's own
    # code. If you hit this, add the method above following the same
    # pattern as the others.
    def __getattr__(self, name):
        raise AttributeError(
            f"FakeBot has no fake implementation of bot.{name}() yet — "
            f"add one in loadtest.py's FakeBot class, following the pattern "
            f"of the other methods there."
        )


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
    and return instantly, which measures nothing."""
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


# ─────────────────────────────────────────────────────────────────
# STEP 5 — the two scenarios
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
    REAL build_daily_quiz_questions (the exact function that crashed in
    production from the mistakes-bank KeyError, and the one with the
    O(n)-scan-turned-cache fix from earlier in this conversation) plus
    start_daily_quiz's setup work. This is deliberately the single
    heaviest per-tap operation in the whole bot, so it's the one most
    worth knowing the p95 for."""
    ctx = make_fake_context(mod, fake_bot)
    # Reset this user's daily-quiz gate so every simulated tap actually
    # runs the full build, instead of hitting the "already did it today"
    # short-circuit after the first call.
    entry = mod._get_settings_entry(user_id)
    entry["daily_quiz_last_date"] = None

    t0 = time.perf_counter()
    await mod.start_daily_quiz(ctx, user_id)
    t1 = time.perf_counter()

    mod.DAILY_QUIZ_SESSIONS.pop(user_id, None)
    return (t1 - t0) * 1000


async def run_scenario(mod, fake_bot, scenario: str, num_users: int, year, module, subject) -> RunResult:
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


def percentile(data: list, pct: float) -> float:
    if not data:
        return float("nan")
    s = sorted(data)
    k = (len(s) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def print_report(scenario: str, num_users: int, result: RunResult, fake_bot: FakeBot):
    lat = result.latencies_ms
    print(f"\n{'=' * 60}")
    print(f"  Scenario: {scenario}   Simulated concurrent users: {num_users}")
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


# ─────────────────────────────────────────────────────────────────
# STEP 6 — CLI
# ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bot-path", default="bot.py", help="Path to your bot.py (default: ./bot.py)")
    parser.add_argument("--scenario", choices=["poll_answer", "daily_quiz", "both"], default="both")
    parser.add_argument("--users", type=int, nargs="+", default=[100, 300, 500, 700],
                         help="One or more concurrent-user counts to test, e.g. --users 100 500 700")
    parser.add_argument("--fake-latency-ms", type=float, default=0.0,
                         help="Simulated per-Telegram-call network delay, in ms. "
                              "0 (default) isolates pure code latency. Try 50-150 "
                              "to approximate a realistic round trip and see how "
                              "that compounds under concurrency.")
    parser.add_argument("--lectures", type=int, default=5, help="Fake lectures to seed (default 5)")
    parser.add_argument("--questions-per-lecture", type=int, default=10)
    args = parser.parse_args()

    if not os.path.exists(args.bot_path):
        print(f"ERROR: {args.bot_path} not found. Run this from the same folder as your bot.py, "
              f"or pass --bot-path /path/to/bot.py")
        sys.exit(1)

    print(f"[loadtest] Importing {args.bot_path} with Telegram calls stubbed out...")
    mod, fake_bot = load_bot_module(args.bot_path, args.fake_latency_ms)
    print("[loadtest] Import succeeded. app.bot is now a FakeBot.")

    year, module, subject = seed_fake_data(mod, args.lectures, args.questions_per_lecture)

    scenarios = ["poll_answer", "daily_quiz"] if args.scenario == "both" else [args.scenario]

    for scenario in scenarios:
        for n in args.users:
            fake_bot.call_counts.clear()
            result = asyncio.run(run_scenario(mod, fake_bot, scenario, n, year, module, subject))
            print_report(scenario, n, result, fake_bot)


if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════
# READING THE RESULTS
# ═══════════════════════════════════════════════════════════════════════
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
#     bottleneck somewhere in that scenario's path, the same shape as the
#     poll_status_by_mid / daily_quiz_subject_pool bugs already found and
#     fixed earlier — worth profiling further rather than just adding
#     more vCPU, since more CPU won't fix an algorithmic bottleneck, only
#     delay when it becomes visible.
#
#   - Errors appearing only at higher concurrency (not at low counts):
#     usually a race condition on shared in-memory state. Check whether
#     the function under test is missing an @_serialize_per_user-style
#     guard, or two users are mutating the same dict without one waiting
#     for the other.
#
# What this DOESN'T tell you:
#
#   - Real Telegram API latency or rate-limit behavior (AIORateLimiter
#     is real code but was never exercised here since FakeBot bypasses
#     it entirely — the rate limiter wraps the bot object PTB constructs
#     internally, and this script swaps the whole bot instance).
#   - Railway's actual CPU/memory ceiling at your current plan — this
#     tells you how your CODE scales, which combined with Railway's
#     metrics (CPU % during a real run, from Railway's own dashboard)
#     lets you decide when to size up. Run this script, watch where p95
#     starts climbing sharply, then cross-reference with Railway's CPU
#     graph during a comparable real traffic burst if you want to map
#     "code latency" to "the vCPU number Railway shows using."
#   - Disk I/O contention exactly as Railway's filesystem would behave —
#     _atomic_write_json's fsync cost depends on the underlying storage,
#     which will differ between your local machine and Railway's volumes.
