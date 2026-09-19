import re
import string
import random
import json
import os
import time
import copy
import asyncio
import functools
import html
import unicodedata
import tempfile
import zipfile
import traceback
from io import BytesIO
from datetime import time as dt_time, datetime, timedelta
from zoneinfo import ZoneInfo

# ═══════════════════════════════════════════════════════════════
# FILE INDEX — where to find things (line numbers approximate;
# section banners below are exact and searchable).
# ═══════════════════════════════════════════════════════════════
#  130   FONT SETUP
#  388   QUIZZY — The Quizician's cat friend (persona/flavor text)
#  432   BOT MESSAGES — every user-facing string, in one place
#  489   USERS STORAGE
#  505   ANALYTICS — XP · LEVELS · ACHIEVEMENTS (also: telegram_name /
#          telegram_username via _update_telegram_name). ACHIEVEMENTS
#          holds 7 tiered categories: questions_answered, correct_streak,
#          lectures_completed, xp_levels, daily_quiz, daily_streak (tier
#          counts vary 4-6, see the dict itself and ACHIEVEMENT_STAT_FIELD
#          for which analytics field each checks), and the meta category
#          achievement_collector — thresholds check
#          _total_achievements_unlocked (every OTHER category's unlocked
#          tiers + unlocked extras) rather than a normal field, and its
#          tiers grant a permanent entry["xp_multiplier"] instead of a
#          one-time XP bonus; every _award_xp call scales by it, and tier
#          5 (only reachable once literally everything else is unlocked)
#          renames to "The Quizician". EXTRA_ACHIEVEMENTS holds 5 one-off
#          (non-tiered) awards — quick_thinker, basmagy, perfect_run,
#          insomniac, curious (the last checked via _settings_customized,
#          no dedicated tracker) — unlocked
#          via _check_extra_achievement and stored in
#          entry["achievements"]["extras"]. New counters this system
#          added: lectures_completed (_finish_lecture_session, non-retake
#          only) and daily_quizzes_completed (_advance_daily_quiz_session,
#          on completion).
#  927   SETTINGS — per-user personalization (nickname, reactions,
#          auto_next, randomize)
#        🔎 SEARCH CONTENT — "Search Content 🔎" button on the Quizzes
#          module list (and the rare no-locked-year fallback picker).
#          search_pick_year:/search_year:/search_mod: callbacks (grep
#          "SEARCH CONTENT" in button_handler) walk year -> module (or
#          "All Modules") -> arms AWAITING_SEARCH_QUERY, then the user's
#          next text message is matched against every ready lecture's
#          poll questions via _search_quiz_questions (Arabic-aware
#          substring match, same normalization as the nickname filter).
#          Results are resent as fresh live quiz polls straight into the
#          chat via deliver_quiz (channel is private, so linking out
#          isn't reliable). See _send_search_results for the shared send
#          used by both the search itself and its "🔎 Search Again" button.
# 1114   LECTURE RESULTS — per-lecture leaderboard (own file + own
#          backup channel: LECTURE_RESULTS_GROUP_ID)
# 1267   MISTAKES BANK — per-user wrong-answer pool that seeds each user's
#          own Daily Quiz "questions you got wrong before" slice. Entries
#          are lightweight {user_id, mid, year, module, subject}
#          REFERENCES, not full question snapshots — record_mistake()
#          stores just the id; _resolve_mistake(s) turns a reference back
#          into a full question dict on demand via _snapshot_from_mid +
#          QUIZ_POLL_STATUS[year].
#          Grep "_resolve_mistake" for every call site that needs resolved
#          content (_build_daily_quiz_questions, start_mistakes_retake).
# 1749   MISTAKES BANK RETAKE — 🧠 Mistakes Bank menu button, one-shot
#          practice quiz over _scoped_mistakes_bank() (resolved first)
# 1863   PASSWORD-GATED STORAGE (private group) — unrelated to quiz
#          content; this is the password-triggered media vault. Its backup
#          file is quizician_storage_backup.json (singular, no year
#          suffix) — do NOT confuse with each year's Quizician_Quiz_Backup
#          file below, despite the similar "storage/backup" wording.
# 1986   QUIZ CHANNELS (per-year: YEARS registry near the top of the file
#          controls channel_id + curriculum per year; index/state/poll-status/
#          backup are all keyed by year, e.g. QUIZ_INDEX[year]). Each
#          year's pinned backup document is named
#          Quizician_Quiz_Backup_Y1.json / _Y2.json / _Y3.json
#          (backup_quiz_to_channel) and contains quiz_index + quiz_state +
#          quiz_poll_status for that year — this is what MISTAKES BANK
#          entries resolve against.
# 2212   STATE (in-memory dicts: LECTURE_SESSIONS, QUIZ_POLL_STATUS, etc.
#          QUIZ_POLL_STATUS is persisted per-year alongside QUIZ_INDEX/
#          QUIZ_STATE — see QUIZ CHANNELS above. LECTURE_SESSIONS,
#          DAILY_QUIZ_SESSIONS, and MISTAKES_RETAKE_SESSIONS are now also
#          persisted — see SESSION PERSISTENCE (grep the banner) for the
#          local-file + SESSIONS_GROUP_ID channel backup, restored in
#          _post_init. A crash within the ~30s SESSIONS_BACKUP_MIN_INTERVAL
#          window can still lose that window's worth of session progress,
#          same accepted risk shape as the analytics flush.)
# 2238   CONSTANTS
# 2247   PER-USER SERIALIZATION — @_serialize_per_user decorator, applied
#          to handle_poll_answer and button_handler. Needed because
#          concurrent_updates() (see MAIN, near the bottom) lets different
#          users' updates run truly concurrently now; this keeps each
#          individual user's own updates ordered against each other via a
#          private asyncio.Lock per user_id, without touching either
#          handler's body.
# 2287   HELPERS
# 2446   QUIZ DELIVERY (single source of truth for sending a live quiz poll)
# 2619   KEYBOARD HELPERS (main menu, settings menu, etc.)
# 2706   MENU TEXT CONTENT
# 3127   REACTIONS (react_random, lecture-answer streak reactions)
# 3183   SLEEP / WAKE COMMANDS
# 3195   PASSIVE ANSWER BACKFILL / LECTURE DELIVERY + SESSION LOGIC
#          — _deliver_next_lecture_question, _deliver_all_lecture_questions,
#            handle_poll_answer (@_serialize_per_user), _advance_lecture_session
#            (records mistakes into MISTAKES_BANK via record_mistake(user_id, mid, ...))
# 3567   FORWARDED POLL HANDLER
# 3752   IMAGE HANDLER
# 3917   STORAGE GROUP — AUTO-INDEXING
# 4307   TEXT MESSAGE HANDLER (includes /start's onboarding nickname prompt)
# 4564   INLINE BUTTON HANDLER (button_handler, @_serialize_per_user — all
#          callback_data routing, including lecture preview/leaderboard,
#          lecture start, settings toggles, and the /edit_quiz flow —
#          eqyr:/eqmodule:/eqsubject:/eqlecture:/eqq:/eqdel:/eqins:,
#          admin-only, parallel to yr:/module:/subject:/lecture: — lets an
#          admin drill into a lecture, pick one question (16-char
#          preview), then delete it or reopen the lecture in the quiz
#          channel to insert new poll(s) right after it via
#          QUIZ_INSERT_AFTER, closed the same way as authoring: -END)
# 5437   START (also wakes bot from sleep; asks for a nickname on first use)
# 5505   ADMIN HELPERS
# 5526   BROADCAST COMMAND (admin only)
# 5603   MAIN — ApplicationBuilder here sets .concurrent_updates(256), so
#          updates from different users are handled in parallel instead of
#          one-at-a-time globally (see PER-USER SERIALIZATION above for
#          how same-user ordering is still preserved). Also where
#          _reconcile_backups_job lives: a job_queue.run_repeating() job
#          (every BACKUP_RECONCILE_INTERVAL seconds) that re-checks each
#          backup channel's pin and re-uploads if it's out of sync, so a
#          missed pin/delete on the reactive path gets caught within a few
#          seconds instead of waiting for the next real data change. Also
#          registers _zikr_push_job (job_queue.run_repeating, every 3600s,
#          first fire aligned to the next real clock-hour via
#          _next_top_of_hour_delay) — hourly zikr to every user who hasn't
#          opted out via Settings -> More Settings -> Hourly Zikr
#          (get_zikr_enabled, on by default). Also registers
#          _daily_backup_export_job (job_queue.run_daily,
#          DAILY_BACKUP_EXPORT_HOUR/MIN) — zips every local data file and
#          sends it to ERROR_LOG_GROUP_ID once a day, as a flat-file
#          backup on top of the per-system pinned-message backups.
#
# NOTE ON save_*() FUNCTIONS: all 11 are async, writing via
# asyncio.to_thread(_atomic_write_json, ...) — atomic (temp file + fsync +
# os.replace, so a crash can't leave a half-written JSON file) AND
# non-blocking (the disk I/O runs in a worker thread instead of stalling
# the event loop for every other user while one save is in flight). Every
# call site must `await` them; a few originally-sync helper functions
# (_record_activity, set_daily_quiz_scope, _record_lecture_result,
# record_mistake, _index_item) became async too since they call save_*()
# internally — grep any of these names before adding a new call site.
#
# NOTE: line numbers drift as the file grows — treat them as "roughly
# here", and confirm with a grep for the section banner text if unsure.
# ═══════════════════════════════════════════════════════════════


from telegram import Update, ReactionTypeEmoji, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputFile
from telegram.error import Forbidden, BadRequest, TimedOut, NetworkError, RetryAfter
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    PollHandler,
    PollAnswerHandler,
    filters,
    ContextTypes,
    AIORateLimiter,
)
from telegram.constants import ParseMode

BOT_TOKEN = os.environ["BOT_TOKEN"]  # set this in Railway's Variables tab — never hardcode it
# NOTE: AIORateLimiter (used below when building `app`) needs the extra:
#   pip install "python-telegram-bot[rate-limiter]"
# Add that to requirements.txt too, or the import at the top of this file fails.

# Portable temp dir: tempfile.gettempdir() respects $TMPDIR, so this resolves
# to a writable path on both Railway (/tmp) and Termux ($PREFIX/tmp) — a
# hardcoded "/tmp" fails on Android, which has no writable /tmp.
IMG_BASE_DIR  = os.path.join(tempfile.gettempdir(), "quizician_imgs")

# ── Replace with YOUR Telegram numeric user ID ──────────────────
# To find it: message @userinfobot on Telegram → it replies with your ID
ADMIN_ID = 940770584

# ── Replace with your private GROUP's chat ID ────────────────────
# 1. Create the group, add this bot to it as a member (admin not required
#    unless you want it to survive being demoted/re-added later).
# 2. Add @userinfobot to the same group (or forward a message from the
#    group to it in DM) — it replies with the chat ID (a negative number,
#    e.g. -1001234567890). Paste it below, then remove @userinfobot.
STORAGE_GROUP_ID = -1004447646576

# ── YEARS — one quiz channel + curriculum per academic year ──────────
# /quiz now asks "which year?" first, then drills into that year's own
# modules -> subjects -> lectures, exactly like before. Each year is
# completely isolated: its own Telegram channel, its own quiz index, its
# own backup document. That isolation is the whole point — a single
# combined index/backup eventually outgrows Telegram's practical JSON
# document size as more years/lectures pile in, so splitting by year
# keeps each backup small indefinitely instead of one ever-growing file.
#
# To add/wire up a year's channel:
#   1. Create a channel, add this bot as an ADMIN (channels require admin
#      rights for the bot to receive posts at all).
#   2. Forward any message from that channel to @userinfobot in a private
#      DM — it replies with the channel's chat ID.
#   3. Paste that ID below as that year's "channel_id".
#
# The old single-channel setup (channel -1004402622263) is kept as Year 3
# below, repurposed and started fresh — its lecture index/state/poll-status
# are stored under new "_y3" files, so nothing from the old combined
# quiz_index.json/quiz_state.json/quiz_poll_status.json carries over.
#
# Subject names here are plain text, no emoji — this is the exact string
# admins type in a lecture title ("<Module> - <Subject> Lecture <n>: ...")
# and the exact string stored in QUIZ_INDEX, so it needs to stay simple and
# typeable. Emoji are purely cosmetic and live in SUBJECT_EMOJI below,
# looked up only when rendering a subject as a button label.
YEARS = {
    "y1": {
        "label": "Year 1",
        "channel_id": -1004491934509,
        "modules": {
            "Foundation (1)": ["Anatomy", "Embryology", "Biochemistry", "Histology", "Physiology"],
            "Foundation (2)": ["Pathology", "Pharmacology", "Microbiology", "Parasitology", "Communication skills"],
            "MSK":            ["Anatomy", "Biochemistry", "Histology", "Physiology", "Pathology"],
            "CVS":            ["Physiology", "Anatomy", "Pharmacology", "Pathology", "Histology", "MP"],
        },
    },
    "y2": {
        "label": "Year 2",
        "channel_id": -1004370807195,
        "modules": {
            "Respiratory":  ["Biochemistry", "Anatomy", "Physiology", "Histology", "Pharmacology", "Microbiology", "Pathology"],
            "Blood":        ["Microbiology", "Physiology", "Biochemistry", "Pharmacology", "Parasitology", "Histology", "Pathology", "Psychiatry"],
            "GIT":          ["Anatomy", "Pharmacology", "Parasitology", "Histology", "Pathology", "Physiology", "Microbiology"],
            "CNS 1":        ["Physiology", "Anatomy"],
            "CNS 2":        ["Pharmacology", "Physiology", "Parasitology", "Histology", "Pathology"],
        },
    },
    "y3": {
        "label": "Year 3",
        # This is the existing channel, repurposed — starting fresh.
        "channel_id": -1004402622263,
        "modules": {
            "Endocrine":      ["Biochemistry", "Physiology", "Pathology", "Histology", "Pharmacology"],
            "Genitourinary":  ["Anatomy", "Physiology", "Histology", "Pathology", "Microbiology"],
        },
    },
}
# Display order for the /quiz year picker.
YEAR_ORDER = ["y1", "y2", "y3"]

# Cosmetic-only: emoji shown next to a subject's name on module/subject
# selection buttons. Looked up by the plain subject string above — never
# stored, matched against lecture titles, or used as a dict key anywhere.
# A subject with no entry here just renders without an emoji.
SUBJECT_EMOJI = {
    "Anatomy":              "🩻",
    "Embryology":           "👶🏼",
    "Biochemistry":         "🧬",
    "Histology":            "🔬",
    "Physiology":           "🧠",
    "Pathology":            "🩸",
    "Pharmacology":         "💊",
    "Microbiology":         "🦠",
    "Parasitology":         "🪱",
    "Communication skills": "💬",
    "MP":                   "👨‍⚕️",
    "Psychiatry":           "🏥",
}

def subject_label(subject: str) -> str:
    """Subject string for display: name + its emoji, if it has one in
    SUBJECT_EMOJI. Never use this for storage/matching — always plain
    `subject` for that (parse_lecture_title, QUIZ_INDEX, callback_data)."""
    emoji = SUBJECT_EMOJI.get(subject)
    return f"{subject} {emoji}" if emoji else subject

# Cosmetic-only, same idea as SUBJECT_EMOJI but for module names.
MODULE_EMOJI = {
    "Respiratory": "🫁",
    "Blood":       "🩸",
    "GIT":         "😋",
    "CNS 1":       "⚡️",
    "CNS 2":       "⚡️⚡️",
}

def module_label(module: str) -> str:
    """Module string for display: name + its emoji, if it has one in
    MODULE_EMOJI. Never use this for storage/matching — always plain
    `module` for that (parse_lecture_title, QUIZ_INDEX, callback_data)."""
    emoji = MODULE_EMOJI.get(module)
    return f"{module} {emoji}" if emoji else module


def year_channel_id(year: str):
    return YEARS.get(year, {}).get("channel_id")

def year_label(year: str) -> str:
    return YEARS.get(year, {}).get("label", year)

def year_modules(year: str) -> dict:
    return YEARS.get(year, {}).get("modules", {})

def year_for_chat(chat_id: int):
    """Which year (if any) a given chat/channel ID belongs to."""
    for y, cfg in YEARS.items():
        if cfg.get("channel_id") == chat_id:
            return y
    return None

def configured_years() -> list:
    """Years that actually have a channel_id set — unset ones (TODOs above)
    are silently skipped everywhere (year picker, filters, backups, etc.)
    until someone fills them in."""
    return [y for y in YEAR_ORDER if YEARS.get(y, {}).get("channel_id")]

# Every configured year's channel ID, e.g. for filters.Chat(...) which
# accepts either a single ID or a list of them.
QUIZ_CHANNEL_IDS = [cid for cid in (YEARS[y]["channel_id"] for y in YEARS) if cid]

# ── Dedicated group for analytics JSON backups ────────────────
# The bot pins the latest analytics.json here after every change
# and deletes the previous one — always exactly one file in the group.
ANALYTICS_GROUP_ID = -1003767364410

# ── Dedicated group for user-settings JSON backups ─────────────
# Same pin-and-replace pattern as ANALYTICS_GROUP_ID, but for
# per-user personalization settings (nickname, etc).
SETTINGS_GROUP_ID = -1004423684829

# ── Dedicated group for per-lecture leaderboard/results JSON backups ──
# Same pin-and-replace pattern as ANALYTICS_GROUP_ID, kept in its own
# group (rather than folded into ANALYTICS_GROUP_ID) so a growing
# leaderboard file never risks the analytics backup itself, and vice
# versa. Set this up the same way as the others: create a group, add
# the bot as admin, add @userinfobot to it, paste the ID it replies with below.
LECTURE_RESULTS_GROUP_ID = -1004292587669

# ── Mistakes bank: every wrong lecture answer, kept per-user ──
# Feeds each user's own "questions you got wrong before" slice of the
# Daily Quiz. Given by the user directly (already an existing group/channel).
MISTAKES_BANK_GROUP_ID = -1004394139690

# ── Dedicated group the bot posts crash/error reports to ────────────
# Not a backup destination like the ones above — just a plain group the
# bot sends a message to whenever an update handler raises an
# unhandled exception. See the global error handler near app setup.
ERROR_LOG_GROUP_ID = -1004333428419

# ── User-submitted issue reports land here (see /report_issue) ──────
# Each report is its own message with a "↩️ Reply" button; the admin's
# next text message in this group becomes the reply, then the message is
# edited to show both sides with fresh Reply/Close buttons. A plain group
# the bot posts to — not a backup destination.
REPORT_ISSUE_GROUP_ID = -1004331095016

# ── Dedicated group for LECTURE_SESSIONS/DAILY_QUIZ_SESSIONS/
# MISTAKES_RETAKE_SESSIONS JSON backups ──────────────────────────────
# Unlike every group above, this backs up data that changes on almost
# every answered question across every active user, so it deletes the
# previous pinned message on each new upload instead of keeping every
# backup forever (see the SESSION PERSISTENCE section for the full
# reasoning). Set up the same way as the others: create a group, add
# the bot as admin, add @userinfobot to it, paste the ID it replies with.
SESSIONS_GROUP_ID = -1004499530524

# ── Curriculum structure for the quiz channels ─────────────────────
# Each year in YEARS (above) has its own "modules" dict in this same shape.
# Lecture titles posted in a year's quiz channel must be formatted as:
#   "<Module> - <Subject> Lecture <number>: <name>"
#   e.g. "Endocrine - Physiology Lecture 3: Insulin Signaling"
# Matching is case-insensitive; the canonical spelling from that year's
# "modules" dict is what gets stored/displayed.

# ═══════════════════════════════════════════════════════════════
# QUIZZY — The Quizician's cat friend 🐾
# ═══════════════════════════════════════════════════════════════
QUIZZY_WELCOME_ART = (
    " /\\_/\\ \n"
    "( ⌒.⌒ )\n"
    "  > ^ <  "
)
QUIZZY_SLEEPING_ART = (
    " /\\_/\\ \n"
    "(  -.- ) zzz\n"
    " > ^ <  "
)
# No "oops" expression was provided yet — this one's improvised to match
# the same style. Swap QUIZZY_OOPS_ART for a real one whenever you draw it.
QUIZZY_OOPS_ART = (
    " /\\_/\\ \n"
    "( ×_× )\n"
    " > ~ <  "
)
QUIZZY_AMAZED_ART = (
    " /\\_/\\ \n"
    "( ✦.✦ )\n"
    "  > ^ <  "
)

QUIZZY_WELCOME_LINES = [
    "صباح (أو مساء) الورد 🌹",
    "باشا البلد",
    "الله أكبر أخيرا قررت تذاكر",
]
QUIZZY_SUCCESS_LINES = [
    "تحياتي 🫡",
    "مش بقول باشا 😎",
    "قدوة 😌🙌",
]
QUIZZY_ERROR_LINES = [
    "كويزي وقع على دماغه من الصدمة، بس متقلقش هنظبطها 😓",
    "كويزي شايف إن المشكلة دي معندهاش داعي، جرب تاني 😓",
    "احنا مش عارفين إيه اللي حصل، بس كويزي واثق إنها هتتحل 😓",
    "كله بسبب قسم الفسيو 😓",
]

def quizzy_block(art: str, line: str) -> str:
    """Quizzy's ASCII art + one of his lines, wrapped for Telegram HTML.
    The art contains literal < > characters (whiskers/paws) which Telegram's
    HTML parser would otherwise choke on as broken tags — escape them."""
    return f"<pre>{html.escape(art)}</pre>\n<i>{html.escape(line)}</i>"

# ═══════════════════════════════════════════════════════════════
# BOT MESSAGES — every user-facing string the bot sends, in one place.
# Grouped by feature. Dynamic ones use {placeholders} filled with .format().
# (Content generated in a loop — like /c's command list, /quiz_list's
# lecture rows — stays where it's built, since there's
# nothing fixed to centralize there; only their static labels live here.)
# ═══════════════════════════════════════════════════════════════

# ── Generic / shared ──────────────────────────────────────────
MSG_ADMIN_ONLY = "🚫 للأدمن فقط"

# ── /cancel ────────────────────────────────────────────────────
MSG_CANCEL_DONE = "❌ تم نطر أبلكاش"
MSG_CANCEL_NOTHING = "بتلغيني أنا يعني ولا أي🤨"

# ═══════════════════════════════════════════════════════════════
# JSON HELPERS
# ═══════════════════════════════════════════════════════════════
def _atomic_write_json(path: str, data, **dump_kwargs):
    """Writes JSON to `path` atomically: dump to a temp file in the same
    directory, flush + fsync it to disk, then os.replace() it over the
    real path. os.replace is atomic on both POSIX and Windows (same
    filesystem), so a crash/power loss can only ever leave either the old
    file or the fully-written new one — never a half-written/truncated
    one. Every save_*() function in this file should go through this
    instead of `open(path, "w")` + json.dump directly."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)   # atomic rename, same filesystem
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

def _backup_filename(base: str) -> str:
    """base.json -> base_20260910T143201Z.json. Every backup upload now
    gets a distinct, sortable filename instead of reusing the same name —
    paired with no longer deleting the previous backup message in every
    backup_*_to_channel function, this means every backup ever taken
    stays in its channel, each individually identifiable by caption +
    timestamp. restore_cmd (/restore) reads this back off the filename to
    show the admin what they're picking between."""
    from datetime import timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name, _, ext = base.rpartition(".")
    return f"{name}_{stamp}.{ext}" if name else f"{base}_{stamp}"

# ═══════════════════════════════════════════════════════════════
# USERS STORAGE
# ═══════════════════════════════════════════════════════════════
USERS_FILE = "users.json"

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

async def save_users():
    await asyncio.to_thread(_atomic_write_json, USERS_FILE, list(USERS), ensure_ascii=False)

USERS = load_users()

# ═══════════════════════════════════════════════════════════════
# ANALYTICS — XP · LEVELS · ACHIEVEMENTS
#
# analytics.json schema per user:
# {
#   "questions_created": int,
#   "streak":            int,   # daily activity streak — backs "daily_streak" achievements
#   "streak_best":       int,
#   "last_active_date":  "YYYY-MM-DD" | null,
#   "lecture_questions_answered":   int,
#   "lecture_questions_correct":    int,
#   "lecture_questions_incorrect":  int,
#   "lecture_correct_streak_current": int,
#   "lecture_correct_streak_best":    int,
#   "subject_stats":     {module: {subject: "correct/answered"}},  # per-subject accuracy,
#                        # every answered question (lecture, Daily Quiz, retake).
#                        # String-encoded on purpose — see _record_subject_answer.
#   "last_module":       str | None,  # module of the user's most recent answer — /mystats
#                        # falls back to it when no /daily_module scope is set
#   "lectures_completed":       int,   # real (non-retake) lecture completions
#   "daily_quizzes_completed":  int,   # Daily Quiz completions
#   "xp":                int,
#   "xp_multiplier":     float,   # permanent multiplier on every XP award (see
#                                  # _award_xp), granted by "achievement_collector"
#   "level":             int,
#   "achievements": {
#       # tiered categories — see ACHIEVEMENTS for tier counts/thresholds:
#       "questions_answered": 0-6, "correct_streak": 0-5,
#       "lectures_completed": 0-5, "xp_levels": 0-6,
#       "daily_quiz": 0-5, "daily_streak": 0-5,
#       "achievement_collector": 0-5,   # meta: counts unlocks across every
#                                        # OTHER category + extras (see
#                                        # _total_achievements_unlocked);
#                                        # tier 5 renames to "The Quizician"
#                                        # (unlocks only once EVERY other
#                                        # achievement — tiers + extras —
#                                        # is unlocked)
#       "extras": {key: True, ...}   # one-off EXTRA_ACHIEVEMENTS unlocked
#   }
# }
# ═══════════════════════════════════════════════════════════════
ANALYTICS_FILE          = "analytics.json"
ANALYTICS_BACKUP_MARKER = "🗄 QUIZICIAN_ANALYTICS_BACKUP"


# ── Level curve ──────────────────────────────────────────────
# xp_needed(level) = LEVEL_CURVE_A * level^2 + LEVEL_CURVE_B * level
# (a gentle quadratic, not the old pure sqrt curve — see _xp_to_level /
# _level_xp_range for the actual formulas, which invert/evaluate this).
#
# Tuned against a concrete weekly pace: 8 lectures/week, ~40 questions
# each, ~80% accuracy (15xp/correct, 5xp/incorrect, +25 lecture-completion
# bonus) -> ~545 XP/lecture. Target was level 5 in ~4 lectures, level 10
# in ~12, level 15 in ~16 — i.e. steady progress, not accelerating or
# decelerating. These coefficients land on ~5.2 / ~10.6 / ~16.2 lectures
# for those three, and keep costing roughly 6-7 lectures per level all
# the way up: level 50 (~62 lectures, ~1.5 months), level 100
# (~147 lectures, ~4.5 months) — reachable within a school year for a
# dedicated student, instead of the old sqrt curve's multi-year ceiling.
LEVEL_CURVE_A = 2.5
LEVEL_CURVE_B = 550

# ── XP per action ────────────────────────────────────────────
XP_PER_QUESTION   = 10

# ── Achievement definitions ───────────────────────────────────
# Each tiered achievement is (threshold, name, xp_bonus, emoji, quip) —
# quip is a short flavor line shown alongside the unlock announcement
# (see _announce_events) and in the achievements gallery (_send_achievements).
# Tier counts vary per category (4-6), so nothing below assumes exactly 5.
# A name/quip containing the literal token "{nick}" gets that token
# replaced with the unlocking user's own nickname wherever it's actually
# shown — see _personalize_ach_text — so it's a plain string here, not a
# per-user value.
# stat_key → the actual analytics entry field each category's threshold is
# checked against (kept separate from the ACHIEVEMENTS dict key so the
# lookup doesn't silently break if either name changes later).
ACHIEVEMENT_STAT_FIELD = {
    "questions_answered": "lecture_questions_answered",
    "correct_streak":     "lecture_correct_streak_best",
    "lectures_completed": "lectures_completed",
    "xp_levels":          "level",
    "daily_quiz":         "daily_quizzes_completed",
    "daily_streak":       "streak_best",
}
ACHIEVEMENTS = {
    "questions_answered": [
        (100,   "Rookie",      25,  "📚", "لسه يا دوب بنقول يا هادي"),
        (500,   "Apprentice",  75,  "📚", "كدا دخلنا فالجد بقا"),
        (1000,  "Expert",      150, "📚", "دماغك بدأت تنور أهي"),
        (3000,  "Master",      300, "📚", "وصل الكبير"),
        (5000,  "Grandmaster", 600, "📚", "يبني أرحم"),
        (8000,  "Titan",       1200, "📚", "ولا حتى ليفاي يقدر يعملك حاجة"),
    ],
    "correct_streak": [
        (5,   "Hot Start",        20,  "🔥", "ولع يا باشا"),
        (15,  "On Fire",          50,  "🔥", "ولعععع"),
        (25,  "Unstoppable",      125, "🔥", "خلاص كفايه ولعة كدا"),
        (50,  "Monster Streak",   300, "🔥", "💀 كفايه توليع يسطا"),
        (60,  "Legendary Streak", 700, "🔥", "حد يتصل على المطافي"),
    ],
    "lectures_completed": [
        (4,  "First Lecture",     25,  "📖", "هانت متقلقش أول يوم خلص أهو والأجازه قربت 🥹"),
        (8,  "Student",           100, "📖", "Aaaand DOES... I mean DONE!"),
        (15, "Scholar",           200, "📖", "💯 الدنيا بدأت توسع، بس أنت قدها"),
        (25, "Professor",         350, "📖", "😏 دانت تشرحلنا الماده بقا"),
        (50, "Walking Textbook",  700, "📖", "أنت مش هتسيب حاجة لباقي الدفعه؟"),
    ],
    "xp_levels": [
        # xp_levels' own threshold is checked against xp directly for its
        # first tier (First XP), then level for the rest — see the special
        # case in _check_achievements.
        (2000, "First XP",     0,   "⭐", "عبي يابا"),
        (5,    "Level Up!",    50,  "⭐", "ما الدنيا حلوه أهي"),
        (10,   "Rising Star",  100, "⭐", "دانا نسيبلك الطلعه دي بقا"),
        (25,   "Powerhouse",   250, "⭐", "ربنا يعلي مراتبك يبني"),
        (50,   "Legend",       500, "⭐", "مش سهلة دي خالي بالك"),
        (100,  "Ascended",     1000, "⭐", "أنت قفلت البوت مش باقي غير تقفل المادة 😂"),
    ],
    "daily_quiz": [
        (1,   "Daily Visitor", 25,  "📅", "أول يوم مدرسة أول يوم مدرسة!!"),
        (7,   "Dedicated",     75,  "📅", "أسبوع بحاله، دانت رايق بقا"),
        (30,  "Committed",     200, "📅", "الشهر خلص، وأنت لا 💪"),
        (100, "Disciplined",   500, "📅", "الترم قرب يخلص، بس أن شاء الله تلحق تلم الدنيا"),
        # {nick} -> the unlocking user's nickname, resolved at display
        # time (see _personalize_ach_text) — this is the only tiered
        # achievement whose *name* (not just quip) is personalized.
        (200, "{nick} was here", 1500, "📅", "i was there when it was written!"),
    ],
    "daily_streak": [
        (3,   "Three-Peat",       20,  "🔥", "تلاته - صفر لينا"),
        (7,   "Week Warrior",     50,  "🔥", "a week isn't for the weak"),
        (30,  "Monthly Machine",  150, "🔥", "أنت واخدها تحدي شخصي بقا"),
        (50,  "determined",       400, "🔥", "أنت لسه عايش يا بلدينا؟"),
        (100, "Unstoppable",      1000, "🔥", "ميه ميه 😎"),
    ],
    "achievement_collector": [
        # A meta-category: its threshold is checked against the total
        # number of OTHER achievements unlocked (every tiered category
        # above + every unlocked extra — see _total_achievements_unlocked),
        # not a normal analytics field. Every slot's xp_bonus is a permanent
        # XP multiplier, not a one-time XP bonus — see the special case in
        # _check_achievements and how _award_xp applies entry["xp_multiplier"].
        # Tier 5 ("The Quizician") is appended below, right after
        # EXTRA_ACHIEVEMENTS is defined, since its threshold has to equal
        # the total count of every tier + extra that exists. Its quip uses
        # {nick} too, same mechanism as daily_quiz's last tier above.
        (5,  "Achievement Collector", 1.05, "🔍", "حلاوة البدايات"),
        (10, "Achievement Collector", 1.10, "🔍", "عشرة فعين الحاسدين البهم بارك 😤😤"),
        (20, "Achievement Collector", 1.15, "🔍", "this"),
        (30, "Achievement Collector", 1.20, "🔍", "You are making me 'tier' up 🥹"),
    ],
}

# ── "Extras" — one-off achievements, not tiered thresholds ─────────
# Each is (name, emoji, xp_bonus, description, quip). Unlocked state lives
# in entry["achievements"]["extras"] as {key: True}, checked by bespoke
# conditions at the relevant event (see _check_extra_achievement and its
# call sites) rather than a single numeric stat. "curious" is checked by
# _settings_customized/_maybe_award_curious — no dedicated tracker field,
# just a live diff of the user's SETTINGS entry against
# _blank_settings_entry() every time a preference toggle saves. It needs
# every watched setting (all except Daily Notification / Hourly Zikr) to be
# off its default at the same moment.
EXTRA_ACHIEVEMENTS = {
    "quick_thinker": ("Quick Thinker", "⚡️", 100, "خلصت محاضرة في أقل من 15 دقيقة",         "سرعة نبيهه ⚡️"),
    "basmagy":       ("Basmagy",       "😎", 150, "خلصت الـ Daily Quiz في أقل من 60 ثانية بـ 100%", "قوة بصمجتك محتاجة تدرس"),
    "perfect_run":   ("Perfect Run",   "💯", 100, "خلصت محاضرة كاملة بـ 100%",                "متكلمنيش عن البيرفكشونزم"),
    "insomniac":     ("Insomniac",     "🌚", 75,  "خلصت كويز بين 2-5 الفجر",                  "النوم دا لضعفاء القلب 👊"),
    "curious":       ("Curious",       "🧐", 50,  "غيّرت كل الإعدادات (غير الإشعارات والزكر) عن الوضع الافتراضي",                "هو أنت ديدي أخت ديكستر اللي بتتك على كل الزراير؟ 🤨"),
}

# "achievement_collector" tier 5 — "The Quizician" — only unlocks once
# EVERY other achievement in the whole system (every tier in every other
# category + every extra) has been unlocked. Computed here, right after
# EXTRA_ACHIEVEMENTS exists, so the threshold always tracks the real total
# instead of a hardcoded number that would silently drift the moment a
# tier or extra is added/removed elsewhere in this file. Its quip's
# {nick} is resolved to the unlocking user's own nickname at display time
# — see _personalize_ach_text.
_TOTAL_ACHIEVEMENTS_POSSIBLE = (
    sum(len(tiers) for key, tiers in ACHIEVEMENTS.items() if key != "achievement_collector")
    + len(EXTRA_ACHIEVEMENTS)
)
ACHIEVEMENTS["achievement_collector"].append((
    _TOTAL_ACHIEVEMENTS_POSSIBLE, "The Quizician", 1.25, "🪄",
    "you have become THE QUIZICIAN... The Creator sends you his kindest regards, "
    "Thanks for quizzing along, Dr.{nick} ❤️",
))

LEVEL_TITLES = {
    0:   "Beginner",
    5:   "Active",
    10:  "Pro",
    15:  "Expert",
    20:  "Master",
    25:  "Sage",
    30:  "Mythic",
    40:  "Superhuman",
    50:  "Genius",
    60:  "Emperor of Quizzes",
    70:  '"HIM"',
    80:  "The star",
    90:  "The Legend",
    100: "The Quizician.",
}

def _level_title(level: int) -> str:
    title = LEVEL_TITLES[0]
    for threshold, t in LEVEL_TITLES.items():
        if level >= threshold:
            title = t
    return title

def _xp_to_level(xp: int) -> int:
    """Inverts xp_needed(level) = A*level^2 + B*level for level, via the
    quadratic formula (positive root only — level can't be negative),
    then floors it. See LEVEL_CURVE_A/B for the tuning behind this, and
    _level_xp_range for why thresholds are ceil'd — this function relies
    on that ceiling to guarantee _xp_to_level(threshold) always lands
    exactly on the right level. A tiny epsilon absorbs sqrt's own
    floating-point rounding noise right at a threshold."""
    import math
    if xp <= 0:
        return 0
    a, b = LEVEL_CURVE_A, LEVEL_CURVE_B
    level = (-b + math.sqrt(b ** 2 + 4 * a * xp)) / (2 * a)
    return int(math.floor(level + 1e-9))

def _level_xp_range(level: int) -> tuple[int, int]:
    """(xp_start, xp_end) for this level. Ceils the formula's result
    (which can be fractional, e.g. LEVEL_CURVE_A*1^2 + LEVEL_CURVE_B*1 =
    552.5 for level 1) up to a whole number — XP awarded is always an
    int, so a level's true threshold is the first whole XP value at or
    above the formula's output, not a truncation of it."""
    import math
    def xp_needed(lvl: int) -> int:
        return math.ceil(LEVEL_CURVE_A * lvl ** 2 + LEVEL_CURVE_B * lvl)
    return xp_needed(level), xp_needed(level + 1)

def _blank_entry() -> dict:
    return {
        "questions_created": 0,
        "streak":            0,
        "streak_best":       0,
        "last_active_date":  None,
        "lecture_questions_answered":   0,
        "lecture_questions_correct":    0,
        "lecture_questions_incorrect":  0,
        "lecture_time_spent_seconds":   0.0,  # cumulative time-to-answer across every
                                               # lecture question ever answered, timed
                                               # question-delivered -> question-answered
                                               # (or timed out). See _record_time_spent
                                               # and the year leaderboard, which ranks
                                               # by correct count first, this second.
        "lecture_correct_streak_current": 0,
        "lecture_correct_streak_best":    0,
        "subject_stats":             {},  # {module: {subject: "correct/answered"}} — see
                                            # _record_subject_answer / _send_mystats
        "last_module":               None,  # module of the most recent answered question
        "lectures_completed":        0,   # real (non-retake) lecture completions —
                                            # see _finish_lecture_session. Backs the
                                            # "lectures_completed" achievement category.
        "daily_quizzes_completed":   0,   # Daily Quiz completions — see
                                            # _advance_daily_quiz_session. Backs the
                                            # "daily_quiz" achievement category.
        "xp":                0,
        "xp_multiplier":     1.0,   # global XP multiplier from the "achievement_collector"
                                     # meta-achievement (see ACHIEVEMENTS/_award_xp) — applied
                                     # to every XP award, this one included.
        "level":             0,
        "achievements":      {**{k: 0 for k in ACHIEVEMENTS}, "extras": {}},
        "daily_medals":      {"gold": 0, "silver": 0, "bronze": 0},  # lifetime Daily Quiz
                                                                       # leaderboard finishes —
                                                                       # see _finalize_daily_leaderboard
        "telegram_name":     None,   # full display name (first + last), Telegram side
        "telegram_username": None,   # @handle, without the @, or None if not set
        "nickname":          None,   # bot-side nickname (see SETTINGS/get_nickname) —
                                      # duplicated here so the analytics backup is
                                      # readable on its own without cross-referencing
                                      # the settings backup.
    }

def _is_valid_analytics_entry(e) -> bool:
    """A minimal shape check for one ANALYTICS[user_id] entry — not
    "has every key" (see _get_entry's own setdefault backfill for that,
    which already handles a schema that's grown fields over time), just
    "is this a dict at all, and are the fields most read directly by
    key (without going through _get_entry first) actually the right
    type." Guards against the kind of corruption that isn't a missing
    key but a wrong-shaped value entirely — e.g. a restore that hands
    back a string or a list where a dict belongs."""
    if not isinstance(e, dict):
        return False
    if "achievements" in e and not isinstance(e["achievements"], dict):
        return False
    if "daily_medals" in e and not isinstance(e["daily_medals"], dict):
        return False
    if "subject_stats" in e and not isinstance(e["subject_stats"], dict):
        return False
    for k in ("questions_created", "streak", "streak_best", "lecture_questions_answered",
              "lecture_questions_correct", "lecture_questions_incorrect", "lecture_time_spent_seconds",
              "lectures_completed", "daily_quizzes_completed", "xp", "xp_multiplier", "level"):
        if k in e and not isinstance(e[k], (int, float)):
            return False
    return True

def _clean_analytics_dict(raw: dict) -> dict:
    """Filters a whole ANALYTICS-shaped dict ({user_id_str: entry}),
    dropping any entry that fails _is_valid_analytics_entry. Used by both
    load_analytics and restore_analytics_from_channel so a bad entry from
    either source is caught the same way, before it can crash something
    that reads a field directly off it."""
    if not isinstance(raw, dict):
        print(f"ANALYTICS: top-level data wasn't a dict ({type(raw).__name__}) — ignoring entirely.")
        return {}
    clean = {
        k: v for k, v in raw.items()
        if isinstance(k, str) and k.lstrip("-").isdigit() and _is_valid_analytics_entry(v)
    }
    if len(clean) != len(raw):
        print(f"ANALYTICS: dropped {len(raw) - len(clean)} malformed entr(y/ies).")
    return clean

def load_analytics() -> dict:
    if os.path.exists(ANALYTICS_FILE):
        with open(ANALYTICS_FILE) as f:
            raw = json.load(f)
        return _clean_analytics_dict(raw)
    return {}

async def save_analytics():
    # deepcopy BEFORE handing off to the background thread: to_thread runs
    # _atomic_write_json (and therefore json.dump, which iterates the
    # whole structure) on a separate OS thread while the event loop keeps
    # running — any coroutine that mutates ANALYTICS while that thread is
    # mid-iteration (e.g. another user's answer landing at the same
    # moment) races json.dump and can throw "dictionary changed size
    # during iteration" or worse, write corrupt/partial JSON. A snapshot
    # copy freezes what gets written; the live dict stays free to mutate.
    snapshot = copy.deepcopy(ANALYTICS)
    await asyncio.to_thread(_atomic_write_json, ANALYTICS_FILE, snapshot, indent=2, ensure_ascii=False)

ANALYTICS: dict = load_analytics()

# Message ID of the currently pinned analytics backup in ANALYTICS_GROUP_ID.
# Populated on startup by restore_analytics_from_channel; the pin is the
# source of truth — no separate state file needed.
_analytics_backup_msg_id: int | None = None

# Throttle for backup_analytics_to_channel — the channel mirror (upload +
# pin + delete-old-pin, three Telegram calls) gets debounced, since callers
# like lecture-answer XP can fire dozens of times a minute and would
# otherwise risk hitting Telegram's rate limits.
_last_analytics_backup_at: float = 0.0
ANALYTICS_BACKUP_MIN_INTERVAL = 30  # seconds

# ── Local-disk debounce for the hot answer path ──────────────────
# save_analytics() itself (deepcopy + atomic write of the WHOLE file, every
# user's entry, not just the one who just answered) is still called
# directly — and immediately — from low-frequency call sites (restore,
# reset/import, nickname changes): those need the file on disk to be
# correct right away and don't fire often enough for the cost to matter.
#
# The three poll-answer advance functions (_advance_lecture_session,
# _advance_daily_quiz_session, _advance_mistakes_retake_session) are
# different: they're the single hottest path in the bot, firing on every
# answered question from every active user. Calling the real save_analytics()
# there means every answer pays for a full-file deepcopy + fsync'd write,
# scaling with total user count, not with "one answer." Those three now
# call _mark_analytics_dirty() instead — an O(1) flag set, no I/O — and a
# periodic job (_flush_analytics_job, registered in _post_init) does the
# real save every ANALYTICS_FLUSH_INTERVAL seconds if anything changed.
#
# Data-loss window: a hard crash (not a clean restart/shutdown — see
# _post_shutdown) between flushes can lose up to one interval's worth of
# analytics deltas. 60s is deliberately much shorter than the 300s channel
# backup already tolerates, so this isn't a new category of risk, just a
# smaller version of one already accepted elsewhere in this file.
_analytics_dirty: bool = False
ANALYTICS_FLUSH_INTERVAL = 60  # seconds

def _mark_analytics_dirty() -> None:
    global _analytics_dirty
    _analytics_dirty = True

async def _flush_analytics_if_dirty() -> None:
    """Writes ANALYTICS to disk only if something changed since the last
    flush. Called by the periodic job and by _post_shutdown for a final
    flush on clean exit."""
    global _analytics_dirty
    if not _analytics_dirty:
        return
    _analytics_dirty = False
    await save_analytics()

def _today() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _get_entry(user_id: int) -> dict:
    key   = str(user_id)
    entry = ANALYTICS.setdefault(key, _blank_entry())
    # backfill missing keys for users created before this system
    if "streak_best" not in entry:
        # streak_best is new — backfill from their current streak (not 0)
        # so an existing user with an active streak doesn't look like
        # they've never had one; there's no historical data to do better.
        entry["streak_best"] = entry.get("streak", 0)
    for k, v in _blank_entry().items():
        entry.setdefault(k, v)
    if not isinstance(entry.get("achievements"), dict):
        entry["achievements"] = {k: 0 for k in ACHIEVEMENTS}
    for k in ACHIEVEMENTS:
        entry["achievements"].setdefault(k, 0)
    if not isinstance(entry["achievements"].get("extras"), dict):
        entry["achievements"]["extras"] = {}
    if not isinstance(entry.get("subject_stats"), dict):
        entry["subject_stats"] = {}
    if not isinstance(entry.get("daily_medals"), dict):
        entry["daily_medals"] = {"gold": 0, "silver": 0, "bronze": 0}
    for k in ("gold", "silver", "bronze"):
        entry["daily_medals"].setdefault(k, 0)
    return entry

def _record_subject_answer(entry: dict, module: str | None, subject: str | None, is_correct: bool) -> None:
    """Bumps this user's per-subject tally for one answered question and
    remembers which module it was in (entry["last_module"]).

    Stored as {module: {subject: "correct/answered"}} — a short string
    rather than a [correct, answered] list, because analytics.json is
    written with indent=2 and a JSON list expands to four lines per
    subject; the string stays on one line, which keeps the file (and the
    channel backup document) noticeably smaller across thousands of users.

    Silently ignores a missing module/subject (legacy sessions, or a
    session shape that never carried them) rather than raising — this
    sits on the hot path of every answered question and must never be
    able to break someone's quiz flow."""
    if not module or not subject:
        return
    stats = entry.setdefault("subject_stats", {})
    subj_map = stats.setdefault(module, {})
    correct, answered = _parse_subject_stat(subj_map.get(subject))
    subj_map[subject] = f"{correct + (1 if is_correct else 0)}/{answered + 1}"
    entry["last_module"] = module

def _parse_subject_stat(value) -> tuple[int, int]:
    """'88/120' -> (88, 120). Anything malformed -> (0, 0) so one bad
    value from a restore can't crash /mystats or the answer path."""
    try:
        c, a = str(value).split("/", 1)
        c, a = int(c), int(a)
        if 0 <= c <= a:
            return c, a
    except (ValueError, TypeError):
        pass
    return 0, 0

def _current_module_for_stats(entry: dict) -> str | None:
    """The module /mystats reports on: the admin-set /daily_module scope
    when there is one (that's the module currently being taught), else the
    module of the user's own most recent answer, else None."""
    scope = get_daily_quiz_scope()
    if scope and scope.get("module"):
        return scope["module"]
    return entry.get("last_module")

def _subject_ranking(entry: dict, module: str | None) -> list[tuple[str, float, int, int]]:
    """[(subject, pct, correct, answered), ...] for `module`, strongest
    first. Ties on percentage break toward MORE answered questions (a
    90% over 200 questions is more trustworthy than a 90% over 10), then
    alphabetically so the order is stable between /mystats calls.
    Subjects with zero answers are omitted."""
    if not module:
        return []
    rows = []
    for subject, value in entry.get("subject_stats", {}).get(module, {}).items():
        correct, answered = _parse_subject_stat(value)
        if answered:
            rows.append((subject, correct / answered * 100, correct, answered))
    rows.sort(key=lambda r: (-r[1], -r[3], r[0]))
    return rows

def _record_time_spent(user_id: int, seconds: float | None) -> None:
    """Adds to a user's cumulative lecture_time_spent_seconds — see the
    field's own comment in _blank_entry for what it measures and why.
    Silently ignores None/negative/absurd values (clock skew, a session
    that somehow never recorded a delivery time) rather than letting one
    bad reading corrupt a running total that can never be un-summed."""
    if seconds is None or seconds < 0 or seconds > 3600:
        return
    entry = _get_entry(user_id)
    entry["lecture_time_spent_seconds"] = entry.get("lecture_time_spent_seconds", 0.0) + seconds
    _mark_analytics_dirty()

def _format_duration(seconds: float) -> str:
    """1234.5 -> '20m 34s' (or just '34s' under a minute) — used on the
    year leaderboard next to each user's correct count."""
    total = int(seconds)
    m, s = divmod(total, 60)
    return f"{m}m {s}s" if m else f"{s}s"

def _year_leaderboard(year_class: str, limit: int = 15) -> list[dict]:
    """Top users in one Year/Class cohort (SETTINGS' year_class — see the
    onboarding question, NOT the quiz-browsing YEARS), ranked by all-time
    lecture_questions_correct first (highest first), then by accuracy
    (correct / (correct + incorrect), highest first) as the tiebreaker.
    Only counts users who've actually set a Year/Class — that's the whole
    filter, since ANALYTICS itself isn't year-scoped, SETTINGS is."""
    rows = []
    for uid_str, entry in ANALYTICS.items():
        if not (isinstance(uid_str, str) and uid_str.lstrip("-").isdigit()):
            continue   # not a real Telegram user id — corrupted/stray key, skip rather than crash
        uid = int(uid_str)
        if get_year_class(uid) != year_class:
            continue
        correct = entry.get("lecture_questions_correct", 0)
        if correct <= 0:
            continue   # no lecture activity yet — nothing to rank
        incorrect = entry.get("lecture_questions_incorrect", 0)
        answered  = correct + incorrect
        accuracy  = (correct / answered * 100) if answered else 0.0
        rows.append({
            "user_id":  uid,
            "name":     get_nickname(uid) or entry.get("telegram_name") or f"مستخدم #{uid % 10000}",
            "correct":  correct,
            "accuracy": accuracy,
        })
    rows.sort(key=lambda r: (-r["correct"], -r["accuracy"]))
    return rows[:limit]

def _update_telegram_name(user_id: int, tg_user) -> None:
    """Keeps the Telegram display name/username (and bot nickname) on the
    analytics entry fresh — people rename themselves on Telegram all the
    time, so this just overwrites rather than only filling blanks. tg_user
    is a telegram.User (update.effective_user); no-ops if that's missing."""
    if tg_user is None:
        return
    name = " ".join(p for p in (tg_user.first_name, tg_user.last_name) if p).strip() or None
    entry = _get_entry(user_id)
    entry["telegram_name"]     = name
    entry["telegram_username"] = tg_user.username or None
    entry["nickname"]          = get_nickname(user_id)

def _award_xp(entry: dict, amount: int) -> int:
    """Add XP (scaled by the "achievement_collector" meta-achievement's
    xp_multiplier, if unlocked — see ACHIEVEMENTS), recalculate level.
    Returns new level if levelled up, else 0. The multiplier applies here
    at the single choke point every XP award already goes through, so
    every caller (raw actions, lecture/daily-quiz answers, other
    achievements' own XP bonuses) benefits automatically without needing
    its own awareness of it."""
    scaled = round(amount * entry.get("xp_multiplier", 1.0))
    entry["xp"] += scaled
    new_level    = _xp_to_level(entry["xp"])
    levelled_up  = new_level > entry["level"]
    entry["level"] = new_level
    return new_level if levelled_up else 0

def _total_achievements_unlocked(entry: dict) -> int:
    """Count of every unlocked tier across every OTHER tiered category
    (excluding achievement_collector itself — it can't count toward its
    own threshold) plus every unlocked Extra. Backs the
    "achievement_collector" meta-achievement's own threshold check."""
    ach = entry.get("achievements", {})
    tiers_unlocked = sum(v for k, v in ach.items() if k in ACHIEVEMENTS and k != "achievement_collector")
    extras_unlocked = sum(1 for v in ach.get("extras", {}).values() if v)
    return tiers_unlocked + extras_unlocked

def _personalize_ach_text(text: str, user_id: int) -> str:
    """Substitutes the literal "{nick}" token some achievement
    names/quips use (daily_quiz's last tier, achievement_collector's
    tier 5 quip) with the user's own nickname. A no-op for every other
    achievement, which contains no such token. Call this on any
    name/quip right before it's actually shown to someone — the
    definitions in ACHIEVEMENTS/EXTRA_ACHIEVEMENTS stay generic strings,
    not per-user values."""
    if "{nick}" not in text:
        return text
    return text.replace("{nick}", get_nickname(user_id) or "حد ما")

def _check_achievements(entry: dict, stat_key: str) -> list[dict]:
    """Check one stat against its achievement tiers. Returns list of newly
    unlocked tiers as dicts with keys: name, emoji, xp_bonus, tier, quip
    (1-based, tier counts vary by category — see ACHIEVEMENTS). name/quip
    may still contain an unresolved "{nick}" token — see
    _personalize_ach_text, applied by callers right before display.

    Special cases:
    - "xp_levels" mixes two fields — its first tier (First XP) checks raw
      xp, every tier after that checks level.
    - "achievement_collector" checks _total_achievements_unlocked(entry)
      instead of any ACHIEVEMENT_STAT_FIELD entry, and its 4th tuple slot
      is a permanent XP multiplier (entry["xp_multiplier"]) rather than a
      one-time XP bonus — no _award_xp call for this category, and
      "xp_bonus" in the returned dict holds the new multiplier instead,
      alongside "prev_multiplier" (the value it just replaced, for
      announcing the increase — see _announce_events, which renders this
      category differently).
    Every other category checks a single field throughout, per
    ACHIEVEMENT_STAT_FIELD."""
    tiers    = ACHIEVEMENTS[stat_key]
    current  = entry["achievements"][stat_key]
    unlocked = []
    if stat_key != "achievement_collector":
        field = ACHIEVEMENT_STAT_FIELD.get(stat_key, stat_key)
    for i, (threshold, name, xp_bonus, emoji, quip) in enumerate(tiers):
        tier = i + 1
        if tier <= current:
            continue
        if stat_key == "achievement_collector":
            value = _total_achievements_unlocked(entry)
        elif stat_key == "xp_levels" and i == 0:
            value = entry.get("xp", 0)
        else:
            value = entry.get(field, 0)
        if value >= threshold:
            entry["achievements"][stat_key] = tier
            if stat_key == "achievement_collector":
                prev_multiplier = entry.get("xp_multiplier", 1.0)
                entry["xp_multiplier"] = xp_bonus   # permanent multiplier, not a one-time bonus
                unlocked.append({"name": name, "emoji": emoji, "quip": quip,
                                  "xp_bonus": xp_bonus, "prev_multiplier": prev_multiplier,
                                  "tier": tier, "multiplier": True})
            else:
                unlocked.append({"name": name, "emoji": emoji, "quip": quip,
                                  "xp_bonus": xp_bonus, "tier": tier})
                _award_xp(entry, xp_bonus)
        else:
            break   # tiers are ordered — no point checking higher ones
    return unlocked

def _check_extra_achievement(entry: dict, key: str) -> dict | None:
    """Awards a one-off EXTRA_ACHIEVEMENTS entry if not already unlocked.
    Returns the same shape _check_achievements' list entries use (name,
    emoji, xp_bonus, tier, quip) so _announce_events can treat both the
    same way — tier is always 1 here (extras aren't leveled) with no ⭐
    shown (see _announce_events). Callers are expected to have already
    checked the actual unlock condition; this only handles the "already
    have it" guard + bookkeeping + XP."""
    extras = entry["achievements"].setdefault("extras", {})
    if extras.get(key):
        return None
    name, emoji, xp_bonus, _desc, quip = EXTRA_ACHIEVEMENTS[key]
    extras[key] = True
    _award_xp(entry, xp_bonus)
    return {"name": name, "emoji": emoji, "xp_bonus": xp_bonus, "tier": 1, "extra": True, "quip": quip}

# Preference toggles that count as "poking around in Settings" for the
# "Curious" extra — deliberately excludes nickname/year_class, which
# onboarding requires from everyone and so say nothing about curiosity.
# Every Settings button that has a default state EXCEPT Daily Notification
# and Hourly Zikr (those two are opt-out reminders, not preferences about
# how quizzing itself behaves). "Curious" needs ALL of these off their
# default at the same time — see _settings_customized. Edit Nickname isn't
# listed: onboarding forces everyone to set one, so it has no "default".
_CURIOUS_WATCHED_SETTINGS = (
    "reactions", "auto_next", "randomize", "achievement_notifs",
    "spaced_repetition", "question_timer",
)

def _settings_customized(user_id: int) -> bool:
    """True only while EVERY _CURIOUS_WATCHED_SETTINGS toggle is away from
    its default at the same time. It's a live check, not a running tally:
    flipping one back to its default makes this False again, so a user
    has to reach the state where all of them differ at once. No dedicated
    tracker field for this — just diffs the live SETTINGS entry against a
    fresh _blank_settings_entry() on demand, which is why it's only ever
    worth calling right after a settings mutation (see
    _maybe_award_curious) rather than on some schedule. The achievement
    itself stays unlocked once awarded (see _check_extra_achievement)."""
    entry   = SETTINGS.get(str(user_id), {})
    default = _blank_settings_entry()
    return all(entry.get(k, default[k]) != default[k] for k in _CURIOUS_WATCHED_SETTINGS)

async def _maybe_award_curious(context, user_id: int) -> None:
    """Call right after any of the _CURIOUS_WATCHED_SETTINGS toggles
    below save. Awards "Curious" (mutates the ANALYTICS entry via
    _get_entry — a different dict than the SETTINGS one
    _settings_customized reads, hence _mark_analytics_dirty rather than
    a settings save here) the moment ALL of a user's watched settings
    differ from default at once, then announces it immediately since a
    Settings tap is already an interactive moment, same as any other
    achievement."""
    if not _settings_customized(user_id):
        return
    ach = _check_extra_achievement(_get_entry(user_id), "curious")
    if ach:
        _mark_analytics_dirty()
        await _announce_events(context, user_id, {"achievements": [ach], "level_up": 0})

async def _record_activity(user_id: int, questions_delta: int = 0,
                     persist: bool = True) -> dict:
    """Update all stats. Returns dict of events for the caller to announce:
    { "achievements": [...], "level_up": int | 0 }

    persist=False skips the save_analytics() call at the end — for callers
    (the three poll-answer advance functions) that go on to mutate the
    entry further and call save_analytics() themselves right after, so the
    whole ANALYTICS dict doesn't get deep-copied and written to disk twice
    for the same answer."""
    from datetime import datetime, timezone, timedelta
    today  = _today()
    entry  = _get_entry(user_id)
    last   = entry.get("last_active_date")

    # ── streak ───────────────────────────────────────────────
    if last != today:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        entry["streak"] = (entry["streak"] + 1) if last == yesterday else 1
        entry["last_active_date"] = today
        if entry["streak"] > entry.get("streak_best", 0):
            entry["streak_best"] = entry["streak"]

    # ── counters ─────────────────────────────────────────────
    entry["questions_created"] += questions_delta

    # ── XP for raw actions ───────────────────────────────────
    xp_earned = questions_delta * XP_PER_QUESTION
    new_level  = _award_xp(entry, xp_earned) if xp_earned else 0

    # ── achievement checks ───────────────────────────────────
    newly_unlocked = []
    newly_unlocked += _check_achievements(entry, "daily_streak")
    newly_unlocked += _check_achievements(entry, "achievement_collector")

    # level-up might also happen from achievement XP bonuses
    final_level = _xp_to_level(entry["xp"])
    if final_level > entry["level"]:
        entry["level"] = final_level
        new_level = final_level

    if persist:
        await save_analytics()
    return {"achievements": newly_unlocked, "level_up": new_level}

async def _announce_events(context, chat_id: int, events: dict, settings_uid: int | None = None):
    """Send achievement unlocks and level-up notifications to chat_id.
    settings_uid is whose achievement_notifs setting to check (and whose
    nickname any "{nick}" token in a name/quip resolves to — see
    _personalize_ach_text); defaults to chat_id itself. Level-ups are a
    separate, more significant event and always sent regardless."""
    if settings_uid is None:
        settings_uid = chat_id
    msgs = []

    if get_achievement_notifs_enabled(settings_uid):
        for ach in events.get("achievements", []):
            name = _personalize_ach_text(ach["name"], settings_uid)
            quip = _personalize_ach_text(ach.get("quip", ""), settings_uid)
            if ach.get("multiplier"):
                prev = ach.get("prev_multiplier", 1.0)
                msgs.append(
                    f"{ach['emoji']} <b>إنجاز جديد!</b>\n"
                    f"<b>{name}</b>\n"
                    f"<i>XP Multiplier: x{prev:g} → x{ach['xp_bonus']:g}</i>\n"
                    f"<i>{quip}</i>"
                )
                continue
            stars = "" if ach.get("extra") else (" " + "⭐" * ach["tier"])
            msgs.append(
                f"{ach['emoji']} <b>إنجاز جديد!</b>\n"
                f"<b>{name}</b>{stars}\n"
                f"<i>+{ach['xp_bonus']} XP</i>\n"
                f"<i>{quip}</i>"
            )

    if events.get("level_up"):
        lvl   = events["level_up"]
        title = _level_title(lvl)
        msgs.append(
            f"🎉 <b>ترقية!</b>\n"
            f"وصلت للمستوى <b>{lvl}</b> — <i>{title}</i>"
        )

    for msg in msgs:
        await context.bot.send_message(
            chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML
        )

RESTORE_MAX_ATTEMPTS      = 3   # attempts before giving up on a startup restore
RESTORE_RETRY_DELAY_BASE  = 4   # seconds; multiplied by attempt number (4s, then 8s)

# Whether each system's channel restore succeeded this run. False blocks
# that system's backup_*_to_channel from firing — if the restore never
# got the real data locally, we must NOT let a later backup push a
# fresh/empty local file over the good backup still sitting in the
# channel. Fixed by a restart once the underlying Telegram/network issue
# clears (or manually via /restore_analytics etc. for analytics).
RESTORE_OK = {"analytics": True, "settings": True, "storage": True, "lecture_results": True, "mistakes_bank": True, "report_threads": True, "sessions": True}
RESTORE_OK.update({f"quiz_{y}": True for y in YEARS})  # one flag per year's quiz index

class _RestoreNoBackup(Exception):
    """Raised by a restore _do() when the channel has nothing pinned at
    all, or what's pinned isn't this system's backup (no document, or
    caption doesn't match the marker). Not a failure — local data is
    left exactly as it was and is still trusted (RESTORE_OK stays
    True). Distinguished from other exceptions so the retry loop
    doesn't burn attempts on it, and so /restore's button flow can
    offer a reset (create a fresh file) instead of showing an error."""
    pass


class _RestoreInvalidArchitecture(Exception):
    """Raised by a restore _do() when a pinned backup WAS found and
    downloaded, but its parsed JSON doesn't match the shape this
    system expects (wrong top-level type, missing keys, entries that
    don't look like real records, etc). Local data and the local file
    are left untouched — we never partially apply a backup that might
    be corrupted or from the wrong system."""
    pass


async def _run_restore_with_retries(app, key: str, label: str, do_restore, not_found_hint: str | None = None) -> str:
    """Runs do_restore() (an async no-arg callable doing the actual
    get_chat/get_file/parse work for one system) up to
    RESTORE_MAX_ATTEMPTS times with a short backoff between attempts —
    these failures are almost always a transient Telegram API timeout,
    so a couple retries clear most of them without ever bothering the
    admin. Only if every attempt fails do we DM the admin and mark this
    system unsynced for the session (see RESTORE_OK).

    Returns a short status string so callers (like /restore's button
    flow) can react differently depending on what happened:
      "ok"        — restored successfully.
      "no_backup" — nothing pinned to restore from; local data untouched.
      "invalid"   — a pinned backup exists but failed the architecture
                    check; local data untouched, admin notified.
      "error"     — every attempt raised a real (unexpected) error;
                    local data untouched, admin notified.
    """
    for attempt in range(1, RESTORE_MAX_ATTEMPTS + 1):
        try:
            await do_restore()
            RESTORE_OK[key] = True
            return "ok"
        except _RestoreNoBackup:
            return "no_backup"
        except _RestoreInvalidArchitecture as e:
            print(f"{label.upper()} RESTORE ERROR — pinned backup failed the architecture check, refusing to restore:", e)
            RESTORE_OK[key] = False
            await _notify_admin_sync_failure(
                app, label,
                f"a pinned backup was found but its structure looks wrong, so nothing was restored: {e}",
            )
            return "invalid"
        except Exception as e:
            hint = not_found_hint if (not_found_hint and "chat not found" in str(e).lower()) else None
            print(f"{label.upper()} RESTORE ERROR (attempt {attempt}/{RESTORE_MAX_ATTEMPTS}):", hint or e)
            if attempt < RESTORE_MAX_ATTEMPTS:
                await asyncio.sleep(RESTORE_RETRY_DELAY_BASE * attempt)
            else:
                RESTORE_OK[key] = False
                await _notify_admin_sync_failure(app, label, hint or e)
                return "error"
    return "error"

async def _notify_admin_sync_failure(app, what: str, error):
    """Best-effort DM to ADMIN_ID once every retry has been exhausted.
    Never raises — this runs inside _post_init, and a failure here (bot
    blocked, admin never DM'd it, etc.) must not crash startup."""
    if not ADMIN_ID:
        return
    try:
        await app.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"<pre>{html.escape(QUIZZY_OOPS_ART)}</pre>"
                f"⚠️ <b>Error: failed to fetch {html.escape(what)} — try again later.</b>\n"
                f"<code>{html.escape(str(error))}</code>\n\n"
                f"Gave up after {RESTORE_MAX_ATTEMPTS} attempts. Local data was left as-is — "
                f"I won't touch or re-save the {html.escape(what.lower())} file(s), and I won't "
                f"push a new backup to the channel either, so nothing gets overwritten. "
                f"Restart me once things look stable to retry."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as notify_err:
        print(f"ADMIN SYNC-FAILURE NOTIFY ERROR ({what}):", notify_err)

async def backup_analytics_to_channel(context):
    global _analytics_backup_msg_id, _last_analytics_backup_at
    if not ANALYTICS_GROUP_ID:
        return
    if not RESTORE_OK["analytics"]:
        print("ANALYTICS BACKUP SKIPPED — last restore failed, refusing to overwrite the channel backup.")
        return
    now = time.monotonic()
    if now - _last_analytics_backup_at < ANALYTICS_BACKUP_MIN_INTERVAL:
        return   # backed up recently enough — local save_analytics() already has the latest data
    _last_analytics_backup_at = now
    data = json.dumps(ANALYTICS, indent=2).encode("utf-8")
    try:
        sent = await context.bot.send_document(
            chat_id=ANALYTICS_GROUP_ID,
            document=InputFile(BytesIO(data), filename=_backup_filename("analytics.json")),
            caption=ANALYTICS_BACKUP_MARKER,
        )
    except Exception as e:
        print("ANALYTICS BACKUP ERROR:", e)
        return
    try:
        await context.bot.pin_chat_message(
            chat_id=ANALYTICS_GROUP_ID,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as e:
        print("ANALYTICS PIN ERROR:", e)
    # Previous backups are no longer deleted — every one ever taken stays
    # in the channel, timestamped, so /restore can offer a choice and a
    # bad backup never wipes out the only copy of a good one. The pin
    # just moves to the newest; nothing else needs to change here.
    _analytics_backup_msg_id = sent.message_id

async def restore_analytics_from_channel(app) -> str:
    global _analytics_backup_msg_id
    if not ANALYTICS_GROUP_ID:
        return "not_configured"

    async def _do():
        global _analytics_backup_msg_id
        chat   = await app.bot.get_chat(ANALYTICS_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document or (pinned.caption or "") != ANALYTICS_BACKUP_MARKER:
            raise _RestoreNoBackup()
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        restored = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(restored, dict) or not all(isinstance(v, dict) for v in restored.values()):
            raise _RestoreInvalidArchitecture('expected a JSON object mapping user_id -> stats dict')
        ANALYTICS.update(_clean_analytics_dict(restored))
        await save_analytics()
        _analytics_backup_msg_id = pinned.message_id
        print(f"Restored analytics: {len(ANALYTICS)} user(s).")

    return await _run_restore_with_retries(app, "analytics", "Analytics", _do)



# ═══════════════════════════════════════════════════════════════
# SETTINGS — per-user personalization (nickname, etc.)
#
# settings.json schema per user:
# {
#   "nickname":   str | None,
#   "reactions":  bool,  # emoji reactions on submitted quiz questions
#   "auto_next":  bool,  # sends lecture questions one by one, waiting for
#                        # each answer, instead of all at once
#   "randomize":  bool,  # shuffles question order within a lecture
#   "achievement_notifs": bool,  # DMs a message when an achievement unlocks
#                                # (level-up messages are separate and always sent)
#   "year_class": str | None,  # one of YEAR_CLASS_NUMBER's keys ("y1"/"y2"/"y3") —
#                              # asked once during onboarding, editable later
#                              # in Settings. Not the quiz year picker (YEARS) —
#                              # this is who the person is, for future features
#                              # that need to know their class/cohort.
#   "daily_quiz_last_date": str | None,  # "YYYY-MM-DD" — once-per-day gate for
#                                        # the 💥Daily Quiz💥 button
# }
#
# Mirrors the ANALYTICS system exactly: local JSON file, plus a pinned
# backup in SETTINGS_GROUP_ID that gets replaced (upload + pin + delete
# old pin) on every change and restored from on startup.
# ═══════════════════════════════════════════════════════════════
SETTINGS_FILE          = "settings.json"
SETTINGS_BACKUP_MARKER = "⚙️ QUIZICIAN_SETTINGS_BACKUP"

# Class numbers per academic year, for the onboarding "which year/class are
# you in?" question. Keyed the same as YEARS ("y1"/"y2"/"y3") so this can
# reuse year_label() for display, but kept as its own dict since a person's
# class/cohort is who they are, not which quiz year they're browsing right
# now — those happen to share y1/y2/y3 today but are conceptually separate.
YEAR_CLASS_NUMBER = {
    "y1": 46,
    "y2": 45,
    "y3": 44,
}

def _blank_settings_entry() -> dict:
    return {
        "nickname":  None,
        "reactions": True,
        "auto_next": True,
        "randomize": True,
        "achievement_notifs": True,
        "spaced_repetition": True,   # see get_spaced_repetition_enabled below
        "question_timer": 0,   # seconds a live quiz poll stays open before
                                # auto-closing; 0 = off. Cycles 0 -> 60 -> 30 -> 0.
        "year_class": None,    # "y1"/"y2"/"y3" — see YEAR_CLASS_NUMBER above
        "daily_quiz_last_date": None,   # "YYYY-MM-DD" (UTC) of the last completed Daily Quiz
        "daily_notifs": True,   # the 2pm 💥Daily Quiz💥 push — see get_daily_notifs_enabled
        "zikr_reminders": True,   # hourly automated zikr — see get_zikr_enabled / _zikr_push_job. On by
                                    # default; opt out via Settings -> More Settings -> Hourly Zikr.
        "banned_until": None,   # epoch seconds (time.time()) this user's /ban lifts at, or None if not
                                 # currently banned — see /ban (ban_cmd), get_ban_info, _ban_gate.
        "ban_reason":   None,   # reason string from their most recent /ban (kept after it lifts too).
    }

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    return {}

async def save_settings():
    # See save_analytics for why this snapshot copy is required, not
    # just defensive style — same to_thread-races-live-mutation risk.
    snapshot = copy.deepcopy(SETTINGS)
    await asyncio.to_thread(_atomic_write_json, SETTINGS_FILE, snapshot, indent=2, ensure_ascii=False)

SETTINGS: dict = load_settings()

# Message ID of the currently pinned settings backup in SETTINGS_GROUP_ID.
# Populated on startup by restore_settings_from_channel; the pin is the
# source of truth — no separate state file needed.
_settings_backup_msg_id: int | None = None

# Channel mirror debounce — separate from the local-disk debounce below.
_last_settings_backup_at: float = 0.0
SETTINGS_BACKUP_MIN_INTERVAL = 30  # seconds

# ── Local-disk debounce for the hot settings paths ────────────────
# save_settings() itself (deepcopy + atomic write of EVERY user's settings,
# not just the one who triggered it) is still called directly — and
# immediately — from low-frequency, explicit single-user actions (a
# settings toggle, a nickname change, a ban/unban, a channel restore):
# those want the file on disk correct right away and don't fire often
# enough for the cost to matter.
#
# start_daily_quiz is different: every one of the day's Daily Quiz taps
# (hundreds of users within the same push window) used to pay for a full
# SETTINGS file rewrite just to persist that one user's
# daily_quiz_last_date. That call site now calls _mark_settings_dirty()
# instead — an O(1) flag set, no I/O — and a periodic job
# (_flush_settings_job, registered in _post_init) does the real save every
# SETTINGS_FLUSH_INTERVAL seconds if anything changed. Same accepted
# data-loss window as analytics: a hard crash between flushes can lose up
# to one interval's worth of "already did today's Daily Quiz" gates, which
# just means an affected user could see the Daily Quiz prompt again after
# a crash-restart — not a correctness problem worth an fsync per tap.
_settings_dirty: bool = False
SETTINGS_FLUSH_INTERVAL = 60  # seconds

def _mark_settings_dirty() -> None:
    global _settings_dirty
    _settings_dirty = True

async def _flush_settings_if_dirty() -> None:
    """Writes SETTINGS to disk only if something changed since the last
    flush. Called by the periodic job and by _post_shutdown for a final
    flush on clean exit."""
    global _settings_dirty
    if not _settings_dirty:
        return
    _settings_dirty = False
    await save_settings()

def _get_settings_entry(user_id: int) -> dict:
    key   = str(user_id)
    entry = SETTINGS.setdefault(key, _blank_settings_entry())
    # backfill missing keys for users created before this system
    for k, v in _blank_settings_entry().items():
        entry.setdefault(k, v)
    return entry

def get_nickname(user_id: int) -> str | None:
    return SETTINGS.get(str(user_id), {}).get("nickname")

# ── Nickname vulgarity filter ───────────────────────────────────────
# Blocks a nickname that contains a vulgar/inappropriate English or
# Arabic word — checked at set-time in the AWAITING_NICKNAME handler
# below. Matching is substring-based on a *normalized* form of the
# text (lowercased; Arabic tashkeel/alef-yaa/taa-marbuta variants and
# tatweel collapsed; all spacing/punctuation stripped) so trivial
# tricks like "a s s" or "كُسّ" don't just slip past it. This is a
# simple blocklist, not a full profanity classifier — extend the two
# sets below if something obvious gets through.
_AR_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")  # tashkeel + tatweel
_AR_LETTER_NORM = {
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ة": "ه",
}

def _normalize_for_filter(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _AR_DIACRITICS_RE.sub("", text)
    text = text.translate(str.maketrans(_AR_LETTER_NORM))
    text = text.lower()
    return re.sub(r"[^a-z0-9\u0600-\u06FF]", "", text)

_VULGAR_WORDS_EN = {
    "fuck", "shit", "bitch", "asshole", "bastard", "dick", "pussy",
    "cunt", "slut", "whore", "nigger", "nigga", "faggot", "retard",
    "cock", "twat", "wanker", "motherfucker", "dumbass", "jackass",
}
_VULGAR_WORDS_AR = {
    "كس", "كسمك", "كسم", "طيز", "زبي", "زب", "عرص", "عرصة",
    "شرموطة", "شرموط", "قحبة", "قحبه", "متناك", "متناكة", "متناكه",
    "خول", "منيك", "لبوة", "لبوه", "ابن الكلب", "يلعن",
}
# Precomputed once so every nickname check just does plain substring
# lookups against already-normalized sets.
_VULGAR_WORDS_AR_NORM = {_normalize_for_filter(w) for w in _VULGAR_WORDS_AR}

def _contains_vulgar_word(text: str) -> bool:
    """True if `text` (an attempted nickname) contains a blocked English
    or Arabic word, after normalization."""
    normalized = _normalize_for_filter(text)
    if any(word in normalized for word in _VULGAR_WORDS_EN):
        return True
    return any(word in normalized for word in _VULGAR_WORDS_AR_NORM)

def _get_bool_setting(user_id: int, key: str) -> bool:
    # Defaults to True for anyone not yet in SETTINGS (or missing the key) —
    # matches _blank_settings_entry() defaults, no backfill required to read.
    return SETTINGS.get(str(user_id), {}).get(key, True)

def get_reactions_enabled(user_id: int) -> bool:
    return _get_bool_setting(user_id, "reactions")

def get_auto_next_enabled(user_id: int) -> bool:
    return _get_bool_setting(user_id, "auto_next")

def get_randomize_enabled(user_id: int) -> bool:
    return _get_bool_setting(user_id, "randomize")

def get_achievement_notifs_enabled(user_id: int) -> bool:
    return _get_bool_setting(user_id, "achievement_notifs")

def get_spaced_repetition_enabled(user_id: int) -> bool:
    return _get_bool_setting(user_id, "spaced_repetition")

def get_daily_notifs_enabled(user_id: int) -> bool:
    """Whether this user should still get the 2pm 💥Daily Quiz💥 push —
    see _daily_quiz_push_job. Doesn't affect the quiz itself, which stays
    reachable from the main menu either way."""
    return _get_bool_setting(user_id, "daily_notifs")

def get_zikr_enabled(user_id: int) -> bool:
    """Whether this user gets the hourly automated zikr reminder — see
    _zikr_push_job. On by default, like the other toggles here (opt out
    via Settings -> More Settings -> Hourly Zikr)."""
    return _get_bool_setting(user_id, "zikr_reminders")

def get_ban_info(user_id: int) -> tuple[float | None, str | None]:
    """(banned_until epoch seconds, reason) for an active /ban, or
    (None, None) if the user was never banned or their ban already
    lifted — a lifted ban is treated as not-banned without needing a
    cleanup job, since this just compares against time.time() on every
    call. See ban_cmd (/ban) and _ban_gate."""
    entry = SETTINGS.get(str(user_id), {})
    until = entry.get("banned_until")
    if not until or time.time() >= until:
        return None, None
    return until, entry.get("ban_reason")

def is_banned(user_id: int) -> bool:
    return get_ban_info(user_id)[0] is not None

def get_question_timer_seconds(user_id: int) -> int:
    # Defaults to 0 (off) for anyone not yet in SETTINGS — matches
    # _blank_settings_entry()'s default, no backfill required to read.
    return SETTINGS.get(str(user_id), {}).get("question_timer", 0)

def get_year_class(user_id: int) -> str | None:
    return SETTINGS.get(str(user_id), {}).get("year_class")

def year_class_label(year_class: str | None) -> str:
    """'Year 1 (Class 46)' style label for a year_class value, or a
    placeholder if the person hasn't set one yet."""
    if year_class not in YEAR_CLASS_NUMBER:
        return "لسه محدد"
    return f"{year_label(year_class)} / Class {YEAR_CLASS_NUMBER[year_class]}"

def year_class_keyboard(callback_prefix: str) -> InlineKeyboardMarkup:
    """The Year 1/2/3 (Class 46/45/44) picker, reused for both onboarding
    and the Settings edit flow. callback_prefix distinguishes the two so
    the button_handler branch knows whether to continue into the welcome
    menu afterwards or just confirm and return to Settings."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(year_class_label(yc), callback_data=f"{callback_prefix}:{yc}")]
        for yc in YEAR_ORDER
    ])

async def backup_settings_to_channel(context):
    global _settings_backup_msg_id, _last_settings_backup_at
    if not SETTINGS_GROUP_ID:
        return
    if not RESTORE_OK["settings"]:
        print("SETTINGS BACKUP SKIPPED — last restore failed, refusing to overwrite the channel backup.")
        return
    now = time.monotonic()
    if now - _last_settings_backup_at < SETTINGS_BACKUP_MIN_INTERVAL:
        return   # backed up recently enough — local save_settings() already has the latest data
    _last_settings_backup_at = now
    data = json.dumps(SETTINGS, indent=2).encode("utf-8")
    try:
        sent = await context.bot.send_document(
            chat_id=SETTINGS_GROUP_ID,
            document=InputFile(BytesIO(data), filename=_backup_filename("settings.json")),
            caption=SETTINGS_BACKUP_MARKER,
        )
    except Exception as e:
        print("SETTINGS BACKUP ERROR:", e)
        return
    try:
        await context.bot.pin_chat_message(
            chat_id=SETTINGS_GROUP_ID,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as e:
        print("SETTINGS PIN ERROR:", e)
    # Previous backups are no longer deleted — see the analytics backup
    # function's comment for why.
    _settings_backup_msg_id = sent.message_id

async def restore_settings_from_channel(app) -> str:
    global _settings_backup_msg_id
    if not SETTINGS_GROUP_ID:
        return "not_configured"

    async def _do():
        global _settings_backup_msg_id
        chat   = await app.bot.get_chat(SETTINGS_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document or (pinned.caption or "") != SETTINGS_BACKUP_MARKER:
            raise _RestoreNoBackup()
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        restored = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(restored, dict) or not all(isinstance(v, dict) for v in restored.values()):
            raise _RestoreInvalidArchitecture('expected a JSON object mapping user_id -> settings dict')
        SETTINGS.update(restored)
        await save_settings()
        _settings_backup_msg_id = pinned.message_id
        print(f"Restored settings: {len(SETTINGS)} user(s).")

    return await _run_restore_with_retries(app, "settings", "Settings", _do)

# ═══════════════════════════════════════════════════════════════
# LECTURE RESULTS — per-lecture leaderboard, shown before a user
# confirms they want to start a lecture.
#
# lecture_results.json schema:
# {
#   lecture_key: {
#     str(user_id): {
#       "best_correct": int, "best_total": int, "best_pct": int,
#       "attempts": int, "last_at": "YYYY-MM-DD",
#     }
#   }
# }
#
# Mirrors ANALYTICS/SETTINGS exactly: local JSON file, plus a pinned
# backup in LECTURE_RESULTS_GROUP_ID that gets replaced (upload + pin +
# delete old pin) on every change and restored from on startup. Kept in
# its own file/channel rather than folded into ANALYTICS so this
# leaderboard data (which will keep growing lecture over lecture) never
# risks the analytics backup, and vice versa.
# ═══════════════════════════════════════════════════════════════
LECTURE_RESULTS_FILE          = "lecture_results.json"
LECTURE_RESULTS_BACKUP_MARKER = "🏆 QUIZICIAN_LECTURE_RESULTS_BACKUP"

def load_lecture_results() -> dict:
    if os.path.exists(LECTURE_RESULTS_FILE):
        with open(LECTURE_RESULTS_FILE) as f:
            return json.load(f)
    return {}

async def save_lecture_results():
    # See save_analytics for why this snapshot copy is required.
    snapshot = copy.deepcopy(LECTURE_RESULTS)
    await asyncio.to_thread(_atomic_write_json, LECTURE_RESULTS_FILE, snapshot, indent=2, ensure_ascii=False)

LECTURE_RESULTS: dict = load_lecture_results()

# Message ID of the currently pinned lecture-results backup in
# LECTURE_RESULTS_GROUP_ID. Populated on startup by
# restore_lecture_results_from_channel; the pin is the source of truth.
_lecture_results_backup_msg_id: int | None = None

# Same debounce pattern as analytics/settings — local save always
# happens immediately; only the channel mirror is throttled.
_last_lecture_results_backup_at: float = 0.0
LECTURE_RESULTS_BACKUP_MIN_INTERVAL = 30  # seconds

def _lr_key(year: str, lecture_key: str) -> str:
    """LECTURE_RESULTS is one shared file across all years — prefix with
    the year so two different years never collide even if they happen to
    reuse the same module/subject/lecture-number/name text."""
    return f"{year}:{lecture_key}"

def _get_lecture_results(lecture_key: str) -> dict:
    return LECTURE_RESULTS.setdefault(lecture_key, {})

async def _record_lecture_result(user_id: int, lecture_key: str, correct: int, total: int) -> None:
    if total <= 0:
        return
    results = _get_lecture_results(lecture_key)
    key     = str(user_id)
    pct     = round(correct / total * 100)
    prev    = results.get(key)
    if prev is None or pct > prev.get("best_pct", -1) or (
        pct == prev.get("best_pct", -1) and correct > prev.get("best_correct", -1)
    ):
        best_correct, best_total, best_pct = correct, total, pct
    else:
        best_correct, best_total, best_pct = prev["best_correct"], prev["best_total"], prev["best_pct"]
    results[key] = {
        "best_correct": best_correct,
        "best_total":   best_total,
        "best_pct":     best_pct,
        "attempts":     (prev.get("attempts", 0) if prev else 0) + 1,
        "last_at":      _today(),
    }
    await save_lecture_results()

def _lecture_leaderboard(lecture_key: str, limit: int = 10) -> list[dict]:
    """Top attempts for this lecture, best % first (ties broken by more
    correct answers, then earlier last_at). Each row also carries the
    nickname (falling back to a generic label if the user never set one)."""
    results = _get_lecture_results(lecture_key)
    rows = []
    for uid_str, r in results.items():
        if not (isinstance(uid_str, str) and uid_str.lstrip("-").isdigit()):
            continue   # not a real Telegram user id — corrupted/stray key, skip rather than crash
        uid = int(uid_str)
        rows.append({
            "user_id":  uid,
            "nickname": get_nickname(uid) or f"مستخدم #{uid % 10000}",
            **r,
        })
    rows.sort(key=lambda r: (-r["best_pct"], -r["best_correct"], r["last_at"]))
    return rows[:limit]

async def backup_lecture_results_to_channel(context):
    global _lecture_results_backup_msg_id, _last_lecture_results_backup_at
    if not LECTURE_RESULTS_GROUP_ID:
        return
    if not RESTORE_OK["lecture_results"]:
        print("LECTURE RESULTS BACKUP SKIPPED — last restore failed, refusing to overwrite the channel backup.")
        return
    now = time.monotonic()
    if now - _last_lecture_results_backup_at < LECTURE_RESULTS_BACKUP_MIN_INTERVAL:
        return   # backed up recently enough — local save_lecture_results() already has the latest data
    _last_lecture_results_backup_at = now
    data = json.dumps(LECTURE_RESULTS, indent=2).encode("utf-8")
    try:
        sent = await context.bot.send_document(
            chat_id=LECTURE_RESULTS_GROUP_ID,
            document=InputFile(BytesIO(data), filename=_backup_filename("lecture_results.json")),
            caption=LECTURE_RESULTS_BACKUP_MARKER,
        )
    except Exception as e:
        print("LECTURE RESULTS BACKUP ERROR:", e)
        return
    try:
        await context.bot.pin_chat_message(
            chat_id=LECTURE_RESULTS_GROUP_ID,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as e:
        print("LECTURE RESULTS PIN ERROR:", e)
    # Previous backups are no longer deleted — see the analytics backup
    # function's comment for why.
    _lecture_results_backup_msg_id = sent.message_id

async def restore_lecture_results_from_channel(app):
    global _lecture_results_backup_msg_id
    if not LECTURE_RESULTS_GROUP_ID:
        return

    async def _do():
        global _lecture_results_backup_msg_id
        chat   = await app.bot.get_chat(LECTURE_RESULTS_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            return
        if (pinned.caption or "") != LECTURE_RESULTS_BACKUP_MARKER:
            return
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        LECTURE_RESULTS.update(json.loads(bytes(raw).decode("utf-8")))
        await save_lecture_results()
        _lecture_results_backup_msg_id = pinned.message_id
        print(f"Restored lecture results: {len(LECTURE_RESULTS)} lecture(s).")

    await _run_restore_with_retries(app, "lecture_results", "Lecture results", _do)

# ═══════════════════════════════════════════════════════════════
# MISTAKES BANK — every lecture question a user's gotten wrong, kept
# per-user, used to seed the "questions you got wrong before" slice of the
# Daily Quiz (see DAILY QUIZ section).
#
# mistakes_bank.json schema:
# [
#   {"user_id": int, "mid": int, "year": str, "module": str, "subject": str},
#   ...
# ]
#
# Entries are lightweight REFERENCES, not self-contained snapshots — just
# the question's message id (mid) plus enough scoping info to filter by
# /daily_module and to look the question back up. Full content (question/
# options/correct_option_id/explanation) is resolved on demand via
# _snapshot_from_mid, which reads QUIZ_POLL_STATUS[year] — the same data
# that's durably backed up per year in that year's quiz backup document
# (see QUIZ_BACKUP_MARKER / backup_quiz_to_channel). This is what keeps
# this file from ballooning: it used to bake in the full question text/
# options on every miss, which made mistakes_bank.json grow unbounded.
#
# Trade-off vs. the old self-contained design: a mistake now stops
# resolving if its specific question message is later deleted (not just
# edited — QUIZ_POLL_STATUS keeps content fine for edited/closed
# questions). _resolve_mistake prunes any entry that no longer resolves
# the moment it's looked up, so the bank self-cleans instead of
# accumulating dead references.
#
# Mirrors LECTURE_RESULTS exactly: local JSON file, plus a pinned backup
# in MISTAKES_BANK_GROUP_ID that gets replaced (upload + pin + delete old
# pin) on every change and restored from on startup.
# ═══════════════════════════════════════════════════════════════
MISTAKES_BANK_FILE          = "mistakes_bank.json"
MISTAKES_BANK_BACKUP_MARKER = "🗑 QUIZICIAN_MISTAKES_BANK_BACKUP"

def load_mistakes_bank() -> list:
    """Loads the local mistakes-bank file, dropping (and logging) any
    entry missing a required key — including "user_id", added when the
    bank became per-user; older entries recorded before that change don't
    have it and are intentionally discarded here rather than migrated, per
    an explicit decision to start every user's bank fresh instead of
    guessing at ownership. Every entry this system writes itself (see
    record_mistake) always has all five keys, so anything missing one
    didn't come from normal operation and isn't safe to trust downstream.
    Filtering here means every reader (record_mistake's dedup check,
    _scoped_mistakes_bank, _resolve_mistake) can keep assuming a
    well-formed entry without each needing its own defensive check."""
    if not os.path.exists(MISTAKES_BANK_FILE):
        return []
    with open(MISTAKES_BANK_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    required = ("user_id", "mid", "year", "module", "subject")
    clean  = [m for m in raw if isinstance(m, dict) and all(k in m for k in required)]
    if len(clean) != len(raw):
        print(f"MISTAKES BANK: dropped {len(raw) - len(clean)} malformed/legacy entr(y/ies) missing a required key on load.")
    return clean

def _is_valid_mistake_entry(m) -> bool:
    return isinstance(m, dict) and all(k in m for k in ("user_id", "mid", "year", "module", "subject"))

async def save_mistakes_bank():
    # See save_analytics for why this snapshot copy is required — this is
    # the exact function whose race the load test's "dictionary changed
    # size during iteration" errors most likely came from: record_mistake
    # appends to MISTAKES_BANK from many concurrent _advance_lecture_session
    # calls while a background thread could simultaneously be mid-iteration
    # serializing the same live list to JSON.
    snapshot = copy.deepcopy(MISTAKES_BANK)
    await asyncio.to_thread(_atomic_write_json, MISTAKES_BANK_FILE, snapshot, indent=2, ensure_ascii=False)

MISTAKES_BANK: list = load_mistakes_bank()

# ── Per-user index over MISTAKES_BANK ───────────────────────────
# MISTAKES_BANK is one flat list holding every user's entries (see
# schema note above), which made every read of "this user's mistakes"
# a full scan of the whole bank — fine at hundreds of entries, but a
# scan that grows with EVERY user's activity just to answer a question
# about ONE user doesn't scale as the bank grows. _MISTAKES_BY_USER
# keeps the same entry dicts (not copies) grouped by user_id, so a
# lookup is O(this user's mistakes) instead of O(everyone's). It's a
# derived structure, not a second source of truth — MISTAKES_BANK stays
# the one thing that gets saved/backed up; this index is rebuilt or
# patched to match it at every mutation site (grep _reindex_mistakes,
# _mistakes_index_add, _mistakes_index_remove to find all of them).
_MISTAKES_BY_USER: dict[int, list] = {}

def _reindex_mistakes_bank() -> None:
    """Rebuilds _MISTAKES_BY_USER from scratch against the current
    MISTAKES_BANK. Call this after any bulk replacement of the bank's
    contents (initial load, a channel restore) — for the routine
    single-entry cases (one mistake recorded, one stale entry pruned)
    use _mistakes_index_add / _mistakes_index_remove instead, which
    update the index in O(1) rather than rescanning everything."""
    _MISTAKES_BY_USER.clear()
    for m in MISTAKES_BANK:
        if _is_valid_mistake_entry(m):
            _MISTAKES_BY_USER.setdefault(m["user_id"], []).append(m)

def _mistakes_index_add(entry: dict) -> None:
    _MISTAKES_BY_USER.setdefault(entry["user_id"], []).append(entry)

def _mistakes_index_remove(entry: dict) -> None:
    bucket = _MISTAKES_BY_USER.get(entry.get("user_id"))
    if bucket and entry in bucket:
        bucket.remove(entry)

_reindex_mistakes_bank()

# Message ID of the currently pinned mistakes-bank backup in
# MISTAKES_BANK_GROUP_ID. Populated on startup by
# restore_mistakes_bank_from_channel; the pin is the source of truth.
_mistakes_bank_backup_msg_id: int | None = None

# Channel mirror debounce — separate from the local-disk debounce below.
_last_mistakes_bank_backup_at: float = 0.0
MISTAKES_BANK_BACKUP_MIN_INTERVAL = 30  # seconds

# ── Local-disk debounce for the hot mistakes-bank path ─────────────
# save_mistakes_bank() (deepcopy + atomic write of EVERY user's mistakes,
# not just the one entry that just got added) is still called directly —
# and immediately — from low-frequency paths: a channel restore, an admin
# clearing a user's bank, or pruning a stale/malformed entry (see
# _resolve_mistake) — those are rare enough, or want disk correct right
# away, that the cost doesn't matter.
#
# record_mistake is different: it fires on every wrong answer, from every
# active user, all day — the single hottest write path into this file.
# Same fix as analytics/settings: mark dirty (O(1), no I/O) and let a
# periodic job (_flush_mistakes_bank_job, registered in _post_init) do the
# real save every MISTAKES_BANK_FLUSH_INTERVAL seconds if anything
# changed. Worst case on a hard crash: up to one interval's worth of
# recently-missed questions aren't in the bank yet — they just don't seed
# that user's Mistakes Bank slice until missed again, not a correctness
# problem worth an fsync per wrong answer.
_mistakes_bank_dirty: bool = False
MISTAKES_BANK_FLUSH_INTERVAL = 60  # seconds

def _mark_mistakes_bank_dirty() -> None:
    global _mistakes_bank_dirty
    _mistakes_bank_dirty = True

async def _flush_mistakes_bank_if_dirty() -> None:
    """Writes MISTAKES_BANK to disk only if something changed since the
    last flush. Called by the periodic job and by _post_shutdown for a
    final flush on clean exit."""
    global _mistakes_bank_dirty
    if not _mistakes_bank_dirty:
        return
    _mistakes_bank_dirty = False
    await save_mistakes_bank()

async def record_mistake(user_id: int, mid: int, year: str, module: str, subject: str) -> bool:
    """Adds a wrong-answer REFERENCE to the bank — just the question id
    (mid) + scoping info, not the full question text (see schema note
    above). Deduped by (user_id, year, mid), so the same question missed
    twice by the same person only ever occupies one slot — but different
    people missing the same question each get their own entry, since the
    bank is per-user. Returns whether a new entry was added (False if it
    was already there — nothing to save/back up in that case)."""
    for m in _MISTAKES_BY_USER.get(user_id, []):
        if m["mid"] == mid and m["year"] == year:
            return False
    entry = {"user_id": user_id, "mid": mid, "year": year, "module": module, "subject": subject}
    MISTAKES_BANK.append(entry)
    _mistakes_index_add(entry)
    _mark_mistakes_bank_dirty()
    return True

async def backup_mistakes_bank_to_channel(context):
    global _mistakes_bank_backup_msg_id, _last_mistakes_bank_backup_at
    if not MISTAKES_BANK_GROUP_ID:
        return
    if not RESTORE_OK["mistakes_bank"]:
        print("MISTAKES BANK BACKUP SKIPPED — last restore failed, refusing to overwrite the channel backup.")
        return
    now = time.monotonic()
    if now - _last_mistakes_bank_backup_at < MISTAKES_BANK_BACKUP_MIN_INTERVAL:
        return   # backed up recently enough — local save_mistakes_bank() already has the latest data
    _last_mistakes_bank_backup_at = now
    data = json.dumps(MISTAKES_BANK, indent=2).encode("utf-8")
    try:
        sent = await context.bot.send_document(
            chat_id=MISTAKES_BANK_GROUP_ID,
            document=InputFile(BytesIO(data), filename=_backup_filename("mistakes_bank.json")),
            caption=MISTAKES_BANK_BACKUP_MARKER,
        )
    except Exception as e:
        print("MISTAKES BANK BACKUP ERROR:", e)
        return
    try:
        await context.bot.pin_chat_message(
            chat_id=MISTAKES_BANK_GROUP_ID,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as e:
        print("MISTAKES BANK PIN ERROR:", e)
    # Previous backups are no longer deleted — see the analytics backup
    # function's comment for why.
    _mistakes_bank_backup_msg_id = sent.message_id

async def restore_mistakes_bank_from_channel(app) -> str:
    global _mistakes_bank_backup_msg_id
    if not MISTAKES_BANK_GROUP_ID:
        return "not_configured"

    async def _do():
        global _mistakes_bank_backup_msg_id
        chat   = await app.bot.get_chat(MISTAKES_BANK_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document or (pinned.caption or "") != MISTAKES_BANK_BACKUP_MARKER:
            raise _RestoreNoBackup()
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        restored = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(restored, list):
            raise _RestoreInvalidArchitecture('expected a JSON array of mistake entries')
        clean = [m for m in restored if _is_valid_mistake_entry(m)]
        if restored and not clean:
            raise _RestoreInvalidArchitecture('none of the entries matched the expected mistake-entry shape')
        if len(clean) != len(restored):
            print(f"MISTAKES BANK: dropped {len(restored) - len(clean)} malformed entr(y/ies) from the channel backup on restore.")
        MISTAKES_BANK[:] = clean
        _reindex_mistakes_bank()
        await save_mistakes_bank()
        _mistakes_bank_backup_msg_id = pinned.message_id
        print(f"Restored mistakes bank: {len(MISTAKES_BANK)} question(s).")

    return await _run_restore_with_retries(app, "mistakes_bank", "Mistakes bank", _do)

# ═══════════════════════════════════════════════════════════════
# DAILY QUIZ — 💥Daily Quiz💥: each day, every configured year gets ONE
# shared run of DAILY_QUIZ_TOTAL_COUNT (10) random
# questions — built once per (year, day) and then IDENTICAL for every
# user in that year, so everyone's run (and the leaderboard ranking it
# feeds) is a level playing field. No mistakes-bank content is used here
# at all (that's exclusively the 🧠 Mistakes Bank menu button's own
# retake flow). Restricted to that year's DAILY_QUIZ_ACTIVE_MODULE by
# default, or to the admin-set /daily_module scope when one is set for
# that year as a temporary override (see get_daily_quiz_scope).
#
# Tapping the 💥Daily Quiz💥 main-menu button opens a small hub
# (show_daily_quiz_menu) that always shows the day's leaderboard for the
# user's own Year/Class (get_year_class — prompting them to set it first
# if they haven't), plus a Start button if they haven't run today's quiz
# yet. Gated to once per person per day via each user's settings
# "daily_quiz_last_date". Pushed to everyone at 2pm Cairo time once a day
# (see the job_queue.run_daily call in MAIN) as just a button into that
# same hub.
#
# Deliberately its own session type (DAILY_QUIZ_SESSIONS), separate from
# LECTURE_SESSIONS, rather than shoehorned into the lecture-session shape:
# a lecture session's dead-poll pruning, legacy-content recovery, and
# result-recording are all keyed to one specific year+lecture_key, which
# doesn't make sense for a session mixing many lectures/subjects at once.
# A Daily Quiz question is fully self-contained (question/options/
# correct_id baked in directly, same shape as a MISTAKES_BANK entry) so
# delivery never needs to touch any year's live channel/state at all.
# ═══════════════════════════════════════════════════════════════
DAILY_QUIZ_SESSIONS = {}   # user_id -> {"queue": [question dict, ...], "current_poll_id",
                           #             "current_correct_id", "current_message_id",
                           #             "year",
                           #             "total", "answered", "correct", "xp_earned"}

DAILY_QUIZ_TOTAL_COUNT = 10   # random questions per year, per day

# ═══════════════════════════════════════════════════════════════
# DAILY QUIZ LEADERBOARD — deliberately RAM-only, unlike everything else
# in this file. It resets every day anyway (today's ranking is
# meaningless once a new day's questions exist), so there's no reason to
# spend backup/restore machinery keeping it alive across a bot restart —
# worst case, a restart mid-day just clears today's board a little
# early, which is harmless. Contrast with daily_medals on the ANALYTICS
# entry (_blank_entry), which IS persistent — that's the lifetime medal
# count this board hands out before resetting, and that has to survive.
#
# One board PER YEAR — since every user in a year plays the exact same
# shared run (see DAILY QUIZ above), ranking is only meaningful within a
# year, not across years.
# ═══════════════════════════════════════════════════════════════
DAILY_QUIZ_LEADERBOARD_DATE: str | None = None   # which day's date the boards below are for
DAILY_QUIZ_LEADERBOARD: dict[str, dict[int, dict]] = {}   # year -> user_id -> {"name", "correct", "total", "duration"}

def _finalize_daily_leaderboard() -> None:
    """Awards lifetime medals (ANALYTICS[uid]['daily_medals']) to the top 3
    of each year's board, then clears every board. Called explicitly at
    the start of _daily_quiz_push_job (the 2pm-Cairo push) — so medals for
    a day's Daily Quiz are handed out right as the NEXT one goes out,
    not lazily whenever someone happens to open the leaderboard."""
    global DAILY_QUIZ_LEADERBOARD
    any_ranked = False
    for year in list(DAILY_QUIZ_LEADERBOARD):
        ranked = _rank_daily_leaderboard(year)
        for i, medal_key in enumerate(("gold", "silver", "bronze")):
            if i >= len(ranked):
                break
            entry = _get_entry(ranked[i]["user_id"])
            entry["daily_medals"][medal_key] += 1
            any_ranked = True
    if any_ranked:
        _mark_analytics_dirty()
    DAILY_QUIZ_LEADERBOARD = {}

def _ensure_daily_leaderboard_fresh() -> None:
    """Call before any read or write of DAILY_QUIZ_LEADERBOARD. Just rolls
    the tracked date forward and clears stale boards on a day change —
    medal-awarding itself does NOT happen here (see _finalize_daily_leaderboard,
    called explicitly by _daily_quiz_push_job right as the next Daily Quiz
    goes out), so this is safe to call at any time of day without handing
    out medals early."""
    global DAILY_QUIZ_LEADERBOARD_DATE, DAILY_QUIZ_LEADERBOARD
    today = _today()
    if DAILY_QUIZ_LEADERBOARD_DATE == today:
        return
    DAILY_QUIZ_LEADERBOARD_DATE = today
    DAILY_QUIZ_LEADERBOARD = {}

def _rank_daily_leaderboard(year: str) -> list[dict]:
    """Today's Daily Quiz finishers for one year, ranked the same way as
    the year leaderboard: correct count highest first, total duration
    lowest first as the tiebreaker."""
    rows = list(DAILY_QUIZ_LEADERBOARD.get(year, {}).values())
    rows.sort(key=lambda r: (-r["correct"], r["duration"]))
    return rows

def _record_daily_leaderboard_finish(user_id: int, year: str, correct: int, total: int, duration: float) -> None:
    """Records this user's (one-per-day) finish on today's board for
    `year`."""
    _ensure_daily_leaderboard_fresh()
    board = DAILY_QUIZ_LEADERBOARD.setdefault(year, {})
    board[user_id] = {
        "user_id":  user_id,
        "name":     get_nickname(user_id) or f"مستخدم #{user_id % 10000}",
        "correct":  correct,
        "total":    total,
        "duration": duration,
    }

# Push time for the daily 💥Daily Quiz💥 button (see job_queue.run_daily in
# MAIN, and next_daily_quiz_time() / /time below — all three read from
# these two so the schedule only ever needs to change in one place).
DAILY_QUIZ_TZ   = ZoneInfo("Africa/Cairo")
DAILY_QUIZ_HOUR = 14
DAILY_QUIZ_MIN  = 0

# Push time for the daily zipped-backup export (see _daily_backup_export_job
# and job_queue.run_daily in MAIN). Off-peak hour, well clear of the Daily
# Quiz push and the hourly Zikr, in the same DAILY_QUIZ_TZ.
DAILY_BACKUP_EXPORT_HOUR = 3
DAILY_BACKUP_EXPORT_MIN  = 0

def next_daily_quiz_time() -> datetime:
    """The next upcoming 2pm-Cairo push moment — today's if it hasn't
    happened yet, otherwise tomorrow's."""
    now = datetime.now(DAILY_QUIZ_TZ)
    today_push = now.replace(hour=DAILY_QUIZ_HOUR, minute=DAILY_QUIZ_MIN, second=0, microsecond=0)
    return today_push if now < today_push else today_push + timedelta(days=1)

_DAILY_QUIZ_POOL_CACHE: dict[str, dict] = {}   # year -> {"pool", "built_at", "scope_key"}
_DAILY_QUIZ_POOL_CACHE_TTL_SECONDS = 120

# Each year's Daily Quiz is restricted to ONE currently-active module by
# default — whatever's currently being taught — rather than that year's
# whole curriculum. Update this whenever the active module changes; an
# admin can also temporarily override a given year via /daily_module
# (see get_daily_quiz_scope), which takes priority over this default.
DAILY_QUIZ_ACTIVE_MODULE = {
    "y1": "Foundation (1)",
    "y2": "Respiratory",
    "y3": "Endocrine",
}

# Rebuilding this pool means: for every (module, subject) pair, scanning
# the ENTIRE year's QUIZ_INDEX to find lectures matching that pair (see
# ready_lecture_keys), on top of a QUIZ_POLL_STATUS scan. That's fine once
# — it's expensive when 700 students all tap "Daily Quiz" inside the same
# push window and each one triggers a fresh rebuild. The pool doesn't
# depend on which student is asking, so it's cached (per year) for a
# couple of minutes; a lecture that gets closed mid-window just joins the
# pool the next time the cache refreshes rather than instantly, which is
# fine for a once-a-day quiz. Invalidated early if the admin changes
# /daily_module scope, so a scope change is never stuck behind a stale
# cache.
def _daily_quiz_subject_pool(year: str) -> dict:
    """Every ready (closed-poll) question mid for ONE year, grouped by
    (module, subject) — the pool _build_daily_quiz_questions draws its random
    questions from (any subject can contribute more than one; this is
    just how the mids are organized so a scope filter can narrow it
    before picking). Restricted to that year's DAILY_QUIZ_ACTIVE_MODULE by
    default; if the admin has set a /daily_module scope for this same
    year, that overrides the default instead. Cached briefly per year —
    see _DAILY_QUIZ_POOL_CACHE_TTL_SECONDS."""
    scope = get_daily_quiz_scope()
    if scope and scope["year"] == year:
        scoped_module = scope["module"]
    else:
        scoped_module = DAILY_QUIZ_ACTIVE_MODULE.get(year)

    now = time.monotonic()
    cache = _DAILY_QUIZ_POOL_CACHE.setdefault(year, {"pool": None, "built_at": 0.0, "scope_key": None})
    if (cache["pool"] is not None
            and cache["scope_key"] == scoped_module
            and now - cache["built_at"] < _DAILY_QUIZ_POOL_CACHE_TTL_SECONDS):
        return cache["pool"]

    pool = {}   # (module, subject) -> [mid, ...]
    if year in configured_years():
        closed_message_ids = {v["message_id"] for v in QUIZ_POLL_STATUS[year].values() if v["closed"]}
        modules = [scoped_module] if scoped_module else ready_modules(year)
        for module in modules:
            for subject in ready_subjects(year, module):
                mids = []
                for lecture_key in ready_lecture_keys(year, module, subject):
                    ids = QUIZ_INDEX[year][lecture_key]["ids"]
                    mids.extend(mid for mid in ids if mid in closed_message_ids)
                if mids:
                    pool[(module, subject)] = mids

    cache["pool"] = pool
    cache["built_at"] = now
    cache["scope_key"] = scoped_module
    return pool

async def _snapshot_from_mid(context: ContextTypes.DEFAULT_TYPE, year: str, mid: int, module: str, subject: str,
                              status_by_mid: dict | None = None) -> dict | None:
    """Builds a self-contained question dict (same shape as a
    MISTAKES_BANK entry) from a channel poll's captured content. Returns
    None if the content was never captured and couldn't be recovered
    (very old lecture, or the message is gone) — callers skip it.

    status_by_mid, if given, is a {message_id: status} map for this year
    (built once by the caller) used instead of scanning
    QUIZ_POLL_STATUS[year] here — callers that resolve many mids in a row
    (e.g. build_daily_quiz_questions) should pass one in so N lookups cost
    O(N) total instead of O(N * len(QUIZ_POLL_STATUS[year]))."""
    if status_by_mid is not None:
        status = status_by_mid.get(mid)
    else:
        status = next((v for v in QUIZ_POLL_STATUS[year].values() if v["message_id"] == mid), None)
    question    = status.get("question")           if status else None
    options     = status.get("options")             if status else None
    correct_id  = status.get("correct_option_id")   if status else None
    explanation = status.get("explanation")         if status else None
    if not (question and options and correct_id is not None):
        return None   # legacy/uncaptured content — skip rather than spend a forward+delete recovering it here
    return {
        "question": question, "options": options, "correct_option_id": correct_id,
        "explanation": explanation, "year": year, "module": module, "subject": subject,
    }

def _scoped_mistakes_bank(user_id: int) -> list:
    """This user's slice of MISTAKES_BANK (via _MISTAKES_BY_USER — see
    its comment for why), further filtered to the admin-set
    /daily_module scope, if any. Returns lightweight {user_id, mid,
    year, module, subject} references — see _resolve_mistake(s) to turn
    these into full question dicts."""
    scope = get_daily_quiz_scope()
    user_entries = _MISTAKES_BY_USER.get(user_id, [])
    if not scope:
        return list(user_entries)
    return [m for m in user_entries if m["year"] == scope["year"] and m["module"] == scope["module"]]

def _user_mistake_count(user_id: int) -> int:
    """This user's total mistake-bank entries, ignoring any admin-set
    /daily_module scope — for personal displays like /mystats, where the
    admin's narrowing of the Daily Quiz shouldn't make the user's own
    bank look smaller than it really is."""
    return len(_MISTAKES_BY_USER.get(user_id, []))

def _poll_status_index(year: str) -> dict:
    """{message_id: status} for every poll tracked in QUIZ_POLL_STATUS[year].
    Built fresh each call (QUIZ_POLL_STATUS is mutated in many places, so
    this isn't cached) — the point is letting a caller that's about to
    resolve several mids in the same year pay this scan once instead of
    once per mid, not eliminating the scan altogether."""
    return {v["message_id"]: v for v in QUIZ_POLL_STATUS[year].values()}

# ── 🔎 Search Content ──────────────────────────────────────────────
# Matches are resent as fresh live quiz polls straight into the user's
# DM (via deliver_quiz — the same single delivery path every other quiz
# feature uses), NOT as links into the quiz channel — that channel is
# private, so a t.me/c/ link wouldn't reliably open for everyone, and
# resending keeps the whole search self-contained in the chat with
# Quizzy. Kept small since each match is a handful of messages, not one
# line — see _send_search_results.
SEARCH_RESULTS_LIMIT = 8   # max questions resent per search — see _search_quiz_questions

def _search_quiz_questions(year: str, module: str | None, query_text: str,
                            limit: int = SEARCH_RESULTS_LIMIT) -> tuple[list, int]:
    """Searches every *ready* (closed, non-empty — same bar as
    ready_lecture_keys) lecture in `year`, optionally narrowed to one
    `module`, for quiz-channel poll questions containing `query_text`.

    Matching is substring-based on the same Arabic-aware normalized form
    the nickname filter uses (_normalize_for_filter) — tashkeel, alef/yaa/
    taa-marbuta letter variants, case, and spacing differences between
    the query and the stored question text don't cause a false miss.

    Returns (matches, total_count): `matches` is capped at `limit` (in
    posting order — insertion order of QUIZ_INDEX[year], then ids[] order
    within each lecture), `total_count` is how many actually matched, so
    the caller can tell the user "showing 8 of 23" instead of silently
    truncating. Each match dict has everything deliver_quiz needs to
    resend it as a poll — "question", "options", "correct_option_id",
    "explanation" — plus "lecture_name"/"module"/"subject" for the
    caption shown above each resent poll. A poll whose content was never
    captured (very old, pre-tracking content) can't be resent and is
    silently skipped, same bar _snapshot_from_mid uses."""
    needle = _normalize_for_filter(query_text)
    if not needle:
        return [], 0
    status_by_mid = _poll_status_index(year)
    matches, total = [], 0
    for entry in QUIZ_INDEX.get(year, {}).values():
        if not entry.get("closed") or not entry.get("ids"):
            continue
        if module and entry.get("module") != module:
            continue
        for mid in entry["ids"]:
            status = status_by_mid.get(mid)
            question    = status.get("question")           if status else None
            options     = status.get("options")             if status else None
            correct_id  = status.get("correct_option_id")   if status else None
            if not question or not options or correct_id is None:
                continue   # uncaptured/legacy content — can't be resent as a poll
            if needle not in _normalize_for_filter(question):
                continue
            total += 1
            if len(matches) < limit:
                matches.append({
                    "question":          question,
                    "options":           options,
                    "correct_option_id": correct_id,
                    "explanation":       status.get("explanation"),
                    "lecture_name":      entry.get("name", "؟"),
                    "module":            entry.get("module", ""),
                    "subject":           entry.get("subject", ""),
                })
    return matches, total

SEARCH_MOD_ALL = "__ALL__"   # sentinel used in search_mod: callback_data for "search every module"

def _search_scope_label(year: str, module: str | None) -> str:
    scope = module_label(module) if module else "All Modules"
    return f"{year_label(year)} — {scope}"

def _search_again_markup(year: str, module: str | None) -> InlineKeyboardMarkup:
    mod_token = module if module else SEARCH_MOD_ALL
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Search Again", callback_data=f"search_mod:{year}:{mod_token}")],
        [InlineKeyboardButton("🏠 Back to Home", callback_data="back_home")],
    ])

async def _send_search_results(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                 year: str, module: str | None, query_text: str):
    """Runs the search, then resends each match as its own fresh live
    quiz poll (deliver_quiz) directly into chat_id, each preceded by a
    one-line caption naming its subject/lecture. Ends with a "🔎 Search
    Again" / "🏠 Back to Home" prompt either way — see _search_again_markup.
    """
    matches, total = _search_quiz_questions(year, module, query_text)
    scope_line = f"📚 {_search_scope_label(year, module)}\n🔎 \"{html.escape(query_text[:80])}\""
    again_markup = _search_again_markup(year, module)

    if not matches:
        await context.bot.send_message(
            chat_id, scope_line + "\n\n📭 مفيش أسئلة اتطابقت مع البحث ده.",
            parse_mode=ParseMode.HTML, reply_markup=again_markup,
        )
        return

    note = "" if total <= len(matches) else f" (بتعرض {len(matches)} من {total} — دقق البحث أكتر لو مش لاقي اللي عايزه)"
    await context.bot.send_message(
        chat_id, scope_line + f"\n\n✅ لاقيت {len(matches)} سؤال{note}:",
        parse_mode=ParseMode.HTML,
    )
    for i, m in enumerate(matches, 1):
        where = f"{subject_label(m['subject'])} — {html.escape(m['lecture_name'])}"
        await context.bot.send_message(chat_id, f"{i}. {where}", parse_mode=ParseMode.HTML)
        await deliver_quiz(
            context, chat_id, m["question"], m["options"], m["correct_option_id"],
            explanation=m.get("explanation"),
        )
    await context.bot.send_message(chat_id, "🔎 عايز تبحث تاني؟", reply_markup=again_markup)

async def _resolve_mistake(context: ContextTypes.DEFAULT_TYPE, entry: dict, status_by_mid: dict | None = None) -> dict | None:
    """Turns one lightweight MISTAKES_BANK entry ({mid, year, module,
    subject}) into a full self-contained question dict via
    _snapshot_from_mid. If the question no longer resolves (its message
    was deleted since the mistake was recorded), the entry is pruned from
    MISTAKES_BANK right here — a failed lookup means it'll never resolve
    again, so there's no point keeping it around. Returns None in that
    case; callers just skip it.

    status_by_mid, if given, is passed straight through to
    _snapshot_from_mid (see there) — pass one in when resolving several
    entries from the same year in a row, e.g. via _resolve_mistakes."""
    if not _is_valid_mistake_entry(entry):
        # Malformed entry (missing a required key) — shouldn't happen
        # given the load/restore-time filtering (see load_mistakes_bank /
        # restore_mistakes_bank_from_channel), but this crashed the whole
        # Daily Quiz build once already, so degrade to "prune and skip"
        # rather than trust that filtering is airtight everywhere.
        print(f"MISTAKES BANK: skipping and removing malformed entry: {entry!r}")
        try:
            MISTAKES_BANK.remove(entry)
            _mistakes_index_remove(entry)
            await save_mistakes_bank()
        except ValueError:
            pass
        return None
    snap = await _snapshot_from_mid(context, entry["year"], entry["mid"], entry["module"], entry["subject"], status_by_mid)
    if snap is None:
        try:
            MISTAKES_BANK.remove(entry)
            _mistakes_index_remove(entry)
            await save_mistakes_bank()
        except ValueError:
            pass   # already removed by a concurrent lookup — harmless
    return snap

async def _resolve_mistakes(context: ContextTypes.DEFAULT_TYPE, entries: list) -> list:
    """Resolves a list of MISTAKES_BANK entries to full question dicts,
    silently dropping (and pruning) any that no longer resolve. Builds one
    poll-status index per distinct year among the entries, rather than
    scanning QUIZ_POLL_STATUS[year] again for every single entry."""
    status_by_mid_by_year: dict = {}
    resolved = []
    for entry in entries:
        if not _is_valid_mistake_entry(entry):
            await _resolve_mistake(context, entry)   # logs + prunes it, returns None
            continue
        year = entry["year"]
        if year not in status_by_mid_by_year:
            status_by_mid_by_year[year] = _poll_status_index(year)
        snap = await _resolve_mistake(context, entry, status_by_mid_by_year[year])
        if snap:
            resolved.append(snap)
    return resolved

# ── Per-day, per-year shared Daily Quiz questions ────────────────────
# Built once per (year, day) — the first user of that year to tap Daily
# Quiz that day pays the build cost, everyone else in that year that day
# just reads the cached result. Persisted through the same session-
# persistence channel as LECTURE_SESSIONS/DAILY_QUIZ_SESSIONS/
# MISTAKES_RETAKE_SESSIONS (see _sessions_snapshot / _restore_sessions_dict
# / SESSION PERSISTENCE below) so a mid-day restart or redeploy doesn't
# hand out a different question set than whatever's already been played
# that day — restore just loads the raw values back in and lets
# _ensure_daily_quiz_questions_fresh's date check below discard them
# normally once the day actually rolls over.
_DAILY_QUIZ_QUESTIONS_DATE: str | None = None
_DAILY_QUIZ_QUESTIONS: dict[str, list] = {}   # year -> [question dict, ...] (up to DAILY_QUIZ_TOTAL_COUNT)

# One lock per year, created lazily via setdefault (sync, no await in between
# check and creation, so this is safe despite concurrent_updates()). Without
# it, every one of the day's first N concurrent Daily Quiz requests for a
# year sees `year not in _DAILY_QUIZ_QUESTIONS` before any of them finishes
# building, so all N pay the full build cost instead of 1 build + (N-1)
# cheap cache reads.
_DAILY_QUIZ_BUILD_LOCKS: dict[str, asyncio.Lock] = {}

def _ensure_daily_quiz_questions_fresh() -> None:
    """Call before any read of _DAILY_QUIZ_QUESTIONS. Clears every year's
    cached questions the first time it's called on a new day, so the
    day's first request per year rebuilds fresh content."""
    global _DAILY_QUIZ_QUESTIONS_DATE, _DAILY_QUIZ_QUESTIONS
    today = _today()
    if _DAILY_QUIZ_QUESTIONS_DATE != today:
        _DAILY_QUIZ_QUESTIONS = {}
        _DAILY_QUIZ_QUESTIONS_DATE = today

async def _build_daily_quiz_questions(context: ContextTypes.DEFAULT_TYPE, year: str) -> list:
    """Up to DAILY_QUIZ_TOTAL_COUNT (10) questions, drawn
    at random from `year`'s ready pool — no mistakes-bank content.
    Respects the admin-set /daily_module scope if it's set for this year.
    Falls short gracefully (a shorter, or empty, run) if there isn't
    enough ready content yet."""
    subject_pool = _daily_quiz_subject_pool(year)
    # Flatten to one (module, subject, mid) tuple per ready question, so
    # picking is a plain random sample over individual questions — not a
    # pick-a-subject-then-one-question-from-it scheme, which would cap
    # this at one question per subject.
    all_mids = [
        (module, subject, mid)
        for (module, subject), mids in subject_pool.items()
        for mid in mids
    ]
    random.shuffle(all_mids)

    status_by_mid = _poll_status_index(year)   # one scan, reused for every pick below

    questions = []
    for module, subject, mid in all_mids:
        if len(questions) >= DAILY_QUIZ_TOTAL_COUNT:
            break
        snap = await _snapshot_from_mid(context, year, mid, module, subject, status_by_mid)
        if snap:
            questions.append(snap)

    return questions

async def get_daily_quiz_questions(context: ContextTypes.DEFAULT_TYPE, year: str) -> list:
    """Today's shared Daily Quiz questions for `year`, building and
    caching them on first request of the day."""
    _ensure_daily_quiz_questions_fresh()
    if year not in _DAILY_QUIZ_QUESTIONS:
        # Double-checked locking: the cheap path above (699 reads) never
        # touches the lock at all. Only a genuine cache miss pays for the
        # lock acquire, and only the first miss for this year pays for the
        # build — everyone else queued behind the lock re-checks the cache
        # (now warm) and returns immediately instead of building again.
        lock = _DAILY_QUIZ_BUILD_LOCKS.setdefault(year, asyncio.Lock())
        async with lock:
            _ensure_daily_quiz_questions_fresh()  # guards against the day rolling over mid-wait
            if year not in _DAILY_QUIZ_QUESTIONS:
                _DAILY_QUIZ_QUESTIONS[year] = await _build_daily_quiz_questions(context, year)
                # Persist right away rather than waiting for the next periodic
                # sessions tick — this is the one moment (first build of the day
                # for this year) a redeploy landing seconds later would otherwise
                # regenerate a different set. See SESSION PERSISTENCE below.
                await _flush_sessions_if_changed()
                await backup_sessions_to_channel(context)
    return _DAILY_QUIZ_QUESTIONS[year]

async def _deliver_next_daily_question(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict) -> bool:
    """Same idea as _deliver_next_lecture_question, but for a self-
    contained Daily Quiz question dict — no mid/channel lookups needed,
    everything required is already sitting in the queue entry. Sets
    session['current_*']. Returns whether a question went out."""
    if not session["queue"]:
        session["current_poll_id"] = None
        session["current_correct_id"] = None
        session["current_message_id"] = None
        session["current_delivered_at"] = None
        return False
    q = session["queue"].pop(0)
    session["delivered_count"] = session.get("delivered_count", 0) + 1
    timer_seconds = get_question_timer_seconds(user_id)
    try:
        msg = await context.bot.send_poll(
            chat_id=user_id, question=_numbered_question(q["question"], session["delivered_count"]), options=q["options"],
            type="quiz", correct_option_id=q["correct_option_id"], is_anonymous=False,
            explanation=(q.get("explanation") or None),
            open_period=(timer_seconds or None),
        )
    except Exception as e:
        print(f"Couldn't send daily quiz question: {e}")
        session["delivered_count"] -= 1   # this send never went out — don't burn a number on it
        return await _deliver_next_daily_question(context, user_id, session)   # try the next one
    session["current_poll_id"]    = msg.poll.id
    session["current_correct_id"] = q["correct_option_id"]
    session["current_message_id"] = msg.message_id
    session["current_delivered_at"] = time.time()  # see _record_time_spent / year leaderboard
    # q was popped off the queue above, so stash its module/subject on the
    # session — _advance_daily_quiz_session / _advance_mistakes_retake_session
    # read these back to credit the right subject in /mystats.
    session["current_module"]  = q.get("module")
    session["current_subject"] = q.get("subject")
    _schedule_question_timeout(context, session.get("kind", "daily"), user_id, msg.poll.id, timer_seconds)
    return True

async def _advance_daily_quiz_session(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict, is_correct: bool, message_id: int | None, delivered_at: float | None = None):
    """Daily Quiz's counterpart to _advance_lecture_session: same XP
    (15/5/+25 completion) and same lecture_questions/lecture_streak
    achievement tracking (a Daily Quiz question is still practice, so it
    counts toward those same stats — and therefore toward the year
    leaderboard's correct-count/duration ranking too) — but no
    lecture_key, so no dead-poll pruning, no legacy-content recovery, and
    no _record_lecture_result/PER-LECTURE leaderboard involvement; there's
    no single lecture for this to be an "attempt" of."""
    session["answered"] += 1
    session["correct"] = session.get("correct", 0) + (1 if is_correct else 0)
    if delivered_at is not None:
        elapsed = time.time() - delivered_at
        _record_time_spent(user_id, elapsed)
        session["duration_seconds"] = session.get("duration_seconds", 0.0) + elapsed

    sent_next = await _deliver_next_daily_question(context, user_id, session)
    is_last   = not sent_next

    per_question_xp = XP_LECTURE_CORRECT if is_correct else XP_LECTURE_INCORRECT
    xp_delta = per_question_xp + (XP_LECTURE_COMPLETE_BONUS if is_last else 0)
    session["xp_earned"] = session.get("xp_earned", 0) + xp_delta

    events     = await _record_activity(user_id, persist=False)
    user_entry = _get_entry(user_id)
    prev_streak = user_entry.get("lecture_correct_streak_current", 0)
    user_entry["lecture_questions_answered"]  += 1
    user_entry["lecture_questions_correct"]   += 1 if is_correct else 0
    user_entry["lecture_questions_incorrect"] += 0 if is_correct else 1
    _record_subject_answer(user_entry, session.get("current_module"), session.get("current_subject"), is_correct)
    if is_correct:
        user_entry["lecture_correct_streak_current"] += 1
        if user_entry["lecture_correct_streak_current"] > user_entry["lecture_correct_streak_best"]:
            user_entry["lecture_correct_streak_best"] = user_entry["lecture_correct_streak_current"]
    else:
        user_entry["lecture_correct_streak_current"] = 0

    await _react_to_lecture_answer(
        context, user_id, message_id,
        is_correct=is_correct,
        new_streak=user_entry["lecture_correct_streak_current"],
        streak_broken=(not is_correct and prev_streak > 0),
    )

    events["achievements"] += _check_achievements(user_entry, "questions_answered")
    events["achievements"] += _check_achievements(user_entry, "correct_streak")
    events["achievements"] += _check_achievements(user_entry, "achievement_collector")

    _award_xp(user_entry, xp_delta)
    final_level = _xp_to_level(user_entry["xp"])
    if final_level > user_entry["level"]:
        user_entry["level"] = final_level
        events["level_up"] = final_level
    _mark_analytics_dirty()
    await _announce_events(context, user_id, events)
    await backup_analytics_to_channel(context)

    if is_last:
        total     = session["total"]
        correct   = session["correct"]
        incorrect = session["answered"] - correct
        pct       = round(correct / session["answered"] * 100) if session["answered"] else 0
        year      = session["year"]
        duration  = session.get("duration_seconds", 0.0)
        _record_daily_leaderboard_finish(user_id, year, correct, session["answered"], duration)

        # Daily Quiz completion count + its tier achievement, plus the
        # basmagy/insomniac Extras — all reuse data this block already
        # has (duration/pct) or that _finalize_daily_leaderboard's medal
        # tally elsewhere reuses the same DAILY_QUIZ_LEADERBOARD entry
        # for, rather than tracking anything new.
        user_entry["daily_quizzes_completed"] = user_entry.get("daily_quizzes_completed", 0) + 1
        daily_events = _check_achievements(user_entry, "daily_quiz")
        if duration > 0 and duration < 60 and session["answered"] > 0 and pct == 100:
            ach = _check_extra_achievement(user_entry, "basmagy")
            if ach:
                daily_events.append(ach)
        now_hour = datetime.now(DAILY_QUIZ_TZ).hour
        if 2 <= now_hour < 5:
            ach = _check_extra_achievement(user_entry, "insomniac")
            if ach:
                daily_events.append(ach)
        daily_events += _check_achievements(user_entry, "achievement_collector")
        daily_level = 0
        final_level = _xp_to_level(user_entry["xp"])
        if final_level > user_entry["level"]:
            user_entry["level"] = final_level
            daily_level = final_level
        _mark_analytics_dirty()
        await _announce_events(context, user_id, {"achievements": daily_events, "level_up": daily_level})
        await backup_analytics_to_channel(context)

        summary = (
            f"💥 <b>خلصت الـ Daily Quiz!</b>\n\n"
            f"✅ صح: {correct}\n"
            f"❌ غلط: {incorrect}\n"
            f"📊 نسبة: {pct}%\n"
            f"📝 عدد الأسئلة: {session['answered']}/{total}\n"
            f"✨ XP: <b>+{session['xp_earned']}</b>\n\n"
            + (f"{QUIZZY_PERFECT_SCORE_LINE}\n\n" if session["answered"] > 0 and pct == 100 else "")
            + f"{_next_daily_quiz_line()}"
        )
        try:
            await context.bot.send_message(
                chat_id=user_id, text=summary, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏆 Daily Leaderboard", callback_data="daily_quiz"),
                    InlineKeyboardButton("🏠 Back to Home", callback_data="back_home"),
                ]]),
            )
        except Exception:
            pass
        DAILY_QUIZ_SESSIONS.pop(user_id, None)

def get_daily_quiz_last_date(user_id: int) -> str | None:
    return SETTINGS.get(str(user_id), {}).get("daily_quiz_last_date")

# ── Admin-set Daily Quiz scope ──────────────────────────────────
# By default the random-questions pool for each year's Daily Quiz draws
# from that year's whole curriculum. An admin can narrow one year to a
# specific module (e.g. whatever's currently being taught) via
# /daily_module — see _daily_quiz_subject_pool. (The mistakes bank has
# its own separate 🧠 Mistakes Bank retake flow — see _scoped_mistakes_bank —
# no longer feeds into the Daily Quiz.)
#
# Stored under a reserved key in SETTINGS (not a per-user key — this is a
# single global switch) so it rides on the exact same backup/restore path
# as everything else there, with no new infrastructure needed.
def get_daily_quiz_scope() -> dict | None:
    """{"year": ..., "module": ...} to restrict the subject pool to one
    module, or None for the default (every configured year/module)."""
    return SETTINGS.get("_daily_quiz_scope")

async def set_daily_quiz_scope(year: str | None, module: str | None) -> None:
    if year and module:
        SETTINGS["_daily_quiz_scope"] = {"year": year, "module": module}
    else:
        SETTINGS.pop("_daily_quiz_scope", None)
    await save_settings()

async def _prompt_daily_quiz_year_class(context: ContextTypes.DEFAULT_TYPE, user_id: int, message=None) -> None:
    """Shown in place of the Daily Quiz hub when the user hasn't set a
    Year/Class in Settings yet — we need it to know which year's shared
    quiz to give them. Reuses the same picker as onboarding/Settings, its
    own "dqyc:" callback prefix routes back into show_daily_quiz_menu
    once they pick one instead of Settings or the onboarding welcome."""
    text = "📚 محتاج تحدد سنتك/فرقتك الأول، عشان نجيبلك الـ Daily Quiz بتاع سنتك:"
    keyboard = year_class_keyboard("dqyc")
    if message:
        await message.edit_text(text, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)

async def show_daily_quiz_menu(context: ContextTypes.DEFAULT_TYPE, user_id: int, message=None) -> None:
    """The 💥Daily Quiz💥 main-menu button's destination: always shows
    today's Daily Quiz leaderboard for the user's own Year/Class
    (get_year_class — prompting them to set it first if they haven't),
    plus a Start button if they haven't run today's quiz yet — this is
    also where the leaderboard lives now, instead of its own separate
    main-menu button."""
    year_class = get_year_class(user_id)
    if year_class not in YEAR_CLASS_NUMBER:
        await _prompt_daily_quiz_year_class(context, user_id, message)
        return

    _ensure_daily_leaderboard_fresh()
    rows = _rank_daily_leaderboard(year_class)
    board_title = f"🏆 <b>Daily Quiz Leaderboard — {year_class_label(year_class)} — النهاردة</b>"
    if not rows:
        board_text = f"{board_title}\n\nمفيش حد خلص الـ Daily Quiz النهاردة لسه."
    else:
        medal = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = [board_title, ""]
        for i, r in enumerate(rows):
            rank = medal.get(i, f"{i + 1}.")
            lines.append(
                f"{rank} {html.escape(r['name'])} — "
                f"{r['correct']}/{r['total']} ✅ · ⏱️ {_format_duration(r['duration'])}"
            )
        board_text = "\n".join(lines)

    already_done = get_daily_quiz_last_date(user_id) == _today()
    status_line = (
        f"✅ خلصت الـ Daily Quiz بتاع النهاردة خلاص.\n\n{_next_daily_quiz_line()}"
        if already_done else
        f"💥 اضغط تحت تبدأ الـ Daily Quiz بتاع النهاردة — {DAILY_QUIZ_TOTAL_COUNT} سؤال."
    )
    text = f"{board_text}\n\n{status_line}"

    rows_buttons = []
    if not already_done:
        rows_buttons.append([InlineKeyboardButton("▶️ Start Daily Quiz", callback_data="daily_quiz_begin")])
    rows_buttons.append([InlineKeyboardButton("🏠 Back to Home", callback_data="back_home")])
    keyboard = InlineKeyboardMarkup(rows_buttons)

    if message:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def start_daily_quiz(context: ContextTypes.DEFAULT_TYPE, user_id: int, message=None) -> None:
    """Starts the user's own year's shared Daily Quiz run. message, if
    given, gets edited with the "starting..." line instead of a fresh
    message being sent (matches the lecture-start button pattern).
    Once-per-day gating happens here, keyed off the caller's local
    calendar date at the time they tap — not the push time — so someone
    who gets the 2pm ping but taps it at 11pm still only gets today's
    quiz once."""
    year_class = get_year_class(user_id)
    if year_class not in YEAR_CLASS_NUMBER:
        await _prompt_daily_quiz_year_class(context, user_id, message)
        return

    today = _today()
    if get_daily_quiz_last_date(user_id) == today:
        # Already done — send them back to the hub (leaderboard + status)
        # rather than re-starting it.
        await show_daily_quiz_menu(context, user_id, message)
        return

    questions = await get_daily_quiz_questions(context, year_class)
    if not questions:
        text = "لسه مفيش أسئلة كفاية النهاردة — جرب تاني بعدين."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Home", callback_data="back_home")]])
        if message:
            await message.edit_text(text, reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
        return

    entry = _get_settings_entry(user_id)
    entry["daily_quiz_last_date"] = today
    # Hot path (every Daily Quiz start, from every user) — defer the full
    # SETTINGS file rewrite to the periodic flush instead of paying for one
    # on every single tap. See _mark_settings_dirty above. The channel
    # mirror call below is cheap regardless: it's already throttled to
    # SETTINGS_BACKUP_MIN_INTERVAL and no-ops on most calls.
    _mark_settings_dirty()
    await backup_settings_to_channel(context)

    session = {
        "queue": list(questions), "current_poll_id": None, "current_correct_id": None,
        "current_message_id": None, "total": len(questions), "answered": 0, "correct": 0,
        "year": year_class, "kind": "daily",
    }
    DAILY_QUIZ_SESSIONS[user_id] = session

    text = f"💥 <b>Daily Quiz</b> — {len(questions)} سؤال 👇"
    if message:
        await message.edit_text(text, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)

    sent = await _deliver_next_daily_question(context, user_id, session)
    if not sent:
        DAILY_QUIZ_SESSIONS.pop(user_id, None)
        await context.bot.send_message(chat_id=user_id, text="⚠️ حصلت مشكلة في تجهيز الأسئلة — جرب تاني.")

# ── Hourly Zikr reminder — Settings toggle, ON by default ──────────
# One line sent once an hour, aligned to the real clock hour (1:00pm,
# 2:00pm, 3:00pm, ... in DAILY_QUIZ_TZ — see _next_top_of_hour_delay
# and job_queue.run_repeating in MAIN) to every user who's opted in via
# Settings -> More Settings -> Hourly Zikr. Purely a devotional nudge,
# no interaction/state of its own — unlike the Daily Quiz push, there's
# no button or follow-up here. One line is picked at random from the
# pool each time _zikr_push_job fires, so it's not the same line every
# hour.
ZIKR_POOL = [
    "📿 سبحان الله، والحمدُ لله، ولا إله إلا اللهُ، واللهُ أكبرُ، ولا حولَ ولا قوةَ إلا بالله. ❤️",
    "📿 سُبْحَانَ اللهِ وَبِحَمْدِهِ، سُبْحَانَ اللهِ الْعَظِيمِ. ❤️",
    "📿 أَسْتَغْفِرُ اللهَ الَّذِي لَا إِلٰهَ إِلَّا هُوَ، الْحَيُّ الْقَيُّومُ، وَأَتُوبُ إِلَيْهِ. ❤️",
    "📿 اللَّهُمَّ صَلِّ وَسَلِّمْ وَبَارِكْ عَلَى نَبِيِّنَا مُحَمَّدٍ. ❤️",
]

def _next_top_of_hour_delay(tz: ZoneInfo) -> float:
    """Seconds from now until the next top of the hour (e.g. 1:00, 2:00,
    3:00 ...) in `tz`. Used as the `first=` delay for the hourly Zikr
    job so it lands on real clock-hours instead of firing an hour after
    whatever moment the bot happened to start."""
    now = datetime.now(tz)
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return (next_hour - now).total_seconds()

async def _zikr_push_job(context: ContextTypes.DEFAULT_TYPE):
    """Hourly push (see job_queue.run_repeating in MAIN): a random zikr
    line from ZIKR_POOL, picked once per firing (so it's the same line
    for everyone that hour, but varies hour to hour) to every user who's
    opted in (get_zikr_enabled). Skips sleeping users the same way the
    Daily Quiz push skips opted-out ones — no reason to nudge someone
    who's muted the bot."""
    text = random.choice(ZIKR_POOL)
    for uid in list(USERS):
        if uid in SLEEPING or not get_zikr_enabled(uid):
            continue
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            pass   # blocked the bot, deactivated account, etc. — skip silently, same as broadcast_cmd

# ── Daily zipped backup export — a second, independent copy ────────
# Every JSON file this bot maintains is already kept in sync with a
# per-system pinned-message backup in its own channel/group (see the
# backup_*_to_channel / restore_*_from_channel functions throughout this
# file). This job is a belt-and-suspenders extra on top of that: once a
# day it zips up every local data file that currently exists and drops
# the archive straight into ERROR_LOG_GROUP_ID (a chat the admin already
# watches), so there's a single flat file with everything in one place
# even if a backup channel/group itself were ever lost or misconfigured.
def _daily_backup_export_file_paths() -> list:
    paths = [
        USERS_FILE, ANALYTICS_FILE, SETTINGS_FILE, LECTURE_RESULTS_FILE,
        MISTAKES_BANK_FILE, STORAGE_INDEX_FILE, STORAGE_BACKUP_STATE_FILE,
        REPORT_THREADS_FILE,
    ]
    for year in YEAR_ORDER:
        paths += [
            QUIZ_INDEX_FILE_TMPL.format(year=year),
            QUIZ_STATE_FILE_TMPL.format(year=year),
            QUIZ_POLL_STATUS_FILE_TMPL.format(year=year),
        ]
    return paths

async def _daily_backup_export_job(context: ContextTypes.DEFAULT_TYPE):
    """Once a day (see job_queue.run_daily in MAIN, DAILY_BACKUP_EXPORT_HOUR/
    MIN): zips every local data file that currently exists (a file that
    was never created yet — e.g. a year with no channel configured — is
    just skipped, not an error) and sends the archive to
    ERROR_LOG_GROUP_ID. The zip is built off the event loop
    (asyncio.to_thread) since zipping is blocking I/O, and the temp file
    is always cleaned up afterwards, success or failure."""
    if not ERROR_LOG_GROUP_ID:
        return
    existing = [p for p in _daily_backup_export_file_paths() if os.path.exists(p)]
    if not existing:
        return

    date_str = datetime.now(DAILY_QUIZ_TZ).strftime("%Y-%m-%d")
    zip_path = os.path.join(tempfile.gettempdir(), f"quizician_backup_{date_str}.zip")

    def _make_zip():
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in existing:
                zf.write(p, arcname=os.path.basename(p))

    try:
        await asyncio.to_thread(_make_zip)
        with open(zip_path, "rb") as f:
            await context.bot.send_document(
                chat_id=ERROR_LOG_GROUP_ID,
                document=InputFile(f, filename=os.path.basename(zip_path)),
                caption=f"🗄 Daily backup export — {date_str} ({len(existing)} files)",
            )
    except Exception as e:
        print(f"Daily backup export failed: {e}")
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

async def _daily_quiz_push_job(context: ContextTypes.DEFAULT_TYPE):
    """The 2pm-Cairo push (see job_queue.run_daily in MAIN): just a
    button in each user's chat, not an auto-started quiz — tapping it is
    what calls start_daily_quiz and applies the once-per-day gate. Skips
    anyone who's turned it off via Settings -> More Settings -> Daily
    Notification (get_daily_notifs_enabled) — the quiz itself stays
    reachable from the main menu either way, this only silences the ping.

    Finalizes (awards medals for) the outgoing day's Daily Quiz
    leaderboard FIRST, right as this next one goes out — so top-3
    finishers get their medal exactly when the new quiz appears, instead
    of whenever someone next happens to open the leaderboard screen."""
    _finalize_daily_leaderboard()
    _ensure_daily_leaderboard_fresh()   # rolls the tracked date forward for today's fresh board
    for uid in list(USERS):
        if not get_daily_notifs_enabled(uid):
            continue
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="💥 <b>Daily Quiz</b> !الكويز اليومي أتجدد",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("💥Daily Quiz💥", callback_data="daily_quiz"),
                ]]),
            )
        except Exception:
            pass   # blocked the bot, deactivated account, etc. — skip silently, same as broadcast_cmd

# ═══════════════════════════════════════════════════════════════
# MISTAKES BANK RETAKE — 🧠 Mistakes Bank menu button lets a user fire off
# every question in the (module-scoped) MISTAKES_BANK as a one-shot
# practice quiz. Same self-contained-question shape and delivery mechanics
# as the Daily Quiz (_deliver_next_daily_question works unchanged here —
# it only ever touches the passed-in session dict), just its own session
# map and completion message so it doesn't collide with an in-flight Daily
# Quiz for the same user.
# ═══════════════════════════════════════════════════════════════
MISTAKES_RETAKE_SESSIONS = {}   # user_id -> same session shape as DAILY_QUIZ_SESSIONS

async def start_mistakes_retake(context: ContextTypes.DEFAULT_TYPE, user_id: int, message=None) -> None:
    """Every question currently in this user's mistakes bank, restricted to
    the admin-set /daily_module scope (or the whole bank if no scope is
    set), sent one at a time. No once-per-day gate — unlike the Daily
    Quiz, this is an on-demand review the user can retake as often as they
    like."""
    entries   = list(_scoped_mistakes_bank(user_id))
    questions = await _resolve_mistakes(context, entries) if entries else []
    if not questions:
        text = "أما أنت كينج صحيح - 🎉 مفيش أخطاء متسجلة في بنك الأخطاء دلوقتي!"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Home", callback_data="back_home")]])
        if message:
            await message.edit_text(text, reply_markup=keyboard)
        else:
            await context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
        return

    random.shuffle(questions)
    session = {
        "queue": questions, "current_poll_id": None, "current_correct_id": None,
        "current_message_id": None, "total": len(questions), "answered": 0, "correct": 0,
        "kind": "retake",
    }
    MISTAKES_RETAKE_SESSIONS[user_id] = session

    text = f"🧠 <b>مراجعة بنك الأخطاء</b> — {len(questions)} سؤال، هيتبعتولك واحد واحد 👇"
    if message:
        await message.edit_text(text, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)

    sent = await _deliver_next_daily_question(context, user_id, session)
    if not sent:
        MISTAKES_RETAKE_SESSIONS.pop(user_id, None)
        await context.bot.send_message(chat_id=user_id, text="⚠️ حصلت مشكلة في تجهيز الأسئلة — جرب تاني.")

async def _advance_mistakes_retake_session(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict, is_correct: bool, message_id: int | None, delivered_at: float | None = None):
    """Mistakes-retake counterpart to _advance_daily_quiz_session — same
    XP/streak/achievement bookkeeping, its own completion summary text."""
    session["answered"] += 1
    session["correct"] = session.get("correct", 0) + (1 if is_correct else 0)
    if delivered_at is not None:
        _record_time_spent(user_id, time.time() - delivered_at)

    sent_next = await _deliver_next_daily_question(context, user_id, session)
    is_last   = not sent_next

    per_question_xp = XP_LECTURE_CORRECT if is_correct else XP_LECTURE_INCORRECT
    xp_delta = per_question_xp + (XP_LECTURE_COMPLETE_BONUS if is_last else 0)
    session["xp_earned"] = session.get("xp_earned", 0) + xp_delta

    events     = await _record_activity(user_id, persist=False)
    user_entry = _get_entry(user_id)
    prev_streak = user_entry.get("lecture_correct_streak_current", 0)
    user_entry["lecture_questions_answered"]  += 1
    user_entry["lecture_questions_correct"]   += 1 if is_correct else 0
    user_entry["lecture_questions_incorrect"] += 0 if is_correct else 1
    _record_subject_answer(user_entry, session.get("current_module"), session.get("current_subject"), is_correct)
    if is_correct:
        user_entry["lecture_correct_streak_current"] += 1
        if user_entry["lecture_correct_streak_current"] > user_entry["lecture_correct_streak_best"]:
            user_entry["lecture_correct_streak_best"] = user_entry["lecture_correct_streak_current"]
    else:
        user_entry["lecture_correct_streak_current"] = 0

    await _react_to_lecture_answer(
        context, user_id, message_id,
        is_correct=is_correct,
        new_streak=user_entry["lecture_correct_streak_current"],
        streak_broken=(not is_correct and prev_streak > 0),
    )

    events["achievements"] += _check_achievements(user_entry, "questions_answered")
    events["achievements"] += _check_achievements(user_entry, "correct_streak")
    events["achievements"] += _check_achievements(user_entry, "achievement_collector")

    _award_xp(user_entry, xp_delta)
    final_level = _xp_to_level(user_entry["xp"])
    if final_level > user_entry["level"]:
        user_entry["level"] = final_level
        events["level_up"] = final_level
    _mark_analytics_dirty()
    await _announce_events(context, user_id, events)
    await backup_analytics_to_channel(context)

    if is_last:
        total     = session["total"]
        correct   = session["correct"]
        incorrect = session["answered"] - correct
        pct       = round(correct / session["answered"] * 100) if session["answered"] else 0
        summary = (
            f"🧠 <b>خلصت مراجعة بنك الأخطاء!</b>\n\n"
            f"✅ صح: {correct}\n"
            f"❌ غلط: {incorrect}\n"
            f"📊 نسبة: {pct}%\n"
            f"📝 عدد الأسئلة: {session['answered']}/{total}\n"
            f"✨ XP: <b>+{session['xp_earned']}</b>"
        )
        try:
            await context.bot.send_message(
                chat_id=user_id, text=summary, parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Back to Home", callback_data="back_home"),
                ]]),
            )
        except Exception:
            pass
        MISTAKES_RETAKE_SESSIONS.pop(user_id, None)

# ═══════════════════════════════════════════════════════════════
# PASSWORD-GATED STORAGE (private group)
# ═══════════════════════════════════════════════════════════════
# STORAGE_GROUP_ID is the vault: post any photo/video/document/album there
# with a caption starting with a password word, and the bot indexes it.
# A DM containing that exact word gets the item(s) copied to the user.
STORAGE_INDEX_FILE = "storage_index.json"

def load_storage_index():
    if os.path.exists(STORAGE_INDEX_FILE):
        with open(STORAGE_INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

async def save_storage_index():
    # See save_analytics for why this snapshot copy is required.
    snapshot = copy.deepcopy(STORAGE_INDEX)
    await asyncio.to_thread(_atomic_write_json, STORAGE_INDEX_FILE, snapshot, ensure_ascii=False)

# password (lowercased) -> list of items; each item is a list of message_ids
# (a single-message item is [id], an album is [id1, id2, ...]). Reusing the
# same password just appends another item — both get delivered on unlock.
STORAGE_INDEX: dict = load_storage_index()

# media_group_id -> {"ids": [...], "caption": str|None, "task": asyncio.Task}
# Albums arrive as several separate updates; we debounce them so the whole
# album gets filed as one item under one password.
ALBUM_BUFFER: dict = {}

# ── Durable backup: the local JSON files above are only a same-host cache.
# A bot can't scan a channel's history, but it CAN always read a chat's
# currently pinned message on demand — restart, redeploy, or host switch
# doesn't matter. So we mirror USERS + STORAGE_INDEX into one pinned
# message in the storage group itself, and rebuild the local cache from
# it on startup if the local files are ever missing/wiped.
STORAGE_BACKUP_MARKER     = "🗄 QUIZICIAN_STORAGE_BACKUP"
STORAGE_BACKUP_STATE_FILE = "storage_backup_state.json"

def load_storage_backup_state():
    if os.path.exists(STORAGE_BACKUP_STATE_FILE):
        with open(STORAGE_BACKUP_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

async def save_storage_backup_state():
    # Tiny dict, but kept consistent with every other save_* here — see
    # save_analytics for why the snapshot copy matters.
    snapshot = copy.deepcopy(STORAGE_BACKUP_STATE)
    await asyncio.to_thread(_atomic_write_json, STORAGE_BACKUP_STATE_FILE, snapshot, ensure_ascii=False)

STORAGE_BACKUP_STATE: dict = load_storage_backup_state()  # {"backup_msg_id": int}

async def backup_storage_to_channel(context: ContextTypes.DEFAULT_TYPE):
    if not STORAGE_GROUP_ID:
        return
    if not RESTORE_OK["storage"]:
        print("STORAGE BACKUP SKIPPED — last restore failed, refusing to overwrite the channel backup.")
        return
    payload = {"users": list(USERS), "storage_index": STORAGE_INDEX}
    data    = json.dumps(payload).encode("utf-8")
    # A pinned document instead of a pinned text message: Bot API caps
    # documents at 50MB vs ~4KB for a text message — effectively removes
    # the size ceiling for any realistic amount of data this bot handles.
    filename = _backup_filename("quizician_storage_backup.json")

    try:
        sent = await context.bot.send_document(
            chat_id=STORAGE_GROUP_ID,
            document=InputFile(BytesIO(data), filename=filename),
            caption=STORAGE_BACKUP_MARKER,
        )
    except Exception as e:
        print("STORAGE BACKUP ERROR:", e)
        return

    # Save the message id immediately — pinning is a nice-to-have on top,
    # and its failure (e.g. bot isn't admin / lacks rights) must NOT stop
    # us from remembering this new message id.
    STORAGE_BACKUP_STATE["backup_msg_id"] = sent.message_id
    await save_storage_backup_state()

    try:
        await context.bot.pin_chat_message(chat_id=STORAGE_GROUP_ID, message_id=sent.message_id, disable_notification=True)
    except Exception as e:
        print("STORAGE BACKUP PIN ERROR (message saved anyway, but won't be pinned — check bot is admin with pin rights):", e)

    # Previous backups are no longer deleted — see the analytics backup
    # function's comment for why.

async def restore_storage_from_channel(app) -> str:
    """Runs once on startup — rebuilds USERS + STORAGE_INDEX from the
    storage group's pinned backup if the local cache is missing/stale."""
    if not STORAGE_GROUP_ID:
        return "not_configured"

    async def _do():
        chat   = await app.bot.get_chat(STORAGE_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document or (pinned.caption or "") != STORAGE_BACKUP_MARKER:
            raise _RestoreNoBackup()
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        payload = json.loads(bytes(raw).decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("users", []), list)
            or not isinstance(payload.get("storage_index", {}), dict)
        ):
            raise _RestoreInvalidArchitecture('expected {"users": [...], "storage_index": {...}}')
        USERS.update(payload.get("users", []))
        STORAGE_INDEX.update(payload.get("storage_index", {}))
        await save_users()
        await save_storage_index()
        STORAGE_BACKUP_STATE["backup_msg_id"] = pinned.message_id
        await save_storage_backup_state()
        print(f"Restored storage backup: {len(USERS)} user(s), {len(STORAGE_INDEX)} password(s).")

    not_found_hint = (
        f"Chat not found — STORAGE_GROUP_ID ({STORAGE_GROUP_ID}) isn't a real "
        "group this bot knows about. Still the template placeholder, wrong ID, "
        "or the bot was never added to that group. See the setup comment above "
        "STORAGE_GROUP_ID."
    )
    return await _run_restore_with_retries(app, "storage", "Storage", _do, not_found_hint=not_found_hint)

# ═══════════════════════════════════════════════════════════════
# QUIZ CHANNELS (interactive quiz storage, organized by year -> lecture)
# ═══════════════════════════════════════════════════════════════
# Post plain text in a year's quiz channel to open/resume a lecture (that
# text becomes the lecture's name), then post quiz polls one by one — each
# gets filed under the currently-open lecture, in posting order. Post
# "-END" to close the lecture. Users pick Year -> Module -> Subject ->
# Lecture via /quiz and the bot delivers the ready questions as fresh,
# independently-answerable polls (see _deliver_next_lecture_question).
#
# Everything below is keyed by year (one of YEARS' keys, e.g. "y1"/"y3"),
# with a separate on-disk file and a separate pinned backup document per
# year — that's the fix for the combined index eventually outgrowing
# Telegram's practical JSON document size as lectures pile up.
QUIZ_INDEX_FILE_TMPL = "quiz_index_{year}.json"

def _is_valid_quiz_index_entry(e) -> bool:
    """One QUIZ_INDEX[year][lecture_name] entry — checked against the
    schema in the QUIZ_INDEX comment just below: ids must be a list,
    closed a bool, and module/subject/lecture_number/name strings where
    present. Doesn't require every key (older entries or in-progress
    lectures may be missing one), just that whatever IS there is the
    right type — the same "shape, not completeness" check as
    _is_valid_analytics_entry, for the same reason: this is a guard
    against corruption, not a schema-completeness enforcer."""
    if not isinstance(e, dict):
        return False
    if "ids" in e and not isinstance(e["ids"], list):
        return False
    if "closed" in e and not isinstance(e["closed"], bool):
        return False
    for k in ("module", "subject", "lecture_number", "name"):
        if k in e and not isinstance(e[k], str):
            return False
    return True

def _clean_quiz_index_dict(year: str, raw: dict) -> dict:
    """Filters a whole QUIZ_INDEX[year]-shaped dict, dropping any lecture
    entry that fails _is_valid_quiz_index_entry. Used by both
    load_quiz_index and restore_quiz_from_channel."""
    if not isinstance(raw, dict):
        print(f"QUIZ INDEX ({year}): top-level data wasn't a dict ({type(raw).__name__}) — ignoring entirely.")
        return {}
    clean = {k: v for k, v in raw.items() if _is_valid_quiz_index_entry(v)}
    if len(clean) != len(raw):
        print(f"QUIZ INDEX ({year}): dropped {len(raw) - len(clean)} malformed lecture entr(y/ies).")
    return clean

def load_quiz_index(year: str) -> dict:
    path = QUIZ_INDEX_FILE_TMPL.format(year=year)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return _clean_quiz_index_dict(year, raw)
    return {}

async def save_quiz_index(year: str):
    # See save_analytics for why this snapshot copy is required — this is
    # one of the highest-traffic save_* calls in the file (fires on every
    # dead-poll cleanup during lecture delivery), so it's one of the most
    # likely places the load test's races actually came from.
    path = QUIZ_INDEX_FILE_TMPL.format(year=year)
    snapshot = copy.deepcopy(QUIZ_INDEX[year])
    await asyncio.to_thread(_atomic_write_json, path, snapshot, ensure_ascii=False)

# year -> {lecture_name -> {"ids": [...], "closed": bool, "module": str, "subject": str, "lecture_number": str, "name": str}}
QUIZ_INDEX: dict = {y: load_quiz_index(y) for y in YEARS}

# Matches "<Subject> Lecture <number>", e.g. "Physiology Lecture 3"
_LECTURE_TITLE_RE = re.compile(r"^(.*?)\s+Lecture\s+(\d+)\s*$", re.IGNORECASE)

def parse_lecture_title(year: str, text: str):
    """Parses "<Module> - <Subject> Lecture <number>: <name>" against this
    year's curriculum (YEARS[year]["modules"]). Returns
    (module, subject, lecture_number, name) on success, or
    (None, None, None, error_message) on failure — matching is
    case-insensitive but the canonical spelling from that year's modules
    dict is returned."""
    modules = year_modules(year)
    if " - " not in text or ":" not in text:
        return None, None, None, (
            "⚠️ الصيغة غلط. لازم تكون:\n"
            "<code>Module - Subject Lecture Number: Name</code>\n"
            "مثال: <code>Endocrine - Physiology Lecture 3: Insulin Signaling</code>"
        )
    module_part, rest = text.split(" - ", 1)
    subj_lec_part, name = rest.split(":", 1)
    module_part, subj_lec_part, name = module_part.strip(), subj_lec_part.strip(), name.strip()

    module_match = next((m for m in modules if m.lower() == module_part.lower()), None)
    if not module_match:
        valid = ", ".join(modules.keys())
        return None, None, None, f"⚠️ الموديول \"{module_part}\" مش معروف. الموديولات المتاحة: {valid}"

    m = _LECTURE_TITLE_RE.match(subj_lec_part)
    if not m:
        return None, None, None, (
            "⚠️ الصيغة غلط بعد اسم الموديول. لازم تكون:\n"
            "<code>Subject Lecture Number</code>\n"
            "مثال: <code>Physiology Lecture 3</code>"
        )
    subject_part, lecture_number = m.group(1).strip(), m.group(2).strip()
    subject_match = next((s for s in modules[module_match] if s.lower() == subject_part.lower()), None)
    if not subject_match:
        valid = ", ".join(modules[module_match])
        return None, None, None, f"⚠️ المادة \"{subject_part}\" مش من موديول {module_match}. المواد المتاحة: {valid}"

    return module_match, subject_match, lecture_number, name

def ready_modules(year: str):
    # Always show every configured module — even ones with zero lectures
    # posted yet — so the curriculum structure is visible from day one.
    return list(year_modules(year).keys())

def ready_subjects(year: str, module: str):
    # Same idea: every subject defined for this module shows up, regardless
    # of whether any lecture has been posted for it yet.
    return list(year_modules(year).get(module, []))

def ready_lecture_keys(year: str, module: str, subject: str):
    return [
        name for name, v in QUIZ_INDEX[year].items()
        if v["closed"] and v["ids"] and v["module"] == module and v["subject"] == subject
    ]  # insertion order = numbering order

QUIZ_STATE_FILE_TMPL = "quiz_state_{year}.json"

def load_quiz_state(year: str) -> dict:
    path = QUIZ_STATE_FILE_TMPL.format(year=year)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"current_lecture": None}

async def save_quiz_state(year: str):
    # See save_analytics for why this snapshot copy is required.
    path = QUIZ_STATE_FILE_TMPL.format(year=year)
    snapshot = copy.deepcopy(QUIZ_STATE[year])
    await asyncio.to_thread(_atomic_write_json, path, snapshot, ensure_ascii=False)

QUIZ_STATE: dict = {y: load_quiz_state(y) for y in YEARS}  # survives restarts mid-lecture, per year

QUIZ_POLL_STATUS_FILE_TMPL = "quiz_poll_status_{year}.json"

def load_quiz_poll_status(year: str) -> dict:
    path = QUIZ_POLL_STATUS_FILE_TMPL.format(year=year)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

async def save_quiz_poll_status(year: str):
    # See save_analytics for why this snapshot copy is required — also
    # high-traffic (fires on every question delivered), and this exact
    # structure is what poll_status_by_mid is built from, read
    # concurrently by every student's lecture session.
    path = QUIZ_POLL_STATUS_FILE_TMPL.format(year=year)
    snapshot = copy.deepcopy(QUIZ_POLL_STATUS[year])
    await asyncio.to_thread(_atomic_write_json, path, snapshot, ensure_ascii=False)

# year -> {poll_id -> {"lecture": str, "message_id": int, "closed": bool, ...}}
# Tracks whether each quiz-channel poll has been stopped yet — Telegram
# only allows copying a quiz poll once its correct answer is known, i.e.
# once it's been stopped, so this is what /quiz delivery checks against.
QUIZ_POLL_STATUS: dict = {y: load_quiz_poll_status(y) for y in YEARS}

# ── Durable backup: same pinned-message trick as the storage group, so
# each year's lecture/quiz index survives a host switch or wiped local
# disk — only the local JSON cache is fragile, the channel content itself
# never was. One pinned backup document per year's own channel.
QUIZ_BACKUP_MARKER          = "🗄 QUIZICIAN_QUIZ_BACKUP"
QUIZ_BACKUP_STATE_FILE_TMPL = "quiz_backup_state_{year}.json"

def load_quiz_backup_state(year: str) -> dict:
    path = QUIZ_BACKUP_STATE_FILE_TMPL.format(year=year)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

async def save_quiz_backup_state(year: str):
    # Tiny dict, but kept consistent — see save_analytics for why.
    path = QUIZ_BACKUP_STATE_FILE_TMPL.format(year=year)
    snapshot = copy.deepcopy(QUIZ_BACKUP_STATE[year])
    await asyncio.to_thread(_atomic_write_json, path, snapshot, ensure_ascii=False)

QUIZ_BACKUP_STATE: dict = {y: load_quiz_backup_state(y) for y in YEARS}  # year -> {"backup_msg_id": int}

async def backup_quiz_to_channel(context: ContextTypes.DEFAULT_TYPE, year: str):
    channel_id = year_channel_id(year)
    if not channel_id:
        return
    if not RESTORE_OK.get(f"quiz_{year}", True):
        print(f"QUIZ BACKUP ({year}) SKIPPED — last restore failed, refusing to overwrite the channel backup.")
        return
    payload  = {
        "quiz_index":       QUIZ_INDEX[year],
        "quiz_state":       QUIZ_STATE[year],
        "quiz_poll_status": QUIZ_POLL_STATUS[year],
    }
    data     = json.dumps(payload).encode("utf-8")
    # NOTE: named Quizician_Quiz_Backup (not *_Storage_Backup) even though
    # this is the file colloquially thought of as "the storage backup" for
    # a year's questions — quizician_storage_backup.json (no year suffix)
    # is a different, unrelated file: the password-gated media vault. Two
    # files named near-identically would be a landmine for future greps.
    filename = _backup_filename(f"Quizician_Quiz_Backup_{year.upper()}.json")

    try:
        sent = await context.bot.send_document(
            chat_id=channel_id,
            document=InputFile(BytesIO(data), filename=filename),
            caption=QUIZ_BACKUP_MARKER,
        )
    except Exception as e:
        print(f"QUIZ BACKUP ({year}) ERROR:", e)
        return

    # Persist the message id regardless of whether pinning succeeds, so we
    # don't resend a fresh document on every single addition.
    QUIZ_BACKUP_STATE[year]["backup_msg_id"] = sent.message_id
    await save_quiz_backup_state(year)

    try:
        await context.bot.pin_chat_message(chat_id=channel_id, message_id=sent.message_id, disable_notification=True)
    except Exception as e:
        print(f"QUIZ BACKUP ({year}) PIN ERROR (message saved anyway, but won't be pinned — check bot is admin with pin rights):", e)

    # Previous backups are no longer deleted — see the analytics backup
    # function's comment for why.

async def restore_quiz_from_channel(app, year: str) -> str:
    """Runs once on startup per year — rebuilds that year's lecture/quiz
    index from its quiz channel's pinned backup if the local cache is
    missing/stale."""
    channel_id = year_channel_id(year)
    if not channel_id:
        return "not_configured"

    async def _do():
        chat   = await app.bot.get_chat(channel_id)
        pinned = chat.pinned_message
        if not pinned or not pinned.document or (pinned.caption or "") != QUIZ_BACKUP_MARKER:
            raise _RestoreNoBackup()
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        payload = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(payload, dict) or not all(
            isinstance(payload.get(k, {}), dict)
            for k in ("quiz_index", "quiz_state", "quiz_poll_status")
        ):
            raise _RestoreInvalidArchitecture(
                'expected {"quiz_index": {...}, "quiz_state": {...}, "quiz_poll_status": {...}}'
            )
        QUIZ_INDEX[year].update(_clean_quiz_index_dict(year, payload.get("quiz_index", {})))
        QUIZ_STATE[year].update(payload.get("quiz_state", {}))
        QUIZ_POLL_STATUS[year].update(payload.get("quiz_poll_status", {}))
        await save_quiz_index(year)
        await save_quiz_state(year)
        await save_quiz_poll_status(year)
        QUIZ_BACKUP_STATE[year]["backup_msg_id"] = pinned.message_id
        await save_quiz_backup_state(year)
        print(f"Restored quiz backup ({year}): {len(QUIZ_INDEX[year])} lecture(s).")

    not_found_hint = (
        f"Chat not found — {year}'s channel_id ({channel_id}) isn't a real "
        "channel this bot knows about. Wrong ID, or the bot was never added "
        "as an admin there. See the YEARS setup comment near the top of the file."
    )
    return await _run_restore_with_retries(app, f"quiz_{year}", f"Quiz index ({year})", _do, not_found_hint=not_found_hint)

# ═══════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════
SLEEPING               = set()

# ── /health support ──────────────────────────────────────────────
# In-memory only, so it resets on restart — noted explicitly in the
# /health output rather than papered over, since a restart is exactly the
# kind of event this dashboard exists to surface.
_BOT_STARTED_AT   = time.monotonic()
# Timestamps of errors actually posted to ERROR_LOG_GROUP_ID (appended
# only on a successful send — see global_error_handler) — NOT every
# exception the handler saw, since a send that itself failed never made
# it into that channel. This deliberately mirrors "what's actually in the
# errors channel" rather than tracking exceptions independently, since
# the Bot API has no way to read a channel's message history back to
# verify the two ever matched. Self-trimmed to the last ~26h.
_ERROR_LOG_TIMES: list = []
_ERROR_LOG_MAX_AGE_SECONDS = 26 * 3600   # a bit over a day of headroom; /health itself filters to exactly 24h

PENDING_IMAGE          = {}    # user_id -> local path of an image awaiting its question
LECTURE_SESSIONS       = {}    # user_id -> {"year","module","subject","lecture_key","queue":[mid,...],
                                #             "current_poll_id","total","answered",
                                #             "poll_status_by_mid": {mid: QUIZ_POLL_STATUS[year][pid], ...}
                                #             — scoped to this lecture's polls, built once at session
                                #             start so _advance_lecture_session can look up a wrong
                                #             answer's content in O(1) instead of scanning the whole
                                #             year} — active one-at-a-time delivery
RETAKE_STAGING         = {}    # user_id -> {"year","module","subject","lecture_key","mids":[mid,...]}
                                # — wrong-question mids from a just-finished lecture, offered via the
                                # "🔁 Retake incorrect questions!" button; consumed (popped) once tapped
AWAITING_NICKNAME      = {}    # user_id -> True, while the Settings flow is waiting on a nickname reply
PENDING_QUIZ_DELETE    = {}    # admin_id -> (year, lecture_key), set by /quiz_delete while waiting on
                                # the confirm/cancel tap (see quizdel_yes/quizdel_no in button_handler)

# ── 🔎 Search Content support ────────────────────────────────────
# Set by the search_mod: callback (see button_handler) once the user has
# picked a year (always their locked year_class in practice) and a
# module (or "search all modules"); the next text message from that
# real_uid is treated as the search query instead of anything else the
# text handler would normally do with it — see the AWAITING_SEARCH_QUERY
# check near the top of handle(). Same RAM-only, restart-is-harmless
# convention as every other AWAITING_*/PENDING_* dict here.
AWAITING_SEARCH_QUERY  = {}    # real_uid -> {"year": str, "module": str|None}

# ── /broadcast support ────────────────────────────────────────────
# See the BROADCAST section (grep the banner) further down for the full
# interactive composer this backs — audience picker, message entry,
# live preview, estimated recipient count, then SEND with a progress
# bar. BROADCAST_DRAFTS holds the in-progress composition per admin
# (only one at a time each); AWAITING_BROADCAST_MESSAGE mirrors the
# AWAITING_NICKNAME pattern above for capturing the next free-text
# message as the broadcast body. Deliberately RAM-only, same as every
# other AWAITING_*/PENDING_* dict here — a restart mid-compose just
# means starting the /broadcast draft over, which is harmless.
BROADCAST_DRAFTS            = {}    # admin_id -> {"audience": "all"|"y1"|"y2"|"y3"|"active"|"inactive", "text": str|None}
AWAITING_BROADCAST_MESSAGE  = {}    # admin_id -> True, while waiting for the next text message to become the broadcast body

# ── /edit_quiz support ────────────────────────────────────────────
# QUIZ_INSERT_AFTER[year][lecture_key] = message_id (or None), set right
# before an admin is sent back into the quiz channel to add question(s)
# via the normal "post polls, then -END" flow, from the "➕ Insert new
# poll after" button in /edit_quiz. While this is set, incoming polls for
# that lecture in handle_quiz_channel_message are SPLICED into ids[] right
# after that message_id instead of being appended at the end — and the
# marker is advanced to each newly-inserted mid in turn, so posting
# several polls in a row keeps them in the order they were sent. Cleared
# on -END/-FIN alongside the normal lecture-close logic. None value =
# insert at the very start of ids[] (not currently reachable from the UI,
# but supported by the splice logic below for completeness).
QUIZ_INSERT_AFTER: dict = {y: {} for y in YEARS}

# ── /report_issue support ────────────────────────────────────────
# REPORT_THREADS mirrors the MISTAKES_BANK persistence pattern exactly:
# local JSON file, plus a pinned backup in REPORT_ISSUE_GROUP_ID that gets
# replaced (upload + pin + delete old pin) on every change and restored
# from on startup. See restore_report_threads_from_channel /
# backup_report_threads_to_channel below, and their registration
# alongside every other system's restore/backup calls near MAIN.
AWAITING_REPORT_ISSUE  = {}    # user_id -> float (time.time() when /report_issue ran). The next
                                # text OR photo from this user is staged as a draft report (see
                                # _stage_report_draft) — UNLESS more than REPORT_ISSUE_AWAIT_TIMEOUT
                                # has passed, in which case it's left alone and that message is
                                # handled normally instead. Also cleared by /cancel.
                                #
                                # Both the timeout and /cancel exist to fix the same bug: this used
                                # to be a plain `True` with no expiry and no way out, so ANY message
                                # sent any time after /report_issue — even an unrelated one hours or
                                # days later, after the person forgot they'd typed the command —
                                # got silently shipped to admins as a bogus report, and there was no
                                # way for the person to back out once they'd started.
REPORT_ISSUE_AWAIT_TIMEOUT   = 10 * 60   # seconds — how long /report_issue "listens" for the report
REPORT_DRAFTS = {}   # user_id -> {"text": str, "photo_file_id": str|None, "created_at": float}
                      # — a report the user has typed/sent but not yet confirmed via the Send/Cancel
                      # preview. See _stage_report_draft (creates it) and the report_draft_send /
                      # report_draft_cancel callback branches in button_handler (consume it).
REPORT_DRAFT_CONFIRM_TIMEOUT = 30 * 60   # seconds — a Send tap older than this is rejected rather
                                          # than silently posting a stale draft
AWAITING_REPORT_REPLY  = {}    # admin_id -> {"group_message_id": int}
                                # — set when the admin taps "↩️ Reply" on a report in REPORT_ISSUE_GROUP_ID;
                                # the admin's next text message there becomes the reply sent back to that user
AWAITING_USER_FOLLOWUP = {}    # reporter_user_id -> {"group_message_id": int}
                                # — set when the reporter taps "↩️ Reply" on the admin's reply DM'd to
                                # them; their next text message becomes a follow-up appended to the
                                # same thread (see _append_report_message) and shown to the admin

REPORT_THREADS_FILE          = "report_threads.json"
REPORT_THREADS_BACKUP_MARKER = "📩 QUIZICIAN_REPORT_THREADS_BACKUP"

def load_report_threads() -> dict:
    if os.path.exists(REPORT_THREADS_FILE):
        with open(REPORT_THREADS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}   # JSON keys are always strings — back to int here
    return {}

async def save_report_threads():
    # JSON object keys must be strings, so REPORT_THREADS (keyed by an
    # int message_id) needs the same str(k)/int(k) round-trip on the way
    # out and back in — see load_report_threads above.
    #
    # deepcopy, not just the str-keyed dict comprehension below: the
    # comprehension only copies the OUTER dict — each thread dict (and
    # its "messages" list) would still be the same live object the event
    # loop can keep mutating (e.g. a reply landing) while a background
    # thread is mid-serializing it. See save_analytics for the general
    # explanation of why to_thread needs a frozen snapshot.
    snapshot = {str(k): v for k, v in copy.deepcopy(REPORT_THREADS).items()}
    await asyncio.to_thread(
        _atomic_write_json, REPORT_THREADS_FILE,
        snapshot, indent=2, ensure_ascii=False,
    )

REPORT_THREADS: dict = load_report_threads()   # group_message_id -> {"user_id","name","username","user_text","messages","closed"}
                                                # — "messages": [{"from": "admin"|"user", "text": str}, ...] in
                                                # chronological order (see _append_report_message /
                                                # _report_thread_text); "replies" is the pre-follow-up shape,
                                                # still read as a fallback for threads that predate this field
                                                # — one entry per report ever filed, so the report message can be
                                                # rebuilt (user text + every reply so far) each time it's edited
                                                #
                                                # "latest_group_message_id" / "latest_user_message_id": the
                                                # message id of the most recent full-thread message on each
                                                # side (group card / reporter's DM) — see _refresh_report_thread.
                                                # Both start out equal to the dict key (the very first card IS
                                                # the first full-thread message) and get reassigned on every
                                                # reply; missing/None on a thread that predates this field or
                                                # hasn't had its user-side DM refreshed yet.

_report_threads_backup_msg_id: int | None = None
_last_report_threads_backup_at: float = 0.0
REPORT_THREADS_BACKUP_MIN_INTERVAL = 30   # seconds — same debounce as mistakes bank; local save is never throttled

async def backup_report_threads_to_channel(context):
    global _report_threads_backup_msg_id, _last_report_threads_backup_at
    if not REPORT_ISSUE_GROUP_ID:
        return
    if not RESTORE_OK.get("report_threads", True):
        print("REPORT THREADS BACKUP SKIPPED — last restore failed, refusing to overwrite the channel backup.")
        return
    now = time.monotonic()
    if now - _last_report_threads_backup_at < REPORT_THREADS_BACKUP_MIN_INTERVAL:
        return   # backed up recently enough — local save_report_threads() already has the latest data
    _last_report_threads_backup_at = now
    data = json.dumps({str(k): v for k, v in REPORT_THREADS.items()}, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        sent = await context.bot.send_document(
            chat_id=REPORT_ISSUE_GROUP_ID,
            document=InputFile(BytesIO(data), filename=_backup_filename("report_threads.json")),
            caption=REPORT_THREADS_BACKUP_MARKER,
        )
    except Exception as e:
        print("REPORT THREADS BACKUP ERROR:", e)
        return
    try:
        await context.bot.pin_chat_message(
            chat_id=REPORT_ISSUE_GROUP_ID,
            message_id=sent.message_id,
            disable_notification=True,
        )
    except Exception as e:
        print("REPORT THREADS PIN ERROR:", e)
    # Previous backups are no longer deleted — see the analytics backup
    # function's comment for why.
    _report_threads_backup_msg_id = sent.message_id

async def restore_report_threads_from_channel(app) -> str:
    global _report_threads_backup_msg_id
    if not REPORT_ISSUE_GROUP_ID:
        return "not_configured"

    async def _do():
        global _report_threads_backup_msg_id
        chat   = await app.bot.get_chat(REPORT_ISSUE_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document or (pinned.caption or "") != REPORT_THREADS_BACKUP_MARKER:
            raise _RestoreNoBackup()
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        restored = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(restored, dict):
            raise _RestoreInvalidArchitecture('expected a JSON object mapping thread_id -> thread data')
        try:
            cleaned = {int(k): v for k, v in restored.items()}
        except (TypeError, ValueError):
            raise _RestoreInvalidArchitecture('thread keys must be integer-like ids')
        REPORT_THREADS.clear()
        REPORT_THREADS.update(cleaned)
        await save_report_threads()
        _report_threads_backup_msg_id = pinned.message_id
        print(f"Restored report threads: {len(REPORT_THREADS)} thread(s).")

    return await _run_restore_with_retries(app, "report_threads", "Report threads", _do)

# ═══════════════════════════════════════════════════════════════
# SESSION PERSISTENCE — LECTURE_SESSIONS / DAILY_QUIZ_SESSIONS /
# MISTAKES_RETAKE_SESSIONS were previously purely in-memory (see the file
# index note at the top of this file) — a restart mid-quiz dropped every
# active session, which is exactly the "الجلسة دي اتقفلت" path in
# handle_poll_answer. This mirrors REPORT_THREADS' persistence pattern
# (local JSON + a pinned backup in its own channel, restored on startup)
# but tuned for much hotter, throwaway data:
#
#   - Change detection is a content hash taken once per tick, not a
#     dirty flag set at each mutation site. Sessions are mutated from
#     many call sites across lecture/daily-quiz/mistakes-retake delivery
#     (_deliver_next_lecture_question, _advance_lecture_session,
#     _deliver_next_daily_question, _advance_daily_quiz_session, and
#     their mistakes-retake counterparts, plus the two session-creation
#     sites) — flagging every one individually risks silently missing a
#     spot as the file changes. A hash comparison of the whole snapshot
#     costs one json.dumps per tick, which is cheap at this data's size.
#   - The channel backup is throttled to once every
#     SESSIONS_BACKUP_MIN_INTERVAL seconds (30s), same idea as every
#     other backup_*_to_channel — but unlike analytics/settings/
#     lecture_results/mistakes_bank/report_threads, which now keep every
#     backup ever taken (see backup_analytics_to_channel's comment), this
#     ONE deletes the previous pinned message on each new upload. A 30s
#     cadence keeping full history would post thousands of documents a
#     day for data nobody needs a history of.
#   - A session older than SESSIONS_MAX_AGE_SECONDS (48h) is dropped on
#     restore rather than revived — see _cleanup_stale_sessions_job's own
#     6h idle-based cleanup, which already reclaims abandoned sessions
#     during normal operation; this 48h check is just what keeps a
#     restored snapshot from ever reviving something that old.
#   - _sessions_stale_sweep_job additionally re-runs that same 48h check
#     against the LIVE in-memory dicts every 48h, as an independent
#     backstop in case _cleanup_stale_sessions_job's hourly job was ever
#     down for an extended stretch (e.g. a JobQueue outage) — it only
#     removes entries already past 48h old, never active sessions, so it
#     can't interrupt anyone mid-quiz.
# ═══════════════════════════════════════════════════════════════
SESSIONS_FILE                = "sessions.json"
SESSIONS_BACKUP_MARKER       = "🧩 QUIZICIAN_SESSIONS_BACKUP"
SESSIONS_BACKUP_MIN_INTERVAL = 30          # seconds
SESSIONS_MAX_AGE_SECONDS     = 48 * 3600   # 48 hours

def _sessions_snapshot() -> dict:
    """A plain-dict, JSON-safe snapshot of all three session stores.
    Int keys (user_id, and poll_status_by_mid's message_id) become
    strings here — see _restore_sessions_dict for the reverse. Shallow
    per-session copies only (not a deep copy of the whole store): each
    session dict is replaced wholesale by its owning function rather than
    mutated field-by-field across an await, so this is safe against the
    same kind of mid-serialization mutation save_analytics's comment
    warns about — there's no in-place list/dict mutation left exposed
    once a session is captured here except poll_status_by_mid, which is
    built once at session start and never mutated afterward."""
    def _clean(sessions: dict) -> dict:
        out = {}
        for uid, session in sessions.items():
            s = dict(session)
            if "poll_status_by_mid" in s:
                s["poll_status_by_mid"] = {str(k): v for k, v in s["poll_status_by_mid"].items()}
            if "sr_asked" in s:
                # Spaced-repetition tracking (see _maybe_deliver_spaced_
                # repetition) — a live set, not JSON-safe as-is. json.dumps
                # raises TypeError on a bare set with no try/except around
                # it anywhere in this chain (_flush_sessions_if_changed /
                # backup_sessions_to_channel), so this isn't just cosmetic:
                # left unconverted, every tick after the first lecture
                # session with a spaced-repetition miss would throw here
                # and silently break session persistence entirely for
                # everyone, not just that one user — sort() keeps it
                # deterministic in the change-detection JSON (see
                # _last_sessions_snapshot_json above).
                s["sr_asked"] = sorted(s["sr_asked"])
            out[str(uid)] = s
        return out
    return {
        "lecture_sessions":         _clean(LECTURE_SESSIONS),
        "daily_quiz_sessions":      _clean(DAILY_QUIZ_SESSIONS),
        "mistakes_retake_sessions": _clean(MISTAKES_RETAKE_SESSIONS),
        # Shared per-day Daily Quiz question sets — see the "Per-day,
        # per-year shared Daily Quiz questions" section above for why
        # these ride along in the same snapshot/backup as the sessions.
        "daily_quiz_questions_date": _DAILY_QUIZ_QUESTIONS_DATE,
        "daily_quiz_questions":      _DAILY_QUIZ_QUESTIONS,
    }

def _session_age_seconds(session: dict) -> float:
    """Best-effort age for the 48h checks above. Lecture sessions have
    started_at; Daily Quiz / mistakes-retake sessions don't, so this
    falls back to current_delivered_at (when the in-flight question was
    sent). If neither is present, treat it as already-ancient rather
    than immortal, so a malformed entry can never survive indefinitely."""
    anchor = session.get("started_at") or session.get("current_delivered_at")
    if anchor is None:
        return SESSIONS_MAX_AGE_SECONDS + 1
    return time.time() - anchor

_last_sessions_snapshot_json: str | None = None   # change-detection only, never persisted itself

async def _flush_sessions_if_changed() -> None:
    """Writes sessions.json locally only if the snapshot actually changed
    since the last tick. Called every SESSIONS_BACKUP_MIN_INTERVAL seconds
    by _sessions_backup_job, and once more on a clean shutdown."""
    global _last_sessions_snapshot_json
    snapshot = _sessions_snapshot()
    as_json  = json.dumps(snapshot, sort_keys=True)
    if as_json == _last_sessions_snapshot_json:
        return
    _last_sessions_snapshot_json = as_json
    await asyncio.to_thread(_atomic_write_json, SESSIONS_FILE, snapshot, indent=2, ensure_ascii=False)

def _restore_sessions_dict(raw: dict) -> None:
    """Populates LECTURE_SESSIONS/DAILY_QUIZ_SESSIONS/MISTAKES_RETAKE_SESSIONS
    in place from a loaded snapshot (channel backup or local file),
    dropping anything already past SESSIONS_MAX_AGE_SECONDS. Also
    restores the shared per-day Daily Quiz question cache (see the
    "Per-day, per-year shared Daily Quiz questions" section) — loaded
    as-is, with no date check here, since _ensure_daily_quiz_questions_fresh
    already discards it the next time it's read if the date has since
    rolled over."""
    global _DAILY_QUIZ_QUESTIONS_DATE, _DAILY_QUIZ_QUESTIONS
    targets = {
        "lecture_sessions":         LECTURE_SESSIONS,
        "daily_quiz_sessions":      DAILY_QUIZ_SESSIONS,
        "mistakes_retake_sessions": MISTAKES_RETAKE_SESSIONS,
    }
    restored, dropped_stale = 0, 0
    for key, target in targets.items():
        target.clear()
        for uid_str, session in raw.get(key, {}).items():
            if _session_age_seconds(session) > SESSIONS_MAX_AGE_SECONDS:
                dropped_stale += 1
                continue
            if "poll_status_by_mid" in session:
                session["poll_status_by_mid"] = {int(k): v for k, v in session["poll_status_by_mid"].items()}
            if "sr_asked" in session:
                session["sr_asked"] = set(session["sr_asked"])   # reverse of the sorted-list conversion in _sessions_snapshot's _clean
            target[int(uid_str)] = session
            restored += 1
    _DAILY_QUIZ_QUESTIONS_DATE = raw.get("daily_quiz_questions_date")
    _DAILY_QUIZ_QUESTIONS      = raw.get("daily_quiz_questions") or {}
    print(f"Restored {restored} session(s) ({dropped_stale} dropped as stale), "
          f"daily quiz questions for {len(_DAILY_QUIZ_QUESTIONS)} year(s) dated {_DAILY_QUIZ_QUESTIONS_DATE}.")

def load_sessions() -> dict | None:
    if os.path.exists(SESSIONS_FILE):
        with open(SESSIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None

_sessions_backup_msg_id: int | None = None
_last_sessions_backup_at: float = 0.0
_last_sessions_backup_snapshot_json: str | None = None   # what was last actually uploaded — separate
                                                           # from _last_sessions_snapshot_json (local-disk
                                                           # flush's own change-detection), since the two
                                                           # run on different schedules/call sites

async def backup_sessions_to_channel(context, force: bool = False) -> None:
    """Same shape as every other backup_*_to_channel, but throttled AND —
    unlike the others — deletes the previous pinned backup instead of
    keeping it forever. See the section banner above for why.

    Also skips the upload entirely when the snapshot is identical to the
    last one actually sent — most ticks land between quizzes with nobody
    mid-session, so this turns "upload every ~30s no matter what" into
    "upload only when session state actually moved." force=True bypasses
    both this check and the throttle; used by the reconcile job, which
    calls this specifically because the channel's pin is missing or wrong
    and needs a fresh upload regardless of whether anything changed."""
    global _sessions_backup_msg_id, _last_sessions_backup_at, _last_sessions_backup_snapshot_json
    if not SESSIONS_GROUP_ID:
        return
    if not RESTORE_OK.get("sessions", True):
        print("SESSIONS BACKUP SKIPPED — last restore failed, refusing to overwrite the channel backup.")
        return
    now = time.monotonic()
    if not force and now - _last_sessions_backup_at < SESSIONS_BACKUP_MIN_INTERVAL:
        return
    snapshot = _sessions_snapshot()
    as_json  = json.dumps(snapshot, sort_keys=True)
    if not force and as_json == _last_sessions_backup_snapshot_json:
        return   # nothing changed since the last successful upload
    _last_sessions_backup_at = now
    data = json.dumps(snapshot, indent=2, ensure_ascii=False).encode("utf-8")
    try:
        sent = await context.bot.send_document(
            chat_id=SESSIONS_GROUP_ID,
            document=InputFile(BytesIO(data), filename=_backup_filename("sessions.json")),
            caption=SESSIONS_BACKUP_MARKER,
        )
    except Exception as e:
        print("SESSIONS BACKUP ERROR:", e)
        return
    try:
        await context.bot.pin_chat_message(
            chat_id=SESSIONS_GROUP_ID, message_id=sent.message_id, disable_notification=True,
        )
    except Exception as e:
        print("SESSIONS PIN ERROR:", e)
    _last_sessions_backup_snapshot_json = as_json
    old_msg_id = _sessions_backup_msg_id
    _sessions_backup_msg_id = sent.message_id
    if old_msg_id and old_msg_id != sent.message_id:
        try:
            await context.bot.delete_message(chat_id=SESSIONS_GROUP_ID, message_id=old_msg_id)
        except Exception:
            pass   # already gone, too old to delete, etc. — fine either way, next tick re-syncs

async def restore_sessions_from_channel(app) -> None:
    global _sessions_backup_msg_id
    if not SESSIONS_GROUP_ID:
        return

    async def _do():
        global _sessions_backup_msg_id, _last_sessions_backup_snapshot_json
        chat   = await app.bot.get_chat(SESSIONS_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document or (pinned.caption or "") != SESSIONS_BACKUP_MARKER:
            return
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        restored = json.loads(bytes(raw).decode("utf-8"))
        _restore_sessions_dict(restored)
        await _flush_sessions_if_changed()
        _last_sessions_backup_snapshot_json = json.dumps(_sessions_snapshot(), sort_keys=True)
        _sessions_backup_msg_id = pinned.message_id

    await _run_restore_with_retries(app, "sessions", "Sessions", _do)

async def _sessions_backup_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic tick, registered alongside every other job in _post_init:
    flushes sessions.json locally if changed, then pushes the channel
    backup (itself separately throttled to SESSIONS_BACKUP_MIN_INTERVAL)."""
    await _flush_sessions_if_changed()
    await backup_sessions_to_channel(context)

SESSIONS_STALE_SWEEP_INTERVAL = 48 * 3600  # seconds

async def _sessions_stale_sweep_job(context: ContextTypes.DEFAULT_TYPE):
    """Independent backstop alongside _cleanup_stale_sessions_job's normal
    6h idle-based cleanup — see the section banner above. Only removes
    entries already older than SESSIONS_MAX_AGE_SECONDS; never touches an
    active session, so this can't interrupt anyone mid-quiz."""
    removed = 0
    for sessions in (LECTURE_SESSIONS, DAILY_QUIZ_SESSIONS, MISTAKES_RETAKE_SESSIONS):
        for user_id, session in list(sessions.items()):
            if _session_age_seconds(session) > SESSIONS_MAX_AGE_SECONDS:
                sessions.pop(user_id, None)
                removed += 1
    if removed:
        print(f"SESSIONS — 48h stale sweep removed {removed} session(s).")
        await _flush_sessions_if_changed()
        await backup_sessions_to_channel(context)

# ═══════════════════════════════════════════════════════════════
# QUESTION TIMEOUT — unsticking a timed quiz nobody's answering
# ═══════════════════════════════════════════════════════════════
# Auto-next delivery (Daily Quiz / lecture-auto / mistakes-retake /
# wrong-answer-retake — anywhere a single question is in flight at a
# time via session["current_poll_id"]) only ever advances from
# handle_poll_answer, which only fires when the user actually taps an
# option. A timed question's open_period makes Telegram auto-close the
# poll once it expires, but Telegram never tells the bot "nobody
# answered" — so with nothing else in place, a person who lets a timer
# run out just leaves their own quiz stuck forever, waiting on a
# poll_answer update that's never coming.
#
# The fix: _schedule_question_timeout books a one-off job a couple of
# seconds after the question's own open_period should have elapsed. If
# it's still the question the session is waiting on when that job fires
# (the person hasn't answered — handle_poll_answer would have moved
# current_poll_id on if they had), _handle_question_timeout treats it as
# a miss: the first one in a row is skipped exactly like a wrong answer
# and the quiz carries on as normal. A SECOND miss in a row instead
# pauses the session (rather than silently auto-skipping through
# whatever's left) and asks the person whether to resume or abandon it —
# see the qresume:/qabandon: handlers in button_handler.
#
# Batch-mode lecture sessions (every question sent up front, answerable
# in any order) have no single "current" question to time out this way,
# so this deliberately does nothing there — see the mode=="batch" check
# below.
QUESTION_TIMEOUT_GRACE_SECONDS = 2   # buffer past the timer's own open_period, so we're never racing an answer landing right as Telegram auto-closes the poll

_SESSION_STORE_BY_KIND = {
    "daily":   DAILY_QUIZ_SESSIONS,
    "retake":  MISTAKES_RETAKE_SESSIONS,
    "lecture": LECTURE_SESSIONS,
}
_QUIZ_KIND_LABEL = {"daily": "Daily Quiz", "retake": "Retake", "lecture": "Lecture"}

def _schedule_question_timeout(context: ContextTypes.DEFAULT_TYPE, kind: str, user_id: int, poll_id: str, timer_seconds: int | None) -> None:
    """Call right after sending a timed poll as part of single-question
    auto-next delivery. No-op if the question isn't timed (timer_seconds
    falsy) or there's no JobQueue to schedule against."""
    if not timer_seconds or context.job_queue is None:
        return
    context.job_queue.run_once(
        _question_timeout_job, when=timer_seconds + QUESTION_TIMEOUT_GRACE_SECONDS,
        data={"kind": kind, "user_id": user_id, "poll_id": poll_id},
        name=f"qtimeout:{kind}:{user_id}:{poll_id}",
    )

async def _question_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data
    await _handle_question_timeout(context, d["kind"], d["user_id"], d["poll_id"])

async def _advance_session_for_kind(context: ContextTypes.DEFAULT_TYPE, kind: str, user_id: int, session: dict, is_correct: bool) -> None:
    """Dispatches to the right advance function for this session's kind,
    reading the pending question's own message_id/mid/delivered_at
    straight off the session — the same way handle_poll_answer's three
    branches already do for a real answer, just with is_correct forced
    by the caller (used here to skip a timed-out question as wrong)."""
    message_id   = session.get("current_message_id")
    delivered_at = session.get("current_delivered_at")
    if kind == "daily":
        await _advance_daily_quiz_session(context, user_id, session, is_correct, message_id, delivered_at)
    elif kind == "retake":
        await _advance_mistakes_retake_session(context, user_id, session, is_correct, message_id, delivered_at)
    elif kind == "lecture":
        await _advance_lecture_session(context, user_id, session, is_correct, message_id, session.get("current_mid"), delivered_at)

async def _handle_question_timeout(context: ContextTypes.DEFAULT_TYPE, kind: str, user_id: int, poll_id: str) -> None:
    store   = _SESSION_STORE_BY_KIND.get(kind)
    session = store.get(user_id) if store is not None else None
    if not session or session.get("current_poll_id") != poll_id:
        return   # already answered, session finished/replaced/abandoned, or a stale job surviving a restart
    if session.get("mode") == "batch":
        return   # no single "current" question to time out in batch mode — see section banner above

    session["timeout_streak"] = session.get("timeout_streak", 0) + 1
    if session["timeout_streak"] == 1:
        await _advance_session_for_kind(context, kind, user_id, session, is_correct=False)
        return

    # Second consecutive miss — pause instead of skipping again, so an
    # absent user doesn't just get auto-skipped through their whole quiz
    # unattended.
    session["paused"] = True
    label = _QUIZ_KIND_LABEL.get(kind, "Quiz")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="It seems you have stopped quizzing, do wish to abandon the current session? (Quizzy will be sad)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"▶️ Resume {label}", callback_data=f"qresume:{kind}")],
                [InlineKeyboardButton(f"🥀 Abandon {label}", callback_data=f"qabandon:{kind}")],
            ]),
        )
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
MAX_QUESTIONS_PER_MSG = 50
TELEGRAM_Q_LIMIT      = 300   # max chars in poll question field
TELEGRAM_DESC_LIMIT   = 200   # max chars in poll description (shown above question)
TELEGRAM_EX_LIMIT     = 200   # max chars in poll explanation (shown after answering)

# ═══════════════════════════════════════════════════════════════
# PER-USER SERIALIZATION
#
# Now that concurrent_updates() lets updates from different users run at
# the same time (see the ApplicationBuilder call near the bottom of this
# file), two updates from the SAME user in quick succession (a fast
# double-tap, or a poll-answer racing a button tap) could otherwise
# interleave mid-handler and corrupt shared per-user state — e.g. two
# coroutines both reading LECTURE_SESSIONS[user_id], each unaware the
# other is also about to mutate it, with real await points (Telegram API
# calls, disk writes) in between the read and the write.
#
# @_serialize_per_user fixes this without touching either handler's body:
# it runs everything from the same user_id through a private asyncio.Lock,
# so a user's own updates are still handled one-at-a-time, in order — but
# different users remain fully concurrent with each other, which is what
# actually matters at 500+ simultaneous users.
# ═══════════════════════════════════════════════════════════════
_user_locks: dict[int, asyncio.Lock] = {}

def _get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock

def _serialize_per_user(handler):
    """Decorator for update handlers: serializes concurrent updates from
    the same user_id through a per-user lock. No-ops (calls straight
    through) if the update has no identifiable user."""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user is None:
            return await handler(update, context, *args, **kwargs)
        async with _get_user_lock(user.id):
            return await handler(update, context, *args, **kwargs)
    return wrapper

# ═══════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════
def clean_option(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^[A-Ea-e1-5][).\-]\s*", "", line)
    line = re.sub(r"^[-•]\s*", "", line)
    return line.strip()

def strip_leading_letter_prefix(option: str) -> str:
    return re.sub(r"^[A-Ea-e]\)\s*", "", option).strip()

_MCQ_OPTION_PREFIX_RE = re.compile(r"^[A-Ea-e1-5][).\-]\s*")

def _looks_like_mcq_attempt(lines: list) -> bool:
    """True if at least one line looks like someone attempting an MCQ
    option (starts with a "a)"/"b)"/"1." style prefix — same pattern
    clean_option() strips), even though the block as a whole fell short
    of the 3+ lines normalize_mcq_block needs to treat it as a real
    question. Used to tell an ordinary chat message ("مساء الخير", "شكرا")
    apart from a genuine-but-broken question attempt — only the second
    case gets the "wrong format!" warning; see its call site."""
    return any(_MCQ_OPTION_PREFIX_RE.match(l) for l in lines)

def normalize_mcq_block(block: str):
    block = block.strip()
    if "\n" in block:
        return [l.strip() for l in block.split("\n") if l.strip()]
    match = re.search(r"\b([A-Ea-e1-5])[).]", block)
    if not match:
        return [block]
    question     = block[:match.start()].strip()
    options_part = block[match.start():]
    parts = re.split(r"(?=\b[A-Ea-e1-5][).])", options_part)
    return [question] + [p.strip() for p in parts if p.strip()]

def strip_spoiler_markers(text: str) -> str:
    return re.sub(r"\|\|(.+?)\|\|", r"\1", text, flags=re.DOTALL)

def parse_mcq_lines(lines: list):
    """
    Given already-normalized MCQ lines (question line + option lines),
    extracts (question, raw_options, correct_index, explanation).
    correct_index is None if no option was marked correct.
    Shared by the text handler and the image-caption parser so the
    MCQ grammar only lives in one place.
    """
    question      = lines[0]
    options       = []
    correct_index = None
    explanation   = None

    for line in lines[1:]:
        ex_match = re.match(r"^ex:\s*(.+)", line, re.IGNORECASE)
        if ex_match:
            explanation = ex_match.group(1).strip()
            continue

        opt       = clean_option(line)
        has_z_end = re.search(r"\s+[zZ]\s*$", opt)
        has_check = "✅" in opt

        if has_z_end or has_check:
            opt           = opt.replace("✅", "")
            opt           = re.sub(r"\s+[zZ]\s*$", "", opt).strip()
            correct_index = len(options)

        if opt:
            options.append(opt)

    return question, options, correct_index, explanation

def parse_mcq_block(block: str):
    """
    Full validation on top of parse_mcq_lines: returns
    (question, raw_options, correct_index, explanation) only if the block
    is a COMPLETE, valid MCQ (>=3 lines, a correct answer marked).
    Returns None otherwise. Used to detect a fully-formed question in an
    image caption so we don't need to ask the user to resend it.
    """
    lines = normalize_mcq_block(block.strip())
    if len(lines) < 3:
        return None
    question, options, correct_index, explanation = parse_mcq_lines(lines)
    if correct_index is None or correct_index >= len(options):
        return None
    return question, options, correct_index, explanation

def parse_written_strict(block: str):
    block = strip_spoiler_markers(block)
    lines = [l.rstrip() for l in block.split("\n") if l.strip()]
    if len(lines) < 2:
        return None
    title   = lines[0]
    content = "\n".join(lines[1:]).strip()
    if content.startswith(".") and content.endswith("."):
        return title, content[1:-1].strip()
    return None

def split_question_for_telegram(question: str):
    """
    Returns (main_q, description_overflow) where:
    - main_q fits in TELEGRAM_Q_LIMIT
    - description_overflow goes into the 'description' field (shown above question)
      and is capped at TELEGRAM_DESC_LIMIT
    If question fits in Q_LIMIT, description_overflow is None.
    """
    if len(question) <= TELEGRAM_Q_LIMIT:
        return question, None
    # Try to split at a sentence boundary
    cutoff    = TELEGRAM_Q_LIMIT - 3
    split_pos = question.rfind(". ", 0, cutoff)
    if split_pos == -1:
        split_pos = question.rfind(" ", 0, cutoff)
    if split_pos == -1:
        split_pos = cutoff
    main     = question[:split_pos].strip() + "…"
    overflow = "…" + question[split_pos:].strip()
    # Cap overflow to TELEGRAM_DESC_LIMIT
    if len(overflow) > TELEGRAM_DESC_LIMIT:
        overflow = overflow[:TELEGRAM_DESC_LIMIT - 1] + "…"
    return main, overflow

def _prefixed_question(question: str, prefix: str) -> str:
    """Prepends prefix to a question, truncating the question itself
    (never the prefix) if the combination would exceed Telegram's poll
    question limit — shared by _numbered_question (quiz sequence
    numbers) and the spaced-repetition re-ask's '🔁 Review:' tag."""
    if len(prefix) + len(question) <= TELEGRAM_Q_LIMIT:
        return prefix + question
    return prefix + question[:TELEGRAM_Q_LIMIT - len(prefix) - 1].rstrip() + "…"

def _numbered_question(question: str, number: int) -> str:
    """Prefixes a question with its 1-based position in the quiz it's
    being delivered as part of ('1) ...', '2) ...') — see
    _deliver_next_daily_question / _deliver_next_lecture_question, which
    number every question of every quiz (Daily Quiz, lecture, mistakes-
    bank retake, wrong-answer retake) this way via a per-session
    delivered_count counter. Truncates the question itself (never the
    prefix) if the combination would exceed Telegram's poll question
    limit — only ever triggers for a question already sitting right at
    that cap, since the prefix only adds a few characters."""
    return _prefixed_question(question, f"{number}) ")

def options_too_long(options: list) -> bool:
    """Check if any single option exceeds Telegram's 100-char option limit."""
    return any(len(o) > 100 for o in options)

def make_letter_only_options(count: int) -> list:
    """Return ['A', 'B', 'C', ...] for poll when answers are too long."""
    return [string.ascii_uppercase[i] for i in range(count)]

def _clear_pending_image(user_id: int):
    """Drop any image that's still waiting for a question, deleting its file."""
    path = PENDING_IMAGE.pop(user_id, None)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════
# QUIZ DELIVERY  (single source of truth for sending a live quiz poll)
# ═══════════════════════════════════════════════════════════════
async def _send_quiz_poll(context, poll_kwargs: dict, image_path: str = None):
    """
    Sends the poll, attaching image_path as the quiz's native media (Bot API
    10.0+ InputPollMedia) when provided. Falls back to sending the image as a
    separate message + a media-less poll if the media attachment is ever
    rejected — this feature is new enough (May 2026) that we don't want a
    server-side quirk to silently drop the question entirely.
    """
    if image_path:
        try:
            with open(image_path, "rb") as f:
                await context.bot.send_poll(**poll_kwargs, media=InputMediaPhoto(f))
            return
        except Exception as e:
            print("POLL MEDIA ERROR (falling back to separate image message):", e)
            with open(image_path, "rb") as f:
                await context.bot.send_photo(chat_id=poll_kwargs["chat_id"], photo=f)
    await context.bot.send_poll(**poll_kwargs)

async def deliver_quiz(
    context, chat_id: int, question: str, raw_options: list, correct_index: int,
    explanation: str = None, image_path: str = None,
    always_show_question_text: bool = False, header_label: str = "📋 <b>السؤال:</b>",
):
    """
    Sends a single live quiz poll to chat_id, handling Telegram's field-length
    limits consistently (question <=300, options <=100, explanation <=200).
    If image_path is given, it's attached as the quiz's native photo
    attachment (Bot API 10.0+), so it shows up inside the quiz itself.

    always_show_question_text=True forces the original question text to be
    shown as a message even when it fits inside the poll's question field —
    used for forwarded quizzes so the original wording is always visible.

    This is the ONLY place that builds/sends quiz polls, so forwarded
    polls, typed MCQs, and image-paired MCQs all share one code path.
    """
    labeled_options = [
        f"{string.ascii_uppercase[i]}) {opt}" for i, opt in enumerate(raw_options)
    ]
    q_fits      = len(question) <= TELEGRAM_Q_LIMIT
    answers_fit = not options_too_long(labeled_options)

    # chat_id is always the user's own DM here, so it doubles as their user_id.
    timer_seconds = get_question_timer_seconds(chat_id)
    open_period   = timer_seconds if timer_seconds else None

    if q_fits and answers_fit:
        main_q, desc_overflow = split_question_for_telegram(question)

        if always_show_question_text:
            await context.bot.send_message(
                chat_id=chat_id, text=f"{header_label}\n{question}", parse_mode=ParseMode.HTML,
            )

        if desc_overflow:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📋 <b>تكملة السؤال:</b>\n{desc_overflow}",
                parse_mode=ParseMode.HTML,
            )

        poll_kwargs = dict(
            chat_id=chat_id, question=main_q, options=labeled_options,
            type="quiz", correct_option_id=correct_index, is_anonymous=True,
            open_period=open_period,
        )
        if explanation:
            poll_kwargs["explanation"] = explanation[:TELEGRAM_EX_LIMIT]
        await _send_quiz_poll(context, poll_kwargs, image_path)

    elif not q_fits and answers_fit:
        await context.bot.send_message(
            chat_id=chat_id, text=f"{header_label}\n{question}", parse_mode=ParseMode.HTML,
        )

        poll_kwargs = dict(
            chat_id=chat_id, question=".", options=labeled_options,
            type="quiz", correct_option_id=correct_index, is_anonymous=True,
            open_period=open_period,
        )
        if explanation:
            poll_kwargs["explanation"] = explanation[:TELEGRAM_EX_LIMIT]
        await _send_quiz_poll(context, poll_kwargs, image_path)

    else:
        answer_lines = "\n".join(
            f"{'✅ ' if i == correct_index else ''}{string.ascii_uppercase[i]}) {opt}"
            for i, opt in enumerate(raw_options)
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{header_label}\n{question}\n\n<b>الإجابات:</b>\n{answer_lines}",
            parse_mode=ParseMode.HTML,
        )

        letter_opts = make_letter_only_options(len(raw_options))
        poll_kwargs = dict(
            chat_id=chat_id, question=".", options=letter_opts,
            type="quiz", correct_option_id=correct_index, is_anonymous=True,
            open_period=open_period,
        )
        if explanation:
            poll_kwargs["explanation"] = explanation[:TELEGRAM_EX_LIMIT]
        await _send_quiz_poll(context, poll_kwargs, image_path)

# ═══════════════════════════════════════════════════════════════
# KEYBOARD HELPERS
# ═══════════════════════════════════════════════════════════════
def start_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🦦 How To Use", callback_data="menu_how"),
            InlineKeyboardButton("Quizzes ⁉️",    callback_data="menu_quizzes"),
        ],
        [
            InlineKeyboardButton("📊 My Stats", callback_data="menu_mystats"),
            InlineKeyboardButton("⚙️ Settings",  callback_data="menu_settings"),
        ],
        [
            InlineKeyboardButton("💥Daily Quiz💥", callback_data="daily_quiz"),
        ],
        [
            InlineKeyboardButton("🧠 Mistakes Bank", callback_data="mistakes_bank_menu"),
        ],
        [
            InlineKeyboardButton("🏆 Leaderboard", callback_data="year_leaderboard"),
        ],
    ])

def settings_menu_keyboard(user_id: int, page: int = 1) -> InlineKeyboardMarkup:
    def _tag(on: bool) -> str:
        return "🟢 On" if on else "🔴 Off"

    if page == 2:
        reactions    = get_reactions_enabled(user_id)
        ach_notifs   = get_achievement_notifs_enabled(user_id)
        daily_notifs = get_daily_notifs_enabled(user_id)
        zikr         = get_zikr_enabled(user_id)
        rows = [
            [InlineKeyboardButton(f"🎭 Reactions: {_tag(reactions)}", callback_data="toggle_reactions")],
            [InlineKeyboardButton(f"🏆 Achievement Alerts: {_tag(ach_notifs)}", callback_data="toggle_achievement_notifs")],
            [InlineKeyboardButton(f"🔔 Daily Notification: {_tag(daily_notifs)}", callback_data="toggle_daily_notifs")],
            [InlineKeyboardButton(f"📿 Hourly Zikr: {_tag(zikr)}", callback_data="toggle_zikr")],
            [InlineKeyboardButton("⬅️ Back", callback_data="settings_page:1")],
            [InlineKeyboardButton("🏠 Back to Home", callback_data="back_home")],
        ]
        return InlineKeyboardMarkup(rows)

    auto_next  = get_auto_next_enabled(user_id)
    randomize  = get_randomize_enabled(user_id)
    spaced_rep = get_spaced_repetition_enabled(user_id)
    timer      = get_question_timer_seconds(user_id)
    timer_tag  = "🔴 Off" if timer == 0 else f"🟢 {timer}s"
    rows = [
        [InlineKeyboardButton("✏️ Edit Nickname", callback_data="edit_nickname")],
        [InlineKeyboardButton(f"⏭️ Auto-Next: {_tag(auto_next)}", callback_data="toggle_auto_next")],
        [InlineKeyboardButton(f"🔀 Randomize: {_tag(randomize)}", callback_data="toggle_randomize")],
        [InlineKeyboardButton(f"🔁 Spaced Repetition: {_tag(spaced_rep)}", callback_data="toggle_spaced_repetition")],
        [InlineKeyboardButton(f"⏱️ Question Timer: {timer_tag}", callback_data="toggle_question_timer")],
        [InlineKeyboardButton("🗑 Clear Mistake Bank", callback_data="clear_mistakes_bank_ask")],
        [InlineKeyboardButton("➡️ More Settings", callback_data="settings_page:2")],
        [InlineKeyboardButton("🏠 Back to Home",  callback_data="back_home")],
    ]
    return InlineKeyboardMarkup(rows)

# ═══════════════════════════════════════════════════════════════
# MENU TEXT CONTENT
# ═══════════════════════════════════════════════════════════════
HOW_TO_USE_TEXT = (
    "🦦 <b>ازاي تستخدم Quizician؟</b>\n\n"
    "<b>📝 Quizzes ⁉️</b>\n"
    "اختار السنة، بعدين الموديول، بعدين المادة، وبعدين المحاضرة — وابدأ تجاوب. "
    "كل سؤال بيتصحح على طول، وبتاخد XP على كل إجابة صح.\n\n"
    "<b>💥 Daily Quiz</b>\n"
    "10 أسئلة عشوائية من منهج سنتك، بتتجدد كل يوم الساعة 2 الضهر. "
    "أول مرة تستخدمه هيطلب منك تحدد سنتك/فرقتك — ومحاولة واحدة بس في اليوم.\n\n"
    "<b>🧠 Mistakes Bank</b>\n"
    "أي سؤال تغلط فيه بيتسجل هنا تلقائي، عشان ترجعله وتراجعه تاني وقت ما تحب.\n\n"
    "<b>📊 My Stats</b>\n"
    "شوف الـ XP والـ Level بتاعك، عدد الأسئلة الصح والغلط، والـ achievements اللي فتحتها.\n\n"
    "<b>🏆 Leaderboard</b>\n"
    "ترتيبك بين زمايلك في نفس السنة/الفرقة، حسب عدد الإجابات الصح ونسبة الدقة.\n\n"
    "<b>⚙️ Settings</b>\n"
    "غيّر اسمك المستعار، سنتك/فرقتك، أو شغّل/قفّل حاجات زي الـ Auto-Next، "
    "الـ Randomize، تذكير الـ Daily Quiz، وتذكير الزكر كل ساعة.\n\n"
    "😴 /sleep — يوقف تفاعل البوت مؤقتًا لحد ما تبعت /start تاني\n"
    "🆘 /report_issue — لو فيه مشكلة أو سؤال غلط، ابعتلنا بلاغ"
)

# ═══════════════════════════════════════════════════════════════
# REACTIONS
# ═══════════════════════════════════════════════════════════════
async def react_random(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else update.effective_chat.id
    if not get_reactions_enabled(user_id):
        return
    try:
        roll  = random.randint(1, 20)
        emoji = "🫡" if roll <= 15 else "❤️" if roll <= 19 else "🏆"
        await context.bot.set_message_reaction(
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            reaction=[ReactionTypeEmoji(emoji)],
            is_big=False,
        )
    except Exception:
        pass

async def _react_to_lecture_answer(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, message_id: int | None,
    is_correct: bool, new_streak: int, streak_broken: bool,
) -> None:
    """Quizzy's reaction to a single lecture-quiz poll answer, based on
    correctness and the user's current lecture correct-streak:
      - correct, streak > 15  → 🏆
      - correct, streak > 10  → 😍
      - correct, streak > 5   → ❤️‍🔥
      - correct, otherwise    → ❤️
      - wrong, broke a streak → 💔
      - wrong, no streak lost → 😢
    Respects the Reactions setting and no-ops if there's no message to
    react to (e.g. the poll message couldn't be sent/found)."""
    if message_id is None or not get_reactions_enabled(user_id):
        return
    if is_correct:
        if new_streak > 15:
            emoji = "🏆"
        elif new_streak > 10:
            emoji = "😍"
        elif new_streak > 5:
            emoji = "❤️‍🔥"
        else:
            emoji = "❤️"
    else:
        emoji = "💔" if streak_broken else "😢"
    try:
        await context.bot.set_message_reaction(
            chat_id=user_id,
            message_id=message_id,
            reaction=[ReactionTypeEmoji(emoji)],
            is_big=False,
        )
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
# SLEEP / WAKE COMMANDS
# ═══════════════════════════════════════════════════════════════
async def sleep_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    SLEEPING.add(user_id)
    await update.message.reply_text(
        f"{quizzy_block(QUIZZY_SLEEPING_ART, 'قوزي نام، وأنا نايم معاه 😴')}\n\n"
        "نادينا بـ /start لما تحتاجنا تاني",
        parse_mode=ParseMode.HTML,
    )

# ═══════════════════════════════════════════════════════════════
# PASSIVE ANSWER BACKFILL
# Telegram pushes a fresh Update.poll (with correct_option_ids filled in)
# to any bot that has previously seen a poll, once that poll is stopped —
# even for polls the bot didn't create. If the original quiz's creator
# later ends it, we quietly backfill the answer with no user action needed.
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# QUIZ POLL TIMEOUT — a session used to get stuck forever if a poll's
# open_period (the Settings question-timer) expired with nobody voting.
#
# Root cause: session progression only ever happened inside
# handle_poll_answer, which fires on a PollAnswer update — and Telegram
# ONLY sends PollAnswer when someone actually votes. If the timer runs
# out unanswered, Telegram still sends an Update.poll (is_closed=True —
# same shape as any other poll closing) but NEVER a PollAnswer for it,
# since nobody voted. Nothing was listening for that case, so the
# session just sat there permanently waiting for an answer that could
# never arrive.
#
# Fix: poll_update_handler (already registered for every Update.poll)
# now also checks, on every closed poll, whether it's still the
# in-flight question for a Daily Quiz / Mistakes Retake / lecture
# session — i.e. nothing has advanced that session past it yet, which
# is only possible if no PollAnswer ever came in for it. If so, treats
# it exactly like an incorrect answer and advances the session, same
# as handle_poll_answer would for any other wrong answer.
#
# If the user DID answer in time, handle_poll_answer has already
# advanced the session (cleared current_poll_id / popped it out of
# pending_polls) well before this ever runs, so the match below simply
# fails and this is a no-op — it only ever fires for a genuine timeout.
# ═══════════════════════════════════════════════════════════════
async def _handle_quiz_poll_timeout(context: ContextTypes.DEFAULT_TYPE, poll) -> None:
    poll_id = poll.id

    for user_id, session in list(DAILY_QUIZ_SESSIONS.items()):
        if session.get("current_poll_id") == poll_id:
            await _advance_daily_quiz_session(
                context, user_id, session, False, session.get("current_message_id"),
                session.get("current_delivered_at"),
            )
            return

    for user_id, session in list(MISTAKES_RETAKE_SESSIONS.items()):
        if session.get("current_poll_id") == poll_id:
            await _advance_mistakes_retake_session(
                context, user_id, session, False, session.get("current_message_id"),
                session.get("current_delivered_at"),
            )
            return

    for user_id, session in list(LECTURE_SESSIONS.items()):
        if session.get("mode") == "batch":
            pending = session.get("pending_polls", {})
            if poll_id in pending:
                _, message_id, mid, delivered_at = pending.pop(poll_id)
                await _advance_lecture_session(context, user_id, session, False, message_id, mid, delivered_at)
                return
        elif session.get("current_poll_id") == poll_id:
            await _advance_lecture_session(
                context, user_id, session, False,
                session.get("current_message_id"), session.get("current_mid"),
                session.get("current_delivered_at"),
            )
            return

async def _delayed_poll_timeout_check(context: ContextTypes.DEFAULT_TYPE, poll) -> None:
    """A short grace window before treating a closed poll as a genuine
    timeout. Without this, a vote cast in the same instant the timer
    expires — poll_answer and the closing Update.poll landing almost
    simultaneously, with no guaranteed order between them — could get
    double-counted: once here as a timeout, once for real once its
    poll_answer actually arrives. Waiting lets a same-instant real
    answer land and advance the session first, which makes the match
    in _handle_quiz_poll_timeout fail naturally, same as any other
    already-answered poll."""
    await asyncio.sleep(1.0)
    try:
        await _handle_quiz_poll_timeout(context, poll)
    except Exception as e:
        print(f"QUIZ POLL TIMEOUT: failed to advance a session for poll {poll.id}: {e}")

async def poll_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll = update.poll

    # Independent of everything else below (which is about the ORIGINAL
    # channel poll an admin uploaded) — this is about whichever poll
    # Telegram is telling us just closed, full stop, checking whether
    # it's a per-user delivered lecture/Daily-Quiz/retake question that
    # timed out unanswered. See the QUIZ POLL TIMEOUT section above.
    if poll is not None and poll.is_closed:
        asyncio.create_task(_delayed_poll_timeout_check(context, poll))

    # ── Quiz-channel poll tracking: mark it closed once stopped ──
    # poll.id is a Telegram-generated UUID, unique across all years, so a
    # linear check across each year's QUIZ_POLL_STATUS is safe/cheap.
    poll_year = None
    if poll is not None:
        poll_year = next((y for y in YEARS if poll.id in QUIZ_POLL_STATUS[y]), None)
    if poll_year is not None and poll.is_closed:
        entry = QUIZ_POLL_STATUS[poll_year][poll.id]
        if not entry["closed"]:
            entry["closed"] = True
            if poll.correct_option_ids and entry.get("correct_option_id") is None:
                entry["correct_option_id"] = poll.correct_option_ids[0]
            # Capture full poll content too, not just the correct answer —
            # lecture delivery needs to rebuild these as our own polls (see
            # _deliver_next_lecture_question) so it can get real poll_answer
            # events, since a straight copy of a channel poll stays
            # anonymous forever and Telegram never sends those for it.
            entry["question"]    = poll.question
            entry["options"]     = [o.text for o in poll.options]
            entry["explanation"] = poll.explanation
            await save_quiz_poll_status(poll_year)
            try:
                await context.bot.set_message_reaction(
                    chat_id=year_channel_id(poll_year), message_id=entry["message_id"],
                    reaction=[ReactionTypeEmoji("✅")], is_big=False,
                )
            except Exception:
                pass

# ── XP for lecture quiz answers ───────────────────────────────
XP_LECTURE_CORRECT        = 15   # per question answered correctly
XP_LECTURE_INCORRECT      = 5    # per question answered incorrectly
XP_LECTURE_COMPLETE_BONUS = 25   # extra, on top of the above, for the lecture's last question

async def _deliver_next_lecture_question(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict) -> bool:
    """Pops message ids off session['queue'] and sends them to the user one
    at a time as the bot's own non-anonymous quiz polls (built from content
    captured off the channel poll when it closed — see poll_update_handler),
    skipping (and cleaning up) any that were deleted from the channel since
    indexing, until one is delivered or the queue runs dry.

    Why not just copy_message the original? Because channel polls (and
    therefore any straight copy of one) are always anonymous, and Telegram
    never sends poll_answer for anonymous polls — there'd be no way to tell
    the question had been answered. Sending our own copy with
    is_anonymous=False sidesteps that entirely.

    For lecture questions closed before this content-capture existed, the
    QUIZ_POLL_STATUS entry won't have "question"/"options" yet — those get
    backfilled here via a one-time throwaway forward (read its poll
    content, delete it, never shown to the user) the first time they're
    delivered.

    Sets session['current_poll_id']. Returns whether a question went out."""
    year  = session["year"]
    entry = QUIZ_INDEX[year].get(session["lecture_key"])
    # Same index used by _advance_lecture_session for the mistakes-bank
    # lookup — scoped to this lecture's polls, built once at session start.
    # Falls back to a fresh year-wide scan only for sessions predating this
    # field (shouldn't happen once a bot restart has run, but keeps old
    # in-memory sessions from crashing rather than erroring).
    poll_status_by_mid = session.get("poll_status_by_mid")
    if poll_status_by_mid is None:
        poll_status_by_mid = {
            v["message_id"]: v
            for v in QUIZ_POLL_STATUS[year].values()
            if v["lecture"] == session["lecture_key"]
        }
        session["poll_status_by_mid"] = poll_status_by_mid

    async def _drop_dead(mid: int):
        if entry and mid in entry.get("ids", []):
            entry["ids"].remove(mid)
            if not entry["ids"]:
                QUIZ_INDEX[year].pop(session["lecture_key"], None)  # whole lecture was deleted
        dead_status = poll_status_by_mid.pop(mid, None)
        if dead_status is not None:
            for pid, v in list(QUIZ_POLL_STATUS[year].items()):
                if v is dead_status:
                    QUIZ_POLL_STATUS[year].pop(pid, None)
                    break
        else:
            # Not in our index (legacy session, or already gone) — fall back
            # to the direct scan rather than silently leaving a stale entry.
            for pid in [pid for pid, v in QUIZ_POLL_STATUS[year].items() if v["message_id"] == mid]:
                QUIZ_POLL_STATUS[year].pop(pid, None)
        await save_quiz_index(year)
        await save_quiz_poll_status(year)

    while session["queue"]:
        mid = session["queue"].pop(0)
        status = poll_status_by_mid.get(mid)

        question    = status.get("question")    if status else None
        options     = status.get("options")      if status else None
        correct_id  = status.get("correct_option_id") if status else None
        explanation = status.get("explanation")  if status else None

        if not (question and options and correct_id is not None):
            # Legacy entry (closed before content-capture existed) — grab
            # the content via a throwaway forward, then delete it; the
            # forward itself is never what gets answered. Must be
            # forward_message here, not copy_message: copyMessage's API
            # response is just a bare message_id with no poll content at
            # all, so there'd be nothing here to read.
            try:
                probe = await context.bot.forward_message(chat_id=user_id, from_chat_id=year_channel_id(year), message_id=mid)
            except Exception as e:
                print(f"Quiz question {mid} in lecture '{session['lecture_key']}' ({year}) unreachable (likely deleted): {e}")
                await _drop_dead(mid)
                continue
            if probe.poll and probe.poll.correct_option_ids:
                question    = probe.poll.question
                options     = [o.text for o in probe.poll.options]
                correct_id  = probe.poll.correct_option_ids[0]
                explanation = probe.poll.explanation
                if status is not None:
                    status.update(question=question, options=options,
                                   correct_option_id=correct_id, explanation=explanation)
                    await save_quiz_poll_status(year)
                else:
                    # No QUIZ_POLL_STATUS entry existed for this mid AT ALL
                    # (fully legacy — predates poll-status tracking, not
                    # just missing a field on an existing entry). Without
                    # this branch the recovered content only lived in the
                    # local variables above long enough to build and send
                    # THIS poll, then vanished — poll_status_by_mid.get(mid)
                    # would find nothing on the way back, so
                    # _advance_lecture_session's mistakes-bank gate would
                    # silently see status=None and never call
                    # record_mistake for this question, even though it was
                    # correctly delivered and correctly marked wrong. Keyed
                    # by probe.poll.id to match how a normal entry is keyed
                    # (see the QUIZ_POLL_STATUS[year][msg.poll.id] = {...}
                    # assignment where entries are first created), so this
                    # is indistinguishable from a normal entry afterwards.
                    new_status = {
                        "lecture": session["lecture_key"], "message_id": mid,
                        "closed": True, "correct_option_id": correct_id,
                        "question": question, "options": options,
                        "explanation": explanation,
                    }
                    QUIZ_POLL_STATUS[year][probe.poll.id] = new_status
                    poll_status_by_mid[mid] = new_status
                    await save_quiz_poll_status(year)
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=probe.message_id)
            except Exception:
                pass
            if not (question and options and correct_id is not None):
                continue  # still couldn't recover real quiz content — skip it

        timer_seconds = get_question_timer_seconds(user_id)
        session["delivered_count"] = session.get("delivered_count", 0) + 1
        try:
            msg = await context.bot.send_poll(
                chat_id=user_id, question=_numbered_question(question, session["delivered_count"]), options=options,
                type="quiz", correct_option_id=correct_id, is_anonymous=False,
                explanation=(explanation or None),
                open_period=(timer_seconds or None),
            )
        except Exception as e:
            print(f"Couldn't send lecture question {mid}: {e}")
            session["delivered_count"] -= 1   # this send never went out — don't burn a number on it
            continue

        session["current_poll_id"]    = msg.poll.id
        session["current_correct_id"] = correct_id
        session["current_message_id"] = msg.message_id
        session["current_mid"]        = mid
        session["current_delivered_at"] = time.time()  # see _record_time_spent / year leaderboard
        _schedule_question_timeout(context, session.get("kind", "lecture"), user_id, msg.poll.id, timer_seconds)
        return True

    session["current_poll_id"]    = None
    session["current_correct_id"] = None
    session["current_message_id"] = None
    session["current_mid"]        = None
    session["current_delivered_at"] = None
    return False

async def _deliver_all_lecture_questions(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict) -> int:
    """Auto-Next OFF path: sends every remaining question in session['queue']
    up front instead of one at a time. Reuses _deliver_next_lecture_question
    for the actual send/skip-dead-poll/legacy-recovery logic, just calling it
    repeatedly and recording each poll_id -> (correct_option_id, message_id,
    mid) in session['pending_polls'] so handle_poll_answer can match any of
    them, not just a single 'current' one. Returns how many were actually sent."""
    session.setdefault("pending_polls", {})
    sent_count = 0
    while session["queue"]:
        sent = await _deliver_next_lecture_question(context, user_id, session)
        if not sent:
            break
        session["pending_polls"][session["current_poll_id"]] = (
            session["current_correct_id"], session["current_message_id"],
            session["current_mid"], session["current_delivered_at"],
        )
        sent_count += 1
    # These are meaningless in batch mode (there's no single "current"
    # question) — clear them so nothing downstream mistakes this for auto mode.
    session["current_poll_id"]    = None
    session["current_correct_id"] = None
    session["current_message_id"] = None
    session["current_mid"]        = None
    return sent_count

@_serialize_per_user
async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fires when a user answers a poll the bot sent (our lecture-delivery
    polls are always is_anonymous=False specifically so this reliably
    fires). In auto-next mode there's a single question in flight at a
    time, matched via current_poll_id. In batch mode (Auto-Next OFF) every
    question was already sent, so any poll_id in pending_polls can come in,
    in any order, as the user works through them."""
    answer  = update.poll_answer
    poll_id = answer.poll_id
    user_id = answer.user.id
    _update_telegram_name(user_id, answer.user)

    daily_session = DAILY_QUIZ_SESSIONS.get(user_id)
    if daily_session and daily_session.get("current_poll_id") == poll_id:
        daily_session["timeout_streak"] = 0
        chosen     = answer.option_ids[0] if answer.option_ids else None
        is_correct = chosen is not None and chosen == daily_session.get("current_correct_id")
        await _advance_daily_quiz_session(
            context, user_id, daily_session, is_correct, daily_session.get("current_message_id"),
            daily_session.get("current_delivered_at"),
        )
        return

    retake_session = MISTAKES_RETAKE_SESSIONS.get(user_id)
    if retake_session and retake_session.get("current_poll_id") == poll_id:
        retake_session["timeout_streak"] = 0
        chosen     = answer.option_ids[0] if answer.option_ids else None
        is_correct = chosen is not None and chosen == retake_session.get("current_correct_id")
        await _advance_mistakes_retake_session(
            context, user_id, retake_session, is_correct, retake_session.get("current_message_id"),
            retake_session.get("current_delivered_at"),
        )
        return

    session = LECTURE_SESSIONS.get(user_id)
    if not session:
        # No session at all for this user — most likely the bot restarted
        # (all of LECTURE_SESSIONS/DAILY_QUIZ_SESSIONS/MISTAKES_RETAKE_SESSIONS
        # are in-memory only, wiped on restart) while they were mid-quiz.
        # We can't tell from here which kind of session this poll_id used
        # to belong to, so this is deliberately generic rather than
        # guessing "lecture" when it might have been a Daily Quiz or
        # retake. Best-effort: if the send fails, still just return quietly.
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    "⚠️ يبدو إن الجلسة دي اتقفلت (البوت أعاد التشغيل لسه) — "
                    "مينفعش نكمل من نفس السؤال. ابدأ تاني: /quiz للمحاضرات، "
                    "أو من زرار 💥Daily Quiz💥."
                ),
            )
        except Exception:
            pass
        return

    if session.get("mode") == "batch":
        pending = session.get("pending_polls", {})
        if poll_id not in pending:
            return   # not one of this lecture's questions (or already answered)
        correct_id, message_id, mid, delivered_at = pending.pop(poll_id)
        chosen     = answer.option_ids[0] if answer.option_ids else None
        is_correct = chosen is not None and chosen == correct_id
        await _advance_lecture_session(context, user_id, session, is_correct, message_id, mid, delivered_at)
        return

    # ── Spaced Repetition re-ask answer ──────────────────────────
    # Checked before the normal current_poll_id match below, since a
    # re-ask's poll_id was never written into current_poll_id/
    # current_correct_id — see _maybe_deliver_spaced_repetition. No-stakes:
    # doesn't touch session["answered"]/["correct"], XP, or the mistakes
    # bank — just the 🤩/😢 reaction — then falls through to delivering
    # the actual next lecture question, same as a normal answer would.
    if session.get("sr_pending_poll_id") == poll_id:
        chosen     = answer.option_ids[0] if answer.option_ids else None
        is_correct = chosen is not None and chosen == session.get("sr_pending_correct_id")
        sr_message_id = session.get("sr_pending_message_id")
        session.pop("sr_pending", None)
        session.pop("sr_pending_poll_id", None)
        session.pop("sr_pending_correct_id", None)
        session.pop("sr_pending_message_id", None)
        if sr_message_id is not None:
            try:
                await context.bot.set_message_reaction(
                    chat_id=user_id, message_id=sr_message_id,
                    reaction=[ReactionTypeEmoji("🤩" if is_correct else "😢")], is_big=False,
                )
            except Exception:
                pass
        sent_sr = await _maybe_deliver_spaced_repetition(context, user_id, session)
        if not sent_sr:
            sent_next = await _deliver_next_lecture_question(context, user_id, session)
            if not sent_next:
                # Queue was already empty (or every remaining id was dead)
                # by the time this re-ask came back — the lecture actually
                # finished on the answer that triggered the re-ask, but
                # the summary was deferred until the re-ask itself
                # resolved so the two questions don't overlap in the chat.
                await _finish_lecture_session(context, user_id, session)
        return

    if session.get("current_poll_id") != poll_id:
        return   # not the question we're tracking for this user right now

    session["timeout_streak"] = 0
    chosen     = answer.option_ids[0] if answer.option_ids else None
    is_correct = chosen is not None and chosen == session.get("current_correct_id")
    await _advance_lecture_session(
        context, user_id, session, is_correct,
        session.get("current_message_id"), session.get("current_mid"),
        session.get("current_delivered_at"),
    )


async def _maybe_deliver_spaced_repetition(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict) -> bool:
    """Spaced Repetition (Settings toggle, on by default): every 5-8
    questions (re-rolled each time so the interval isn't predictable), if
    the user has a wrong answer from earlier in this lecture that hasn't
    been re-asked yet, re-send it as an extra question before the next
    fresh one. Auto-next mode only — batch mode sends every question
    up front, so there's no "next question" moment to insert into.

    The re-ask is no-stakes: it doesn't touch session['answered']/
    ['correct'], XP, the mistakes bank, or the normal correctness
    reactions — it's purely a memory check with its own reaction set
    (👀 on send, 🤩 if they get it right this time, 😢 if not). Getting
    it wrong again does NOT re-queue it; it stays in the mistakes bank
    from its original miss (see _advance_lecture_session) and the user
    can drill it properly later via the Mistakes Bank / retake flow —
    this feature is a lightweight in-lecture nudge, not a full leitner
    system.

    Sets session['sr_pending'] = mid when a re-ask goes out, which
    handle_poll_answer checks before treating an answer as a normal
    lecture question. Returns whether a re-ask was actually sent."""
    if session.get("mode") == "batch":
        return False
    if session.get("is_retake"):
        return False   # the whole session IS already a re-ask of prior wrong answers — nothing to layer on top
    if not get_spaced_repetition_enabled(user_id):
        return False

    pool = session.setdefault("sr_pool", [])          # wrong mids not yet re-asked, this lecture
    asked = session.setdefault("sr_asked", set())      # wrong mids already re-asked once, this lecture
    for wrong_mid in session.get("wrong_mids", []):
        if wrong_mid not in pool and wrong_mid not in asked:
            pool.append(wrong_mid)
    if not pool:
        return False

    session["sr_counter"] = session.get("sr_counter", 0) + 1
    threshold = session.get("sr_next_threshold")
    if threshold is None:
        threshold = random.randint(5, 8)
        session["sr_next_threshold"] = threshold
    if session["sr_counter"] < threshold:
        return False

    # Time to re-ask. Reset the counter/threshold for the next interval
    # regardless of whether the send below actually succeeds — a poll
    # send failure here shouldn't jam the trigger into firing again next
    # question too.
    session["sr_counter"] = 0
    session["sr_next_threshold"] = random.randint(5, 8)

    wrong_mid = pool.pop(0)
    asked.add(wrong_mid)
    status = session.get("poll_status_by_mid", {}).get(wrong_mid)
    if not (status and status.get("question") and status.get("options") and status.get("correct_option_id") is not None):
        return False   # content not resolvable (shouldn't normally happen — it was just answered) — skip quietly

    try:
        msg = await context.bot.send_poll(
            chat_id=user_id, question=_prefixed_question(status["question"], "🔁 Review: "), options=status["options"],
            type="quiz", correct_option_id=status["correct_option_id"], is_anonymous=False,
            explanation=(status.get("explanation") or None),
        )
    except Exception as e:
        print(f"Couldn't send spaced-repetition re-ask for mid {wrong_mid}: {e}")
        return False

    try:
        await context.bot.set_message_reaction(
            chat_id=user_id, message_id=msg.message_id,
            reaction=[ReactionTypeEmoji("👀")], is_big=False,
        )
    except Exception:
        pass

    session["sr_pending"]            = wrong_mid
    session["sr_pending_poll_id"]    = msg.poll.id
    session["sr_pending_correct_id"] = status["correct_option_id"]
    session["sr_pending_message_id"] = msg.message_id
    return True

async def _finish_lecture_session(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict) -> None:
    """The lecture-complete summary message + retake-staging + leaderboard
    write. Split out of _advance_lecture_session so the spaced-repetition
    re-ask path (see handle_poll_answer) can reach the same completion
    logic when a re-ask empties the queue, without re-running the
    XP/streak/achievement bookkeeping that only applies to a real answer.

    Also where lectures_completed gets incremented and the quick_thinker /
    perfect_run Extras get checked — all three only for a real attempt
    (not a retake), matching the leaderboard/results-file exclusion just
    above."""
    total     = session["total"]
    correct   = session["correct"]
    incorrect = session["answered"] - correct
    pct       = round(correct / session["answered"] * 100) if session["answered"] else 0
    year = session["year"]
    lecture_name = QUIZ_INDEX[year].get(session["lecture_key"], {}).get("name", session["lecture_key"])
    is_retake = session.get("is_retake", False)
    extra_events = []
    if not is_retake:
        # Retakes are practice, not a new attempt at the lecture proper —
        # they never touch the leaderboard or best-score file, and don't
        # count toward lectures_completed or its Extras either.
        await _record_lecture_result(user_id, _lr_key(year, session["lecture_key"]), correct, session["answered"])
        await backup_lecture_results_to_channel(context)
        user_entry = _get_entry(user_id)
        user_entry["lectures_completed"] = user_entry.get("lectures_completed", 0) + 1
        extra_events += _check_achievements(user_entry, "lectures_completed")
        started_at = session.get("started_at")
        if started_at is not None and (time.time() - started_at) <= 15 * 60:
            ach = _check_extra_achievement(user_entry, "quick_thinker")
            if ach:
                extra_events.append(ach)
        if session["answered"] > 0 and pct == 100:
            ach = _check_extra_achievement(user_entry, "perfect_run")
            if ach:
                extra_events.append(ach)
        now_hour = datetime.now(DAILY_QUIZ_TZ).hour
        if 2 <= now_hour < 5:
            ach = _check_extra_achievement(user_entry, "insomniac")
            if ach:
                extra_events.append(ach)
        extra_events += _check_achievements(user_entry, "achievement_collector")
        final_level = _xp_to_level(user_entry["xp"])
        level_up = final_level if final_level > user_entry["level"] else 0
        if level_up:
            user_entry["level"] = final_level
        _mark_analytics_dirty()
        await _announce_events(context, user_id, {"achievements": extra_events, "level_up": level_up})
        await backup_analytics_to_channel(context)
    title = "خلصت مراجعة الأسئلة الغلط!" if is_retake else f"خلصت محاضرة {session['module']} - {session['subject']}: {lecture_name}!"
    summary = (
        f"🎓 <b>{title}</b>\n\n"
        f"✅ صح: {correct}\n"
        f"❌ غلط: {incorrect}\n"
        f"📊 نسبة: {pct}%\n"
        f"📝 عدد الأسئلة: {session['answered']}/{total}\n"
        f"✨ XP: <b>+{session['xp_earned']}</b>"
    )
    result_buttons = [[
        InlineKeyboardButton("🏠 Back to Home", callback_data="back_home"),
        InlineKeyboardButton("📚 More Quizzes", callback_data="quiz_years"),
    ]]
    wrong_mids = session.get("wrong_mids", [])
    if wrong_mids:
        RETAKE_STAGING[user_id] = {
            "year": year, "module": session["module"], "subject": session["subject"],
            "lecture_key": session["lecture_key"], "mids": wrong_mids,
        }
        result_buttons.insert(0, [
            InlineKeyboardButton(f"🔁 Retake incorrect questions! ({len(wrong_mids)})", callback_data="retake_wrong"),
        ])
    result_keyboard = InlineKeyboardMarkup(result_buttons)
    try:
        await context.bot.send_message(
            chat_id=user_id, text=summary, parse_mode=ParseMode.HTML, reply_markup=result_keyboard,
        )
    except Exception:
        pass
    LECTURE_SESSIONS.pop(user_id, None)

async def _advance_lecture_session(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict, is_correct: bool, message_id: int | None = None, mid: int | None = None, delivered_at: float | None = None):
    """Called once handle_poll_answer confirms the user answered their
    current lecture question, and whether it was right. Awards XP —
    15 correct, 5 incorrect — silently (no per-question message) and
    immediately forwards the next question. Once the lecture runs out,
    tacks on a completion bonus and sends a single results summary with the
    right/wrong count and the XP total accumulated across the lecture."""
    session["answered"] += 1
    session["correct"] = session.get("correct", 0) + (1 if is_correct else 0)
    if delivered_at is not None:
        _record_time_spent(user_id, time.time() - delivered_at)
    if not is_correct and mid is not None:
        session.setdefault("wrong_mids", []).append(mid)
        # Also pool this question into the user's own mistakes bank, for
        # the Daily Quiz's "questions you got wrong before" slice. Only
        # store the question id (mid) here, not the full text — full
        # content is resolved on demand later via _resolve_mistake, which
        # reads QUIZ_POLL_STATUS[year] (see MISTAKES BANK schema note).
        # Still check QUIZ_POLL_STATUS captured this poll's content before
        # recording, same as before, so we never bank a reference that's
        # already known to be unresolvable. Looked up via the session's own
        # poll_status_by_mid index (built once at lecture-start, scoped to
        # this lecture) instead of scanning every poll ever tracked for the
        # year on every wrong answer.
        year   = session["year"]
        status = session.get("poll_status_by_mid", {}).get(mid)
        if status and status.get("question") and status.get("options") and status.get("correct_option_id") is not None:
            try:
                if await record_mistake(user_id, mid, year, session["module"], session["subject"]):
                    await backup_mistakes_bank_to_channel(context)
            except Exception as e:
                # Recording a mistake should never be able to break the
                # user's flow to their next question — but it also should
                # never fail silently (that's the exact bug being fixed
                # here), so this prints a clear, specific line rather than
                # relying on whatever the caller further up happens to do
                # with an uncaught exception.
                print(f"MISTAKES BANK: record_mistake failed for user {user_id}, mid {mid}, year {year}: {e}")
        else:
            # This is the other half of the bug this fixes: previously,
            # ANY question that reached here without fully-populated
            # status (question/options/correct_option_id) silently never
            # got recorded — no exception, nothing printed, nothing to
            # notice. Most of those cases are now prevented upstream (see
            # the legacy-recovery branch in _deliver_next_lecture_question
            # that persists recovered content instead of discarding it),
            # but if this still triggers for some other reason, at least
            # it's now visible instead of invisible.
            print(f"MISTAKES BANK: skipped recording mistake for user {user_id}, mid {mid}, year {year} — status incomplete: {status!r}")

    if session.get("mode") == "batch":
        # Everything was already sent up front — "last" means every
        # dispatched question has now been answered, not that the queue is
        # dry (the queue was already drained back at dispatch time).
        is_last = session["answered"] >= session["total"]
    else:
        sent_sr = await _maybe_deliver_spaced_repetition(context, user_id, session)
        if sent_sr:
            is_last = False   # a re-ask went out — lecture isn't over, and no fresh question was pulled this round
        else:
            sent_next = await _deliver_next_lecture_question(context, user_id, session)
            is_last   = not sent_next   # queue ran dry (or every remaining id was dead) — lecture's done

    per_question_xp = XP_LECTURE_CORRECT if is_correct else XP_LECTURE_INCORRECT
    xp_delta         = per_question_xp + (XP_LECTURE_COMPLETE_BONUS if is_last else 0)
    if not session.get("award_xp", True):
        xp_delta = 0   # repeat attempt at a lecture already completed once — no XP farming
    session["xp_earned"] = session.get("xp_earned", 0) + xp_delta

    events     = await _record_activity(user_id, persist=False)
    user_entry = _get_entry(user_id)
    prev_streak = user_entry.get("lecture_correct_streak_current", 0)
    user_entry["lecture_questions_answered"]  += 1
    user_entry["lecture_questions_correct"]   += 1 if is_correct else 0
    user_entry["lecture_questions_incorrect"] += 0 if is_correct else 1
    _record_subject_answer(user_entry, session.get("module"), session.get("subject"), is_correct)
    if is_correct:
        user_entry["lecture_correct_streak_current"] += 1
        if user_entry["lecture_correct_streak_current"] > user_entry["lecture_correct_streak_best"]:
            user_entry["lecture_correct_streak_best"] = user_entry["lecture_correct_streak_current"]
    else:
        user_entry["lecture_correct_streak_current"] = 0

    await _react_to_lecture_answer(
        context, user_id, message_id,
        is_correct=is_correct,
        new_streak=user_entry["lecture_correct_streak_current"],
        streak_broken=(not is_correct and prev_streak > 0),
    )

    events["achievements"] += _check_achievements(user_entry, "questions_answered")
    events["achievements"] += _check_achievements(user_entry, "correct_streak")
    events["achievements"] += _check_achievements(user_entry, "achievement_collector")

    _award_xp(user_entry, xp_delta)
    # recompute from scratch rather than trust _award_xp's own return value —
    # the achievement checks above may have just granted bonus XP of their
    # own, so the true level-up (if any) has to account for all of it together
    final_level = _xp_to_level(user_entry["xp"])
    if final_level > user_entry["level"]:
        user_entry["level"] = final_level
        events["level_up"] = final_level
    _mark_analytics_dirty()
    await _announce_events(context, user_id, events)   # still immediate: level-ups/achievements are rare enough to be worth a heads-up mid-lecture

    await backup_analytics_to_channel(context)

    if is_last:
        await _finish_lecture_session(context, user_id, session)


# ═══════════════════════════════════════════════════════════════
# FORWARDED POLL HANDLER
# ═══════════════════════════════════════════════════════════════
async def handle_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.poll:
        return

    user_id = update.effective_chat.id
    if user_id in SLEEPING:
        return

    poll = update.message.poll
    # Strip any existing A) B) C) prefixes from options to avoid double-labeling
    question      = poll.question
    raw_options   = [strip_leading_letter_prefix(opt.text) for opt in poll.options]
    # Telegram only reveals correct_option_ids if the quiz is closed, or was
    # sent by our own bot / directly to it — an open quiz forwarded from
    # someone else comes back empty. We must NOT guess in that case.
    correct_index = poll.correct_option_ids[0] if poll.correct_option_ids else None
    explanation   = poll.explanation or None

    # An image sent (with no caption / unparseable caption) just before this
    # forward is paired with it.
    pending_img = PENDING_IMAGE.pop(user_id, None)

    # ── Show the poll question + choices ─────────────────────────
    lines = [f"❓ <b>{html.escape(question)}</b>"]
    for opt in raw_options:
        lines.append(html.escape(opt))
    full_text = "\n".join(lines)

    if pending_img:
        with open(pending_img, "rb") as f:
            cap = full_text if len(full_text) <= 1024 else None
            sent = await context.bot.send_photo(
                chat_id=user_id, photo=f, caption=cap,
                parse_mode=ParseMode.HTML if cap else None,
            )
            if not cap:
                await context.bot.send_message(
                    chat_id=user_id, text=full_text,
                    parse_mode=ParseMode.HTML,
                )
    else:
        await context.bot.send_message(
            chat_id=user_id, text=full_text,
            parse_mode=ParseMode.HTML,
        )

# ═══════════════════════════════════════════════════════════════
# IMAGE HANDLER
# ═══════════════════════════════════════════════════════════════
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id  = update.effective_chat.id
    real_uid = update.effective_user.id if update.effective_user else user_id

    # ── /report_issue waiting on this user's next message ──────────
    # Same draft-staging path as the text branch in handle() — see
    # AWAITING_REPORT_ISSUE's comment. Checked first (before SLEEPING and
    # before the normal "image for a quiz question" flow below) so a
    # screenshot sent right after /report_issue is never mistaken for a
    # quiz-question image, and still works even while sleeping.
    report_wait_started = AWAITING_REPORT_ISSUE.pop(real_uid, None)
    if report_wait_started is not None and time.time() - report_wait_started <= REPORT_ISSUE_AWAIT_TIMEOUT:
        if not REPORT_ISSUE_GROUP_ID:
            await update.message.reply_text("⚠️ الميزة دي مش متاحة دلوقتي.")
            return
        report_photo = update.message.photo[-1] if update.message.photo else None
        if not report_photo:
            return
        report_caption = (update.message.caption or "").strip()
        await _stage_report_draft(update, context, real_uid, text=report_caption, photo_file_id=report_photo.file_id)
        return

    if user_id in SLEEPING:
        return

    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        return

    caption = (update.message.caption or "").strip()

    img_dir = os.path.join(IMG_BASE_DIR, str(user_id))
    os.makedirs(img_dir, exist_ok=True)
    img_path = os.path.join(img_dir, f"img_{photo.file_unique_id}.jpg")
    tg_file  = await context.bot.get_file(photo.file_id)
    await tg_file.download_to_drive(img_path)

    # ── Case 1: caption already IS a complete quiz question ─────────
    # Parse and build the question immediately — no need to ask again.
    parsed = parse_mcq_block(caption) if caption else None
    if parsed:
        question, raw_options, correct_index, explanation = parsed
        await deliver_quiz(
            context, user_id, question, raw_options, correct_index,
            explanation=explanation, image_path=img_path,
        )
        events = await _record_activity(real_uid, questions_delta=1)
        _update_telegram_name(real_uid, update.effective_user)
        await react_random(update, context)
        await _announce_events(context, user_id, events)
        await backup_analytics_to_channel(context)
        return

    # ── Case 2: no caption — park the image and ask for the question
    _clear_pending_image(user_id)
    PENDING_IMAGE[user_id] = img_path
    await update.message.reply_text(
        "🖼 <b>استلمت الصورة!</b>\n"
        "دلوقتي ابعت السؤال والاختيارات (بنفس صيغة الأسئلة المعتادة) "
        "وهيتضاف الصورة تلقائي للسؤال ده.",
        parse_mode=ParseMode.HTML,
    )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PDFs sent in a private DM: captioned = treated as a manual question
    (caption parsed as the MCQ text — no attachment, since Telegram polls
    can only carry a photo, not a PDF). Uncaptioned PDFs are silently ignored."""
    if not update.message or not update.message.document:
        return
    user_id  = update.effective_chat.id
    real_uid = update.effective_user.id if update.effective_user else user_id
    if user_id in SLEEPING:
        return
    doc = update.message.document
    if doc.mime_type != "application/pdf":
        return

    caption = (update.message.caption or "").strip()
    if caption:
        parsed = parse_mcq_block(caption)
        if not parsed:
            await update.message.reply_text(
                "⚠️ الكابشن مش صيغة سؤال كاملة (لازم سؤال + اختيارات + إجابة صح متعلّم عليها بـ z)."
            )
            return
        question, raw_options, correct_index, explanation = parsed
        await deliver_quiz(context, user_id, question, raw_options, correct_index, explanation=explanation)
        events = await _record_activity(real_uid, questions_delta=1)
        _update_telegram_name(real_uid, update.effective_user)
        await react_random(update, context)
        await _announce_events(context, user_id, events)
        await backup_analytics_to_channel(context)
        return


# ═══════════════════════════════════════════════════════════════
# STORAGE GROUP — AUTO-INDEXING
# ═══════════════════════════════════════════════════════════════
# Reserved vault key for the onboarding "Where are we?! 🙃" message. Post it
# in the storage group like any other item, with a caption starting with
# this exact word. It starts with an underscore on purpose: the password
# DM lookup (see handle in the STORAGE PASSWORD LOOKUP block) lowercases
# what a user types and matches it against STORAGE_INDEX, and the text
# handler treats this as a normal password too — so a real word here would
# let anyone who typed it read the message early. Guarded there too.
ONBOARDING_STORAGE_KEY = "_onboarding"

async def _index_item(caption: str, message_ids: list):
    password = caption.strip().split(maxsplit=1)[0].lower()
    STORAGE_INDEX.setdefault(password, []).append(sorted(message_ids))
    await save_storage_index()
    return password

async def _send_onboarding_special(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Sends the admin's stored onboarding message (every item filed under
    ONBOARDING_STORAGE_KEY, in the order they were posted), then a final
    🗣️🗣️🔥 يلا بينا button.

    Telegram's copyMessages can't attach a keyboard, so the button always
    goes on its own small follow-up message. If nothing has been stored
    yet (or a copy fails) the user still gets that button — onboarding
    must never dead-end on a missing/broken vault item."""
    for message_ids in STORAGE_INDEX.get(ONBOARDING_STORAGE_KEY, []):
        try:
            await context.bot.copy_messages(
                chat_id=user_id, from_chat_id=STORAGE_GROUP_ID, message_ids=message_ids,
            )
        except Exception as e:
            print(f"ONBOARDING: couldn't copy vault item {message_ids}: {e}")
    await context.bot.send_message(
        chat_id=user_id,
        text="👇",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🗣️🗣️🔥 يلا بينا", callback_data="onboard_go"),
        ]]),
    )

async def _finalize_album(context: ContextTypes.DEFAULT_TYPE, media_group_id: str):
    # Wait for the album's parts to stop arriving before filing it as one item.
    await asyncio.sleep(1.5)
    buf = ALBUM_BUFFER.pop(media_group_id, None)
    if not buf:
        return
    caption = buf["caption"]
    if not caption:
        await context.bot.send_message(
            STORAGE_GROUP_ID,
            "⚠️ ألبوم اتبعت من غير كابشن (كلمة سر) — اتجاهله ومحدش هيقدر يفتحه.",
        )
        return
    password = await _index_item(caption, buf["ids"])
    await backup_storage_to_channel(context)
    await context.bot.send_message(
        STORAGE_GROUP_ID,
        f"✅ اتخزن ألبوم من {len(buf['ids'])} ملف تحت الكلمة: <code>{password}</code>\n"
        f"🐾 <i>{random.choice(QUIZZY_SUCCESS_LINES)}</i>",
        parse_mode=ParseMode.HTML,
    )

async def handle_storage_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Vault posts are normally media + a password caption (handle_storage_message
    below), but the onboarding "Where are we?! 🙃" message is often plain
    text. So a text post in the vault is filed too — but ONLY when its first
    word is exactly ONBOARDING_STORAGE_KEY. Any other text in the storage
    group is ignored, same as before, so nothing else about the vault changes."""
    msg = update.message
    if not msg or not msg.text:
        return
    first_word = msg.text.strip().split(maxsplit=1)[0].lower() if msg.text.strip() else ""
    if first_word != ONBOARDING_STORAGE_KEY:
        return
    await _index_item(msg.text, [msg.message_id])
    await backup_storage_to_channel(context)
    await msg.reply_text(
        f"✅ اتخزن تحت الكلمة: <code>{ONBOARDING_STORAGE_KEY}</code>\n"
        f"🐾 <i>{random.choice(QUIZZY_SUCCESS_LINES)}</i>",
        parse_mode=ParseMode.HTML,
    )

async def handle_storage_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Indexes media posted in STORAGE_GROUP_ID. First caption word = password."""
    msg = update.message
    if not msg:
        return
    caption = (msg.caption or "").strip()

    if msg.media_group_id:
        buf = ALBUM_BUFFER.setdefault(msg.media_group_id, {"ids": [], "caption": None})
        buf["ids"].append(msg.message_id)
        if caption:
            buf["caption"] = caption  # usually only one part of the album carries it
        existing_task = buf.get("task")
        if existing_task:
            existing_task.cancel()
        buf["task"] = asyncio.create_task(_finalize_album(context, msg.media_group_id))
        return

    if not caption:
        await msg.reply_text("⚠️ الملف ده اتبعت من غير كابشن — محتاج كلمة سر في الكابشن عشان يتخزن.")
        return

    password = await _index_item(caption, [msg.message_id])
    await backup_storage_to_channel(context)
    await msg.reply_text(
        f"✅ اتخزن تحت الكلمة: <code>{password}</code>\n"
        f"🐾 <i>{random.choice(QUIZZY_SUCCESS_LINES)}</i>",
        parse_mode=ParseMode.HTML,
    )

async def backup_now_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: force-create/refresh both pinned backups right now, instead of
    waiting for the next real change."""
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return
    await backup_storage_to_channel(context)
    for y in configured_years():
        await backup_quiz_to_channel(context, y)
    lines = [
        "✅ اتعمل باك أب دلوقتي.",
        f"📌 Storage group: {'تم' if STORAGE_BACKUP_STATE.get('backup_msg_id') else 'مش متظبط STORAGE_GROUP_ID'}",
    ]
    for y in YEAR_ORDER:
        if not year_channel_id(y):
            lines.append(f"📌 {year_label(y)}: مش متظبط لسه (مفيش channel_id)")
            continue
        ok = bool(QUIZ_BACKUP_STATE[y].get("backup_msg_id"))
        lines.append(f"📌 {year_label(y)}: {'تم' if ok else 'فشل'}")
    await update.message.reply_text("\n".join(lines))

# ═══════════════════════════════════════════════════════════════
# QUIZ CHANNEL — AUTO-INDEXING
# ═══════════════════════════════════════════════════════════════
async def handle_quiz_channel_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Indexes lectures + quiz polls posted in any year's quiz channel —
    which year is resolved from the incoming chat id (see year_for_chat)."""
    msg = update.channel_post or update.message
    if not msg:
        return
    year = year_for_chat(msg.chat.id)
    if year is None:
        return  # not one of the configured quiz channels
    channel_id = year_channel_id(year)

    # ── A quiz poll — file it under the currently-open lecture ──
    if msg.poll:
        current = QUIZ_STATE[year].get("current_lecture")
        if not current or current not in QUIZ_INDEX[year]:
            await context.bot.send_message(
                channel_id,
                "⚠️ محتاج تبعت اسم المحاضرة الأول (أي رسالة نصية) قبل ما تبعت أسئلة."
            )
            return
        insert_after = QUIZ_INSERT_AFTER[year].get(current, "__none__")
        if insert_after == "__none__":
            # Normal authoring — append at the end, as before.
            QUIZ_INDEX[year][current]["ids"].append(msg.message_id)
        else:
            # Reopened via /edit_quiz's "➕ Insert new poll after" — splice
            # this new question in right after insert_after (or at the
            # very start, if it's None), then advance the marker to this
            # mid so a second poll sent right after lands after this one,
            # not after the original target again.
            ids = QUIZ_INDEX[year][current]["ids"]
            pos = (ids.index(insert_after) + 1) if insert_after is not None and insert_after in ids else 0
            ids.insert(pos, msg.message_id)
            QUIZ_INSERT_AFTER[year][current] = msg.message_id
        await save_quiz_index(year)
        # Track this poll so we know once it's stopped (only then is the
        # correct answer known — needed before it can be delivered as a
        # lecture question). question/options are captured right away since
        # those aren't access-restricted like correct_option_id is; that
        # lets lecture delivery build its own poll (see
        # _deliver_next_lecture_question) without depending on a copy of
        # the original, which would stay anonymous forever.
        QUIZ_POLL_STATUS[year][msg.poll.id] = {
            "lecture":           current,
            "message_id":        msg.message_id,
            "closed":            msg.poll.is_closed,
            "correct_option_id": msg.poll.correct_option_ids[0] if msg.poll.correct_option_ids else None,
            "question":          msg.poll.question,
            "options":           [o.text for o in msg.poll.options],
            "explanation":       msg.poll.explanation,
        }
        await save_quiz_poll_status(year)
        # NOTE: the channel backup document + the "still open" reaction are
        # both deliberately deferred to -END (below) instead of happening
        # here per-question — doing them per-question was sending/pinning
        # a fresh backup document for every single poll.
        return

    # ── Plain text: either "-END" or a new/resumed lecture name ─
    if not msg.text:
        return
    text = msg.text.strip()

    if text.upper() in ("-END", "-FIN"):
        is_fin = text.upper() == "-FIN"
        current = QUIZ_STATE[year].get("current_lecture")
        if not current or current not in QUIZ_INDEX[year]:
            await context.bot.send_message(channel_id, "⚠️ مفيش محاضرة مفتوحة دلوقتي.")
            return

        open_message_ids = [
            p["message_id"] for p in QUIZ_POLL_STATUS[year].values()
            if p["lecture"] == current and not p["closed"]
        ]

        # -FIN: best-effort auto-stop of any question still open. NOTE:
        # Telegram's stopPoll only works on a poll the *bot itself* sent —
        # these quiz polls are posted directly by admins in the channel, so
        # the bot has no API-level way to close them on its own; this loop
        # will normally close nothing and every question will still need a
        # manual Stop Poll, exactly like -END. It's left in as a harmless
        # no-op in case that ever changes (e.g. polls start being relayed
        # through the bot), rather than silently pretending to finalize
        # something it technically can't.
        auto_stopped = 0
        if is_fin:
            for mid in list(open_message_ids):
                try:
                    stopped_poll = await context.bot.stop_poll(chat_id=channel_id, message_id=mid)
                except Exception:
                    continue
                for p in QUIZ_POLL_STATUS[year].values():
                    if p["message_id"] == mid:
                        p["closed"]            = True
                        p["correct_option_id"] = stopped_poll.correct_option_id
                        break
                open_message_ids.remove(mid)
                auto_stopped += 1
            if auto_stopped:
                await save_quiz_poll_status(year)

        QUIZ_INDEX[year][current]["closed"] = True
        await save_quiz_index(year)
        QUIZ_STATE[year]["current_lecture"] = None
        await save_quiz_state(year)
        QUIZ_INSERT_AFTER[year].pop(current, None)   # done editing (if this was an /edit_quiz re-open)

        # Batched now, once, instead of one reaction call per question:
        # mark every still-open (forgot to Stop Poll) question in this
        # lecture with 😢.
        for mid in open_message_ids:
            try:
                await context.bot.set_message_reaction(
                    chat_id=channel_id, message_id=mid,
                    reaction=[ReactionTypeEmoji("😢")], is_big=False,
                )
            except Exception:
                pass

        # Single backup for the whole lecture, once it's actually closed.
        await backup_quiz_to_channel(context, year)

        count = len(QUIZ_INDEX[year][current]["ids"])
        open_count = len(open_message_ids)
        note = (
            f"\n⚠️ {open_count} سؤال لسه مفتوح — لازم توقف التصويت عليه (Stop Poll) "
            f"قبل ما يبقى ممكن يتبعت للطلاب."
            if open_count else "\n✅ كل الأسئلة جاهزة للإرسال."
        )
        if is_fin and auto_stopped:
            note += f"\n🤖 اتقفل {auto_stopped} سؤال تلقائي."
        verb = "اتخلصت" if is_fin else "اتقفلت"
        await context.bot.send_message(
            channel_id,
            f"✅ {verb} محاضرة <b>{current}</b> — {count} سؤال.{note}",
            parse_mode=ParseMode.HTML,
        )
        return

    # New lecture name (or resuming one that already exists).
    # Format: "<Module> - <Subject> Lecture <number>: <name>"
    module, subject, lecture_number, name_or_error = parse_lecture_title(year, text)
    if module is None:
        await context.bot.send_message(channel_id, name_or_error, parse_mode=ParseMode.HTML)
        return
    name = name_or_error

    entry = QUIZ_INDEX[year].setdefault(text, {
        "ids": [], "closed": False,
        "module": module, "subject": subject, "lecture_number": lecture_number, "name": name,
    })
    entry["closed"]         = False
    entry["module"]         = module
    entry["subject"]        = subject
    entry["lecture_number"] = lecture_number
    entry["name"]           = name
    await save_quiz_index(year)
    QUIZ_STATE[year]["current_lecture"] = text
    await save_quiz_state(year)
    await backup_quiz_to_channel(context, year)
    await context.bot.send_message(
        channel_id,
        f"🆕 <b>[{year_label(year)}] {module} - {subject} Lecture {lecture_number}: {name}</b>\n"
        f"ابعت الأسئلة (كويزات) دلوقتي، وابعت <code>-END</code> أو <code>-FIN</code> لما تخلص.\n"
        f"⚠️ لازم توقف كل سؤال (Stop Poll) الأول عشان يبقى قابل للإرسال.",
        parse_mode=ParseMode.HTML,
    )

def _locked_year_modules_view(user_id: int | None, years: list) -> tuple[str, InlineKeyboardMarkup] | None:
    """If this user has a year/class set whose channel is still configured
    and has at least one ready module, returns the (text, keyboard) for
    jumping straight to their module list — skipping the "which year?"
    step entirely, since onboarding now makes year/class mandatory (see
    _onboarding_gate) and there's normally no other year for them to
    pick from anyway. Shared by quiz_lectures_cmd (/quiz) and the
    quiz_years callback ("📚 More Quizzes" / "🔙 رجوع للسنين") so both
    entry points skip consistently.

    Returns None — meaning "fall back to the full year picker" — only
    for the now-rare edge cases where locking straight to a year isn't
    actually possible: no year_class on record (shouldn't happen post-
    onboarding, but covers pre-onboarding-gate legacy sessions), that
    year's channel no longer configured, or it has no ready modules yet."""
    if not user_id:
        return None
    year_class = get_year_class(user_id)
    if year_class not in years or not year_channel_id(year_class):
        return None
    modules = ready_modules(year_class)
    if not modules:
        return None
    buttons = [
        [InlineKeyboardButton(module_label(m), callback_data=f"module:{year_class}:{i}")]
        for i, m in enumerate(modules)
    ]
    # Search Content 🔎 — scoped to this locked year; module (or "all
    # modules") is picked on the next screen. See search_year:/search_mod:
    # in button_handler and AWAITING_SEARCH_QUERY.
    buttons.append([InlineKeyboardButton("Search Content 🔎", callback_data=f"search_year:{year_class}")])
    # Not "🔙 رجوع للسنين" here — with a locked year/class there's no
    # other year behind it to go back to, so that button would just
    # reopen this exact same screen. Back to Home is the only meaningful
    # "out" from here now.
    buttons.append([InlineKeyboardButton("🏠 Back to Home", callback_data="back_home")])
    text = f"📚 <b>{year_label(year_class)}</b> — اختار الموديول:"
    return text, InlineKeyboardMarkup(buttons)

async def quiz_lectures_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User-facing: /quiz. Jumps straight to the module list for the
    caller's own year/class (set via the onboarding/Settings year_class
    prompt — see get_year_class, year_class_keyboard, _locked_year_modules_view),
    skipping the "which year?" step entirely. Falls back to the full
    year-picker (old behaviour) if that's not possible right now — see
    _locked_year_modules_view for exactly when."""
    user_id = update.effective_user.id if update.effective_user else None
    years = configured_years()
    if not years:
        await update.message.reply_text("📭 مفيش سنين متاحة دلوقتي.")
        return

    view = _locked_year_modules_view(user_id, years)
    if view:
        text, markup = view
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"yr:{y}")] for y in years]
    buttons.append([InlineKeyboardButton("Search Content 🔎", callback_data="search_pick_year")])
    await update.message.reply_text(
        "📚 <b>اختار السنة:</b>", parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )

def _next_daily_quiz_line() -> str:
    """One line: how long until the next 💥Daily Quiz💥 push, plus its
    Cairo clock time. Shared by /time and the post-quiz results summary."""
    next_push = next_daily_quiz_time()
    now       = datetime.now(DAILY_QUIZ_TZ)
    delta     = next_push - now
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes = remainder // 60
    when = "النهاردة" if next_push.date() == now.date() else "بكرة"
    return f"⏰ الـ Daily Quiz الجاية: {when} الساعة {next_push.strftime('%I:%M %p')} (بعد {hours} ساعة و{minutes} دقيقة)"

async def time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/time — current Cairo time, and when the next 💥Daily Quiz💥 push is."""
    now = datetime.now(DAILY_QUIZ_TZ)
    await update.message.reply_text(
        f"🕒 دلوقتي: <b>{now.strftime('%I:%M %p')}</b> (توقيت القاهرة)\n\n"
        f"{_next_daily_quiz_line()}",
        parse_mode=ParseMode.HTML,
    )

async def daily_module_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: pick a year, then a module, to temporarily override that
    year's DAILY_QUIZ_ACTIVE_MODULE default (e.g. to switch early, before
    the code constant itself gets updated). Own callback_data namespace
    (dqy:/dqm:/dq_scope_off) — deliberately separate from the yr:/module:
    user-facing browsing flow, since this is a one-time admin scope pick,
    not lecture navigation."""
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return
    years = configured_years()
    if not years:
        await update.message.reply_text("📭 مفيش سنين متاحة دلوقتي.")
        return
    scope = get_daily_quiz_scope()
    current = f"\n\nدلوقتي محدد: {year_label(scope['year'])} — {scope['module']}" if scope else "\n\nدلوقتي: كل المنهج (مفيش تحديد)"
    buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"dqy:{y}")] for y in years]
    if scope:
        buttons.append([InlineKeyboardButton("🔓 شيل التحديد (رجّع كل المنهج)", callback_data="dq_scope_off")])
    await update.message.reply_text(
        f"📚 <b>Daily Quiz — اختار الموديول اللي هيتحدد عليه:</b>{current}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )

def _quiz_year_arg(context) -> tuple[str | None, str | None]:
    """Shared arg-parsing for /quiz_list and /quiz_delete: expects the
    year key as the first arg. Returns (year, error_message)."""
    years = configured_years()
    valid = ", ".join(years) if years else "(مفيش سنين متظبطة)"
    if not context.args or context.args[0] not in YEARS:
        return None, f"استخدام: /quiz_list <سنة>\nالسنين المتاحة: {valid}"
    year = context.args[0]
    if not year_channel_id(year):
        return None, f"⚠️ {year_label(year)} لسه مفيهاش channel_id متظبط."
    return year, None

async def quiz_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /quiz_list <year> — numbered list of ALL lectures (open +
    closed) in that year, for /quiz_delete."""
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return
    year, err = _quiz_year_arg(context)
    if err:
        await update.message.reply_text(err)
        return
    index = QUIZ_INDEX[year]
    if not index:
        await update.message.reply_text(f"📭 مفيش محاضرات مسجلة لسه في {year_label(year)}.")
        return
    lines = [f"📋 <b>كل محاضرات {year_label(year)}:</b>"]
    for i, (key, v) in enumerate(index.items(), 1):
        status = "✅ مقفولة" if v["closed"] else "🟡 لسه مفتوحة"
        lecnum = f" {v['lecture_number']}" if v.get("lecture_number") else ""
        lines.append(f"{i}. {v['module']} - {v['subject']} Lecture{lecnum}: {v['name']} — {len(v['ids'])} سؤال — {status}")
    lines.append(f"\nاستخدم /quiz_delete {year} &lt;رقم&gt; للحذف")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def quiz_delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /quiz_delete <year> <n> — asks for confirmation, then
    removes a lecture from that year's index (does not delete the actual
    channel messages; only stops it showing up in /quiz). The actual
    removal happens in button_handler's quizdel_yes branch once the admin
    taps to confirm; see PENDING_QUIZ_DELETE."""
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return
    years = configured_years()
    valid = ", ".join(years) if years else "(مفيش سنين متظبطة)"
    if len(context.args) < 2 or context.args[0] not in YEARS or not context.args[1].isdigit():
        await update.message.reply_text(
            f"استخدام: /quiz_delete <سنة> <رقم>\nالسنين المتاحة: {valid}\nشوف الأرقام في /quiz_list <سنة>"
        )
        return
    year = context.args[0]
    if not year_channel_id(year):
        await update.message.reply_text(f"⚠️ {year_label(year)} لسه مفيهاش channel_id متظبط.")
        return
    n = int(context.args[1])
    index = QUIZ_INDEX[year]
    keys = list(index.keys())
    if n < 1 or n > len(keys):
        await update.message.reply_text(f"❌ رقم غلط — فيه {len(keys)} محاضرة بس في {year_label(year)}")
        return
    key     = keys[n - 1]
    lecture = index[key]

    admin_id = update.effective_user.id
    PENDING_QUIZ_DELETE[admin_id] = (year, key)
    await update.message.reply_text(
        f"⚠️ <b>متأكد إنك عايز تمسح المحاضرة دي؟</b>\n\n"
        f"{year_label(year)} — {lecture['module']} - {lecture['subject']}: {lecture['name']}\n"
        f"(الرسايل نفسها هتفضل في القناة — العملية دي بس بتشيلها من /quiz)",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 أيوه، امسح", callback_data="quizdel_yes")],
            [InlineKeyboardButton("🔙 لأ، سيبها", callback_data="quizdel_no")],
        ]),
    )

async def edit_quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /edit_quiz (alias: /quiz_edit) — browse year -> module -> subject -> lecture
    (same drill-down as /quiz), then pick a question from that lecture to
    delete it or insert a new one right after it. Reuses the same
    yr:/module:/subject: browsing callback_data as /quiz so the flow
    doesn't have to duplicate module/subject listing — only the last step
    (picking a lecture) branches into the editquiz: namespace instead of
    lecture:/lecturego:."""
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return
    years = configured_years()
    if not years:
        await update.message.reply_text("📭 مفيش سنين متاحة دلوقتي.")
        return
    buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"eqyr:{y}")] for y in years]
    await update.message.reply_text(
        "✏️ <b>Edit Quiz — اختار السنة:</b>", parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )

# ═══════════════════════════════════════════════════════════════
# TEXT MESSAGE HANDLER
# ═══════════════════════════════════════════════════════════════
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id  = update.effective_chat.id
    real_uid = update.effective_user.id if update.effective_user else user_id
    if user_id in SLEEPING:
        return

    text = update.message.text.strip()

    # ── "-Reply <id> <message>" (report-issue reply, plain text) ─────
    # Works from REPORT_ISSUE_GROUP_ID regardless of who Telegram reports
    # as the sender — deliberately NOT identity-based (no AWAITING_* /
    # real_uid matching) because the button-driven two-step flow below
    # this one silently breaks when the group has "remain anonymous"
    # enabled for admins: a typed message then arrives with
    # effective_user = GroupAnonymousBot, not the admin's real id, so any
    # check keyed on real_uid never matches. Parsing a self-contained
    # command out of the message text sidesteps that identity question
    # entirely — the id is right there in the text, no state to match up
    # against who's supposedly typing. Not admin-gated beyond "must be
    # sent in this group" for the same reason: if you're posting in a
    # private group only admins are in, that's the access control.
    if REPORT_ISSUE_GROUP_ID and user_id == REPORT_ISSUE_GROUP_ID and text.startswith("-Reply"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3 or not parts[1].isdigit():
            await update.message.reply_text(
                "⚠️ الصيغة: <code>-Reply &lt;id&gt; &lt;رسالتك&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        group_message_id = int(parts[1])
        reply_text        = parts[2]
        thread = REPORT_THREADS.get(group_message_id)
        if not thread:
            await update.message.reply_text(f"⚠️ مفيش report بالـ ID ده: {group_message_id}")
            return
        if thread.get("closed"):
            await update.message.reply_text("⚠️ الـ report ده مقفول بالفعل.")
            return
        _append_report_message(thread, "admin", reply_text)
        user_dm_ok = await _refresh_report_thread(context, group_message_id, thread)
        if not user_dm_ok:
            await update.message.reply_text("⚠️ الرد اتسجل بس معرفتش أبعته للمستخدم (يمكن قافل البوت).")
        return

    # ── AWAITING USER FOLLOWUP (reporter's own "↩️ Reply") ───────
    # Keyed by real_uid (the reporter, in their own DM — no anonymous-
    # admin identity issue here, this only ever runs in a private chat).
    pending_followup = AWAITING_USER_FOLLOWUP.pop(real_uid, None)
    if pending_followup:
        group_message_id = pending_followup["group_message_id"]
        thread = REPORT_THREADS.get(group_message_id)
        if not thread:
            await update.message.reply_text("⚠️ الـ report ده مش لاقيه دلوقتي (يمكن البوت اتعمله restart).")
            return
        if thread.get("closed"):
            await update.message.reply_text("⚠️ الـ report ده اتقفل، مينفعش ترد عليه تاني.")
            return
        _append_report_message(thread, "user", text)
        await update.message.reply_text("✅ اتبعت للأدمن.")
        await _refresh_report_thread(context, group_message_id, thread)
        return

    # ── AWAITING BROADCAST MESSAGE (/broadcast composer) ──────────
    # Set only via the admin-gated "✏️ Set Message" button — the is_admin
    # check here is just defense in depth, not the actual access control.
    if AWAITING_BROADCAST_MESSAGE.pop(real_uid, None):
        if not is_admin(update):
            return
        draft = BROADCAST_DRAFTS.setdefault(real_uid, {"audience": "all", "text": None})
        draft["text"] = text
        body, markup = _broadcast_composer_view(real_uid)
        await update.message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    # ── AWAITING SEARCH QUERY (🔎 Search Content) ─────────────────
    # Set by the search_mod: callback once year (+ module, or "all") is
    # picked — see button_handler. Keyed the same way (real_uid via
    # query.from_user.id there, which is the same id as effective_user
    # here).
    pending_search = AWAITING_SEARCH_QUERY.pop(real_uid, None)
    if pending_search:
        await _send_search_results(context, user_id, pending_search["year"], pending_search["module"], text)
        return

    # ── AWAITING NICKNAME (Settings, or first-ever /start) ───────
    # Keyed by real_uid (the person's Telegram user id, same key SETTINGS
    # uses), not the chat id, so this works the same in DMs and groups.
    awaiting = AWAITING_NICKNAME.get(real_uid)
    if awaiting:
        onboarding = (awaiting == "onboarding")
        del AWAITING_NICKNAME[real_uid]
        nickname = text[:32].strip()
        if not nickname:
            if onboarding:
                # Still no nickname on file — re-ask instead of falling
                # through to the settings menu, since there isn't a main
                # menu to fall back to yet.
                AWAITING_NICKNAME[real_uid] = "onboarding"
                await update.message.reply_text(
                    "⚠️ الاسم فاضي — اكتب اسم تحب أتنادي بيه عليك.",
                )
            else:
                await update.message.reply_text(
                    "⚠️ الاسم فاضي — جرب تاني.",
                    reply_markup=settings_menu_keyboard(real_uid),
                )
            return
        if _contains_vulgar_word(nickname):
            if onboarding:
                # Same re-ask pattern as the empty-name case above — no
                # main menu to fall back to yet during onboarding.
                AWAITING_NICKNAME[real_uid] = "onboarding"
                await update.message.reply_text(
                    "⚠️ الاسم ده مش مناسب — اكتب اسم تاني.",
                )
            else:
                await update.message.reply_text(
                    "⚠️ الاسم ده مش مناسب — جرب اسم تاني.",
                    reply_markup=settings_menu_keyboard(real_uid),
                )
            return
        entry = _get_settings_entry(real_uid)
        entry["nickname"] = nickname
        await save_settings()
        await backup_settings_to_channel(context)
        _get_entry(real_uid)["nickname"] = nickname
        await save_analytics()
        await backup_analytics_to_channel(context)
        if onboarding:
            await update.message.reply_text(
                f"What a lovely name Dr.{html.escape(nickname)} 🥰\n\n"
                "What Year/Class are you currently in?\n\n"
                "(⚠️ Set your class correctly, you can NOT change it again later ⚠️)",
                parse_mode=ParseMode.HTML,
                reply_markup=year_class_keyboard("onboard_yc"),
            )
        else:
            await update.message.reply_text(
                f"✅ اتسجل! هنناديك <b>{html.escape(nickname)}</b> دلوقتي.",
                parse_mode=ParseMode.HTML,
                reply_markup=settings_menu_keyboard(real_uid),
            )
        return

    # ── AWAITING REPORT ISSUE TEXT (/report_issue) ───────────────
    # Keyed by real_uid, same as nickname above. Stages a draft (preview
    # + Send/Cancel buttons) instead of posting straight to admins — see
    # _stage_report_draft. Expired waits (see REPORT_ISSUE_AWAIT_TIMEOUT)
    # fall through and this message is handled normally below instead.
    report_wait_started = AWAITING_REPORT_ISSUE.pop(real_uid, None)
    if report_wait_started is not None and time.time() - report_wait_started <= REPORT_ISSUE_AWAIT_TIMEOUT:
        if not REPORT_ISSUE_GROUP_ID:
            await update.message.reply_text("⚠️ الميزة دي مش متاحة دلوقتي.")
            return
        await _stage_report_draft(update, context, real_uid, text=text, photo_file_id=None)
        return


    # ── AWAITING ADMIN REPLY TEXT (report_reply button) ──────────
    # Keyed by real_uid normally — but if this group has "remain
    # anonymous" enabled for admins, a message the admin sends here
    # arrives with effective_user = GroupAnonymousBot, not the admin's
    # real Telegram id, even though the earlier button tap (a callback
    # query, unaffected by anonymous-admin mode) correctly recorded
    # AWAITING_REPORT_REPLY under the admin's real id. That mismatch was
    # silently swallowing every reply: the pop-by-real_uid below found
    # nothing and fell through with no message and no error.
    #
    # Fix: only the admin is ever expected to type in this specific
    # group, so if the message is IN this group at all, resolve the
    # pending reply by chat rather than strictly requiring real_uid to
    # match — find whichever AWAITING_REPORT_REPLY entry (there should
    # only ever be zero or one at a time in practice) exists, regardless
    # of whose id it's filed under.
    pending_reply = AWAITING_REPORT_REPLY.pop(real_uid, None)
    if pending_reply is None and REPORT_ISSUE_GROUP_ID and user_id == REPORT_ISSUE_GROUP_ID and AWAITING_REPORT_REPLY:
        fallback_uid = next(iter(AWAITING_REPORT_REPLY))
        pending_reply = AWAITING_REPORT_REPLY.pop(fallback_uid)
    if pending_reply:
        group_message_id = pending_reply["group_message_id"]
        thread = REPORT_THREADS.get(group_message_id)
        if not thread:
            await update.message.reply_text("⚠️ الـ report ده مش لاقيه دلوقتي (يمكن البوت اتعمله restart).")
            return
        _append_report_message(thread, "admin", text)
        user_dm_ok = await _refresh_report_thread(context, group_message_id, thread)
        if not user_dm_ok:
            await update.message.reply_text("⚠️ الرد اتسجل بس معرفتش أبعته للمستخدم (يمكن قافل البوت).")
        return

    # ── STORAGE PASSWORD LOOKUP ──────────────────────────────────
    if update.effective_chat.type == "private":
        items = STORAGE_INDEX.get(text.lower()) if text.lower() != ONBOARDING_STORAGE_KEY else None
        if items:
            for message_ids in items:
                try:
                    await context.bot.copy_messages(
                        chat_id=user_id,
                        from_chat_id=STORAGE_GROUP_ID,
                        message_ids=message_ids,
                    )
                except Exception as e:
                    print(f"Storage delivery failed for password lookup: {e}")
                    await update.message.reply_text(
                        quizzy_block(QUIZZY_OOPS_ART, random.choice(QUIZZY_ERROR_LINES)),
                        parse_mode=ParseMode.HTML,
                    )
            return

    try:
        blocks = re.split(r"\n\s*\n", text)

        if len(blocks) > MAX_QUESTIONS_PER_MSG:
            await update.message.reply_text(
                f"❌ الحد الأقصى {MAX_QUESTIONS_PER_MSG} سؤال في المرة الواحدة"
            )
            return

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # ── WRITTEN ─────────────────────────────────────────
            written = parse_written_strict(block)
            if written:
                title, content = written
                await update.message.reply_text(
                    f"*{title}*\n||{content}||",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                continue

            # ── MCQ ─────────────────────────────────────────────
            lines = normalize_mcq_block(block)
            if len(lines) < 3:
                # Only warn if this looks like a genuine (but broken)
                # attempt at a question — i.e. it has at least one
                # option-style line (a)/b)/1. ...). Plain chat text never
                # matches that, so "hi"/"شكرا"/etc. pass through silently
                # instead of getting flagged as a format error, while
                # someone who typed a)/b)/c) but got the shape wrong
                # still gets pointed at the right format.
                if _looks_like_mcq_attempt(lines):
                    await update.message.reply_text(
                        "⚠️ <b>الصياغة غلط!</b>\n\n"
                        "الشكل الصح هو:\n"
                        "<code>السؤال\n"
                        "a) خيار 1\n"
                        "b) خيار 2 z  ← علّم الصح بـ z\n"
                        "c) خيار 3\n"
                        "ex: الشرح (اختياري)</code>",
                        parse_mode=ParseMode.HTML,
                    )
                continue

            question, raw_options, correct_index, explanation = parse_mcq_lines(lines)

            if correct_index is None or correct_index >= len(raw_options):
                await update.message.reply_text(
                    "⚠️ <b>ما فيش إجابة صح!</b>\n\n"
                    "علّم الإجابة الصحيحة بـ <code>z</code> في نهايتها:\n"
                    "<code>b) الإجابة الصح z</code>",
                    parse_mode=ParseMode.HTML,
                )
                continue

            # An image sent (with no caption / unparseable caption) just
            # before this message is paired with this question.
            pending_img = PENDING_IMAGE.pop(user_id, None)

            await deliver_quiz(
                context, user_id, question, raw_options, correct_index,
                explanation=explanation, image_path=pending_img,
            )
            events = await _record_activity(real_uid, questions_delta=1)
            _update_telegram_name(real_uid, update.effective_user)
            await react_random(update, context)
            await _announce_events(context, user_id, events)
            await backup_analytics_to_channel(context)

    except Exception as e:
        print("ERROR:", e)

# ═══════════════════════════════════════════════════════════════
# QUIZZY POPUP EASTER EGGS
#
# A handful of toasts (query.answer(text), show_alert left False — a small
# banner that fades by itself, no OK button) on top of specific button
# taps — Quizzy (the bot's cat persona) getting a word in on top of
# whatever that button normally does. None of these ever block or change
# the underlying action. They're all decided in one place, _tap_toast,
# and shown by the single answer() at the top of button_handler — a tap
# can only be answered once, so a toast can't be raised from inside a
# branch after that.
#
# Two of the five scenarios this was scoped for don't fit this
# mechanism at all, because a toast only exists as a response to a
# button tap (a callback query) — there's no equivalent for a poll answer:
#   - "User gets 10/10" happens when the LAST question of a Daily
#     Quiz is answered — that's a poll vote (handle_poll_answer),
#     not a button tap, so there's no callback query to answer with
#     a popup. The perfect-score line is appended to the completion
#     summary message text instead — same joke, different delivery,
#     see _finish-daily-quiz block in handle_poll_answer.
#   - "User opens quizzes around 3–5 AM" is applied at every place a
#     user actually starts a quiz session via a button tap (Daily
#     Quiz, a lecture, a Mistakes Bank retake) rather than at every
#     screen that merely mentions quizzes, since "opens quizzes" reads
#     as starting one, not browsing a menu.
# ═══════════════════════════════════════════════════════════════
QUIZZY_LATE_NIGHT_START_HOUR = 3   # inclusive
QUIZZY_LATE_NIGHT_END_HOUR   = 5   # exclusive — 3:00–4:59 local (DAILY_QUIZ_TZ)
QUIZZY_LATE_NIGHT_MSG        = "🐱 Bro just go to sleep."
QUIZZY_ALREADY_DONE_MSG      = "🐱 didn't I already give you one..."
QUIZZY_PERFECT_SCORE_LINE    = "🐱 I would say I'm proud of you, but I'm literally a cat."
QUIZZY_MISTAKES_EMPTY_MSG    = "🐱 Does this make you bankrupt?"
QUIZZY_MISTAKES_HIGH_MSG     = "🐱 they say mistakes make you stronger, how much can you bench-press?"
# Toasts for the non-Quizzy taps that used to be OK-button alerts (see _tap_toast)
TOAST_SR_NEEDS_AUTO_NEXT   = "⚠️ Spaced Repetition لازم يكون معاه Auto-Next شغال"
TOAST_AUTO_NEXT_OFF_SR     = "⚠️ قفلت Auto-Next، فـ Spaced Repetition مش هيشتغل لحد ما ترجعه"
TOAST_BANK_ALREADY_EMPTY   = "🎉 بنك الأخطاء بتاعك فاضي أصلاً!"
TOAST_REPORT_CLOSED        = "⚠️ الـ report ده اتقفل، مينفعش ترد عليه تاني."
TOAST_REPORT_NOT_YOURS     = "⚠️ مش قادر أعمل كده."
QUIZZY_MISTAKES_HIGH_THRESHOLD = 10   # mistake count at/above which the bench-press jab fires

def _quizzy_is_late_night() -> bool:
    """Same clock/timezone as the insomniac Extra achievement
    (DAILY_QUIZ_TZ), but its own separate 3–5 AM window rather than
    reusing that achievement's 2–5 AM — a popup and an Extra are
    different things and don't need to agree on the exact cutoff."""
    hour = datetime.now(DAILY_QUIZ_TZ).hour
    return QUIZZY_LATE_NIGHT_START_HOUR <= hour < QUIZZY_LATE_NIGHT_END_HOUR

# ── Onboarding toasts ──────────────────────────────────────────────
# Quizzy's onboarding quips are shown as a toast (query.answer(text) with
# show_alert left False): a banner that appears at the top of the chat and
# disappears by itself, instead of a message that stays in the chat. A
# toast only exists as the answer to a button tap, so this covers the two
# quips that ARE taps — the one after picking a Year/Class, and the
# welcome line when tapping 🗣️🗣️🔥 يلا بينا. The typed-/start greetings
# have no callback query to answer, so they remain ordinary messages.
# Telegram truncates a toast at 200 characters; every line here is far under.
ONBOARDING_YEAR_QUIPS = {
    "y1": "Year 1? You are a new-comer! Oh You will love it here.",
    "y2": "Year 2? Oh you are in for a trip! But don't worry it will be fun. 😉",
    "y3": "Year 3? Wouldn't that be... Oh! You are becoming a Semi-Senior soon!!",
}

def _tap_toast(callback_data: str | None, user_id: int) -> str | None:
    """Quizzy's toast for this button tap, or None for no toast (the normal
    case — None makes query.answer() behave exactly as a bare answer() did).

    Every Quizzy popup lives here, decided BEFORE the handler runs, because
    Telegram takes one answer per tap and button_handler answers first
    thing. Doing the lookup here (rather than answering again from inside
    the branch) is what makes them show at all.

    Never raises: a failure looking up the bank/date must not take down
    the tap it's decorating, so any error just means no toast."""
    data = callback_data or ""
    try:
        # ── onboarding ──
        if data.startswith("onboard_yc:"):
            quip = ONBOARDING_YEAR_QUIPS.get(data.split(":", 1)[1])
            return f"🐱 {quip}" if quip else None
        if data == "onboard_go":
            return "🐱 " + random.choice(QUIZZY_WELCOME_LINES)

        # ── Daily Quiz: "already done today" outranks the late-night nudge —
        # the one place that race surfaces is a stale Start button tapped
        # after the quiz was completed some other way, and it's the more
        # specific joke for that exact case.
        if data == "daily_quiz_begin":
            if get_daily_quiz_last_date(user_id) == _today():
                return QUIZZY_ALREADY_DONE_MSG
            return QUIZZY_LATE_NIGHT_MSG if _quizzy_is_late_night() else None

        # ── Mistakes Bank menu: empty / very full ──
        if data == "mistakes_bank_menu":
            count = len(_scoped_mistakes_bank(user_id))
            if count == 0:
                return QUIZZY_MISTAKES_EMPTY_MSG
            if count >= QUIZZY_MISTAKES_HIGH_THRESHOLD:
                return QUIZZY_MISTAKES_HIGH_MSG
            return None

        # ── starting a lecture quiz / a Mistakes Bank retake at 3–5 AM ──
        if data.startswith("lecturego:") or data == "mistakes_retake":
            return QUIZZY_LATE_NIGHT_MSG if _quizzy_is_late_night() else None

        # ── Settings: the Spaced Repetition / Auto-Next mismatch warnings.
        # The handler flips the toggle AFTER this runs, so this predicts
        # the outcome from the CURRENT state: turning SR on while auto-next
        # is off, or turning auto-next off while SR is on. (Safe to read
        # ahead of the handler: button_handler is serialized per user, so
        # nothing can change the state in between.)
        if data == "toggle_spaced_repetition":
            turning_on = not get_spaced_repetition_enabled(user_id)
            return TOAST_SR_NEEDS_AUTO_NEXT if (turning_on and not get_auto_next_enabled(user_id)) else None
        if data == "toggle_auto_next":
            turning_off = get_auto_next_enabled(user_id)
            return TOAST_AUTO_NEXT_OFF_SR if (turning_off and get_spaced_repetition_enabled(user_id)) else None

        # ── Settings: Clear Mistakes Bank ──
        if data == "clear_mistakes_bank_ask":
            # same scoped count the handler uses for its own empty check
            return TOAST_BANK_ALREADY_EMPTY if not _scoped_mistakes_bank(user_id) else None
        if data == "clear_mistakes_bank_yes":
            # The handler deletes exactly _scoped_mistakes_bank(user_id)
            # (this user's entries in the /daily_module scope, or all of
            # theirs with no scope) — the same set the confirm screen
            # counted — so count that same set.
            cleared = len(_scoped_mistakes_bank(user_id))
            return f"✅ اتمسح {cleared} سؤال من بنك الأخطاء بتاعك."

        # ── Report follow-up: the reporter's own "↩️ Reply" button ──
        if data.startswith("report_user_reply:"):
            thread = REPORT_THREADS.get(int(data.split(":", 1)[1]))
            if not thread or thread.get("closed"):
                return TOAST_REPORT_CLOSED
            if thread["user_id"] != user_id:
                return TOAST_REPORT_NOT_YOURS
            return None
    except Exception as e:
        print(f"TOAST: couldn't decide a toast for {data!r}: {e}")
    return None

# ═══════════════════════════════════════════════════════════════
# INLINE BUTTON HANDLER
# ═══════════════════════════════════════════════════════════════
@_serialize_per_user
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    # Telegram takes ONE answerCallbackQuery per tap, and this is it — so a
    # Quizzy toast (a small banner at the top of the screen that fades on
    # its own, i.e. answer(text) WITHOUT show_alert) has to be decided
    # right here rather than answered later inside the branch. See
    # _tap_toast for which taps get one.
    await query.answer(text=_tap_toast(query.data, user_id))

    # ── QUESTION TIMEOUT — resume/abandon a paused session ────────────
    # Only shown after two consecutive timed-out questions in a row (see
    # _handle_question_timeout / QUESTION TIMEOUT section above).
    if query.data.startswith("qresume:"):
        kind    = query.data.split(":", 1)[1]
        store   = _SESSION_STORE_BY_KIND.get(kind)
        session = store.get(user_id) if store is not None else None
        if not session or not session.get("paused"):
            await query.edit_message_text("⚠️ مفيش جلسة متوقفة نكملها دلوقتي.")
            return
        session["paused"] = False
        session["timeout_streak"] = 0
        await query.edit_message_text("▶️ تمام، ياللا نكمل!")
        # The question that triggered the pause was never answered —
        # skip it as wrong now, same as the first timeout in a row does,
        # then carry on delivering the rest of the queue as normal.
        await _advance_session_for_kind(context, kind, user_id, session, is_correct=False)
        return

    if query.data.startswith("qabandon:"):
        kind  = query.data.split(":", 1)[1]
        store = _SESSION_STORE_BY_KIND.get(kind)
        if store is not None:
            store.pop(user_id, None)
        label = _QUIZ_KIND_LABEL.get(kind, "الجلسة")
        await query.edit_message_text(
            f"🥀 تم إلغاء الـ {label}. تقدر تبدأ واحدة جديدة في أي وقت.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Home", callback_data="back_home")]]),
        )
        return

    # ── BROADCAST composer (admin only) — see the BROADCAST COMMAND ──
    # section for _broadcast_composer_view / _send_broadcast.
    if query.data.startswith("bc") and query.data.split(":", 1)[0] in (
        "bcaud", "bcmsg", "bcmsgcancel", "bcpreview", "bcsend", "bccancel"
    ):
        if not is_admin(update):
            await query.answer("🚫 للأدمن فقط", show_alert=True)
            return

        if query.data.startswith("bcaud:"):
            key = query.data.split(":", 1)[1]
            if key in BROADCAST_AUDIENCE_ORDER:
                BROADCAST_DRAFTS.setdefault(user_id, {"audience": "all", "text": None})["audience"] = key
            body, markup = _broadcast_composer_view(user_id)
            await query.edit_message_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
            return

        if query.data == "bcmsg":
            AWAITING_BROADCAST_MESSAGE[user_id] = True
            await query.edit_message_text(
                "✏️ ابعت نص الرسالة اللي عايز تبثها دلوقتي.\n"
                "HTML بسيط متاح: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>, <code>&lt;code&gt;</code>...",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="bcmsgcancel")]]),
            )
            return

        if query.data == "bcmsgcancel":
            AWAITING_BROADCAST_MESSAGE.pop(user_id, None)
            body, markup = _broadcast_composer_view(user_id)
            await query.edit_message_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)
            return

        if query.data == "bcpreview":
            draft = BROADCAST_DRAFTS.get(user_id)
            text  = draft.get("text") if draft else None
            if not text:
                await query.answer("⚠️ لسه مفيش رسالة.", show_alert=True)
                return
            try:
                await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
            except Exception as e:
                await query.answer(f"⚠️ مشكلة في الرسالة (يمكن الـ HTML مش مظبوط): {e}", show_alert=True)
            return

        if query.data == "bccancel":
            BROADCAST_DRAFTS.pop(user_id, None)
            AWAITING_BROADCAST_MESSAGE.pop(user_id, None)
            await query.edit_message_text("❌ Broadcast اتلغى.")
            return

        if query.data == "bcsend":
            draft = BROADCAST_DRAFTS.get(user_id)
            text  = draft.get("text") if draft else None
            if not text:
                await query.answer("⚠️ لسه مفيش رسالة.", show_alert=True)
                return
            audience   = draft["audience"]
            recipients = _broadcast_audience_user_ids(audience)
            if not recipients:
                await query.answer("⚠️ مفيش مستخدمين في الجمهور ده دلوقتي.", show_alert=True)
                return
            BROADCAST_DRAFTS.pop(user_id, None)
            AWAITING_BROADCAST_MESSAGE.pop(user_id, None)
            await query.edit_message_text("📡 بيتجهز للإرسال…")
            await _send_broadcast(context, query.message, audience, text, recipients)
            return


    # ── /restore: run one system's restore-from-pin on demand ────────
    if query.data.startswith("restore_go:"):
        if not is_admin(update):
            await query.answer("🚫 للأدمن فقط", show_alert=True)
            return
        key = query.data.split(":", 1)[1]
        target = _RESTORE_TARGETS.get(key)
        if not target:
            await query.edit_message_text("⚠️ مش لاقي الـ system ده.")
            return
        label, restore_fn = target
        await query.edit_message_text(f"🔄 بيعمل restore لـ {label}…")
        status = await restore_fn(context.application)
        if status == "ok":
            await context.bot.send_message(chat_id=user_id, text=f"✅ {label} — تم الـ restore من آخر نسخة مثبتة.")
        elif status == "no_backup":
            reset_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"🆕 ابدأ {label} من جديد (Reset)", callback_data=f"restore_reset:{key}"),
            ]])
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"ℹ️ {label} — مفيش نسخة احتياطية متثبتة (pinned) في القناة بتاعته دلوقتي، "
                    "فمحصلش أي restore والداتا المحلية زي ما هي.\n\n"
                    "لو عايز تبدأ الملف ده من جديد (reset لملف فاضي) بدل ما تدور على نسخة قديمة، دوس تحت:"
                ),
                reply_markup=reset_kb,
            )
        elif status == "not_configured":
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⚠️ {label} — القناة/الجروب بتاعه مش متظبط في الإعدادات، فمحصلش أي حاجة.",
            )
        elif status == "invalid":
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ {label} — لقيت نسخة مثبتة بس شكلها/structure غلط أو تالف، "
                    "فرفضت أعمل restore منها عشان محدش يتبعثر محلياً. اتبعتلك رسالة تانية بالتفاصيل."
                ),
            )
        else:  # "error"
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ {label} — الـ restore فشل. اتبعتلك رسالة تانية بالتفاصيل (نفس رسالة فشل الـ restore وقت التشغيل).",
            )
        return

    # ── /restore: "no backup pinned" -> offer to reset (blank file) ──
    if query.data.startswith("restore_reset:"):
        if not is_admin(update):
            await query.answer("🚫 للأدمن فقط", show_alert=True)
            return
        key = query.data.split(":", 1)[1]
        target = _RESTORE_TARGETS.get(key)
        if not target or key not in _RESET_ACTIONS:
            await query.edit_message_text("⚠️ مش لاقي الـ system ده.")
            return
        label, _ = target
        await query.edit_message_text(
            f"⚠️ متأكد إنك عايز تبدأ {label} من ملف جديد فاضي؟ الداتا المحلية الحالية لِـ {label} "
            "(لو فيه حاجة) هتتمسح ويتبعت ملف فاضي كـ backup جديد.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ أيوه، اعمل Reset", callback_data=f"restore_reset_confirm:{key}"),
                InlineKeyboardButton("❌ لأ، إلغاء", callback_data=f"restore_reset_cancel:{key}"),
            ]]),
        )
        return

    if query.data.startswith("restore_reset_confirm:"):
        if not is_admin(update):
            await query.answer("🚫 للأدمن فقط", show_alert=True)
            return
        key = query.data.split(":", 1)[1]
        target = _RESTORE_TARGETS.get(key)
        reset_fn = _RESET_ACTIONS.get(key)
        if not target or not reset_fn:
            await query.edit_message_text("⚠️ مش لاقي الـ system ده.")
            return
        label, _ = target
        await query.edit_message_text(f"🆕 بيعمل reset لـ {label}…")
        try:
            await reset_fn(context)
            await context.bot.send_message(chat_id=user_id, text=f"✅ {label} — اتعمله reset، وبقى فيه ملف فاضي جديد متثبت في القناة بتاعته.")
        except Exception as e:
            print(f"{label.upper()} RESET ERROR:", e)
            await context.bot.send_message(chat_id=user_id, text=f"❌ {label} — الـ reset فشل: {e}")
        return

    if query.data.startswith("restore_reset_cancel:"):
        if not is_admin(update):
            await query.answer("🚫 للأدمن فقط", show_alert=True)
            return
        key = query.data.split(":", 1)[1]
        label = _RESTORE_TARGETS.get(key, (key, None))[0]
        await query.edit_message_text(f"🗑 اتلغى — {label} زي ما هي، مفيش حاجة اتغيرت.")
        return

    # ── REPORT ISSUE: draft confirmation (user's own Send/Cancel) ────
    if query.data == "report_draft_send":
        draft = REPORT_DRAFTS.pop(user_id, None)
        if draft is None:
            await query.edit_message_text("⚠️ مفيش بلاغ متسجل دلوقتي — جرب /report_issue تاني.")
            return
        if time.time() - draft["created_at"] > REPORT_DRAFT_CONFIRM_TIMEOUT:
            await query.edit_message_text("⌛ البلاغ ده قديم شوية — جرب /report_issue تاني.")
            return
        ok = await _submit_report(context, user_id, query.from_user, draft["text"], draft["photo_file_id"])
        if ok:
            await query.edit_message_text("✅ اتبعتت. هيتم الرد عليك من هنا لما الأدمن يشوفها.")
        else:
            await query.edit_message_text("⚠️ مشكلة في إرسال الرسالة — جرب تاني لو سمحت.")
        return

    if query.data == "report_draft_cancel":
        REPORT_DRAFTS.pop(user_id, None)
        await query.edit_message_text("🗑 اتلغى. تقدر تبدأ بلاغ جديد بـ /report_issue في أي وقت.")
        return

    # ── REPORT ISSUE: reply / close (admin-only, from REPORT_ISSUE_GROUP_ID) ──
    if query.data == "report_noop":
        return   # "🔒 Closed" button on an already-closed report — nothing to do

    # ── REPORT ISSUE: reporter's own "↩️ Reply" on the admin's DM'd reply ──
    # No admin gate here — this button is on the REPORTER's own DM, meant
    # for them specifically. group_message_id ties it back to the right
    # thread regardless of how many reports this person has ever filed.
    if query.data.startswith("report_user_reply:"):
        group_message_id = int(query.data.split(":", 1)[1])
        thread = REPORT_THREADS.get(group_message_id)
        if not thread or thread.get("closed"):
            return   # "report closed" toast was shown by _tap_toast
        if thread["user_id"] != user_id:
            # Shouldn't happen (this button only ever goes out to the
            # thread's own reporter), but don't let a forwarded/replayed
            # callback_data let someone follow up on someone else's thread.
            # (its "can't do that" toast was shown by _tap_toast)
            return
        AWAITING_USER_FOLLOWUP[user_id] = {"group_message_id": group_message_id}
        await context.bot.send_message(
            chat_id=user_id,
            text="✏️ اكتب ردك، وهيتبعت للأدمن على طول.",
        )
        return

    if query.data.startswith("report_reply:"):
        if not is_admin(update):
            await query.answer("🚫 للأدمن فقط", show_alert=True)
            return
        group_message_id = int(query.data.split(":", 1)[1])
        thread = REPORT_THREADS.get(group_message_id)
        if not thread or thread.get("closed"):
            await query.answer("⚠️ الـ report ده مقفول أو مش لاقيه.", show_alert=True)
            return
        AWAITING_REPORT_REPLY[user_id] = {"group_message_id": group_message_id}
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="✏️ اكتب ردك على المستخدم ده في رسالة، وهيتبعتله على طول.",
                reply_to_message_id=query.message.message_id,
            )
        except Exception as e:
            print("REPORT REPLY PROMPT FAILED:", e)
            AWAITING_REPORT_REPLY.pop(user_id, None)   # prompt never went out — don't leave a dangling awaiting-state
            await query.answer("⚠️ مشكلة في إرسال طلب الرد — جرب تاني.", show_alert=True)
        return

    if query.data.startswith("report_close:"):
        if not is_admin(update):
            await query.answer("🚫 للأدمن فقط", show_alert=True)
            return
        group_message_id = int(query.data.split(":", 1)[1])
        thread = REPORT_THREADS.get(group_message_id)
        if not thread:
            await query.answer("⚠️ الـ report ده مش لاقيه دلوقتي.", show_alert=True)
            return
        thread["closed"] = True
        AWAITING_REPORT_REPLY.pop(user_id, None)   # cancel any reply this admin was mid-typing for it
        AWAITING_USER_FOLLOWUP.pop(thread["user_id"], None)   # ...and any follow-up the reporter was mid-typing
        await save_report_threads()
        await query.edit_message_text(
            _report_thread_text(thread, group_message_id), parse_mode=ParseMode.HTML,
            reply_markup=_report_reply_keyboard(group_message_id, closed=True),
        )
        await backup_report_threads_to_channel(context)
        return

    # ── 🔎 SEARCH CONTENT ────────────────────────────────────────────
    # search_pick_year  -> full year picker (only reachable when this user
    #                      has no locked year/class — see _locked_year_modules_view)
    # search_year:<year> -> module picker (+ "All Modules") for that year
    # search_mod:<year>:<module|__ALL__> -> arms AWAITING_SEARCH_QUERY and
    #                      prompts for the search text; also what
    #                      "🔎 Search Again" reuses after a completed search.
    if query.data == "search_pick_year":
        years = configured_years()
        if not years:
            await query.edit_message_text("📭 مفيش سنين متاحة دلوقتي.")
            return
        buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"search_year:{y}")] for y in years]
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="quiz_years")])
        await query.edit_message_text(
            "🔎 <b>Search Content</b> — اختار السنة:", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data.startswith("search_year:"):
        year = query.data.split(":", 1)[1]
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if not modules:
            await query.edit_message_text(f"📭 مفيش موديولات متظبطة لـ {year_label(year)} لسه.")
            return
        buttons = [[InlineKeyboardButton("🔎 All Modules", callback_data=f"search_mod:{year}:{SEARCH_MOD_ALL}")]]
        buttons += [[InlineKeyboardButton(module_label(m), callback_data=f"search_mod:{year}:{m}")] for m in modules]
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="quiz_years")])
        await query.edit_message_text(
            f"🔎 <b>Search Content</b> — {year_label(year)}: اختار الموديول (أو كله):",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data.startswith("search_mod:"):
        _, year, mod_token = query.data.split(":", 2)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        module = None if mod_token == SEARCH_MOD_ALL else mod_token
        if module and module not in ready_modules(year):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        AWAITING_SEARCH_QUERY[user_id] = {"year": year, "module": module}
        await query.edit_message_text(
            f"🔎 <b>Search Content</b>\n📚 {_search_scope_label(year, module)}\n\n"
            "Type a word or a part of a question and Quizzy will search for it!",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="quiz_years")]]),
        )
        return

    # ── QUIZ YEARS: top-level list ──────────────────────────────────
    if query.data == "quiz_years":
        years = configured_years()
        if not years:
            await query.edit_message_text("📭 مفيش سنين متاحة دلوقتي.")
            return
        view = _locked_year_modules_view(user_id, years)
        if view:
            text, markup = view
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"yr:{y}")] for y in years]
        buttons.append([InlineKeyboardButton("Search Content 🔎", callback_data="search_pick_year")])
        await query.edit_message_text(
            "📚 <b>اختار السنة:</b>", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── YEAR: list modules within one year ──────────────────────────
    if query.data.startswith("yr:") and query.data.count(":") == 1:
        year = query.data.split(":")[1]
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if not modules:
            await query.edit_message_text(f"📭 مفيش موديولات متظبطة لـ {year_label(year)} لسه.")
            return
        buttons = [[InlineKeyboardButton(module_label(m), callback_data=f"module:{year}:{i}")] for i, m in enumerate(modules)]
        buttons.append([InlineKeyboardButton("🔙 رجوع للسنين", callback_data="quiz_years")])
        await query.edit_message_text(
            f"📚 <b>{year_label(year)}</b> — اختار الموديول:", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── QUIZ MODULE: list subjects within one module ───────────────
    if query.data.startswith("module:") and query.data.count(":") == 2:
        _, year, mod_idx_str = query.data.split(":")
        mod_idx = int(mod_idx_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        buttons = [
            [InlineKeyboardButton(subject_label(s), callback_data=f"subject:{year}:{mod_idx}:{i}")]
            for i, s in enumerate(subjects)
        ]
        buttons.append([InlineKeyboardButton("🔙 رجوع للموديولات", callback_data=f"yr:{year}")])
        await query.edit_message_text(
            f"🎓 <b>{year_label(year)} — {module}</b> — اختار المادة:", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── QUIZ SUBJECT: list lectures within one module + subject ────
    if query.data.startswith("subject:"):
        _, year, mod_idx_str, subj_idx_str = query.data.split(":")
        mod_idx, subj_idx = int(mod_idx_str), int(subj_idx_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        if subj_idx >= len(subjects):
            await query.edit_message_text("⚠️ المادة دي مش موجودة دلوقتي.")
            return
        subject = subjects[subj_idx]
        names = ready_lecture_keys(year, module, subject)
        buttons = [
            [InlineKeyboardButton(
                f"Lecture {QUIZ_INDEX[year][name]['lecture_number'] or (i + 1)}: {QUIZ_INDEX[year][name]['name']}",
                callback_data=f"lecture:{year}:{mod_idx}:{subj_idx}:{i}",
            )]
            for i, name in enumerate(names)
        ]
        buttons.append([InlineKeyboardButton("🔙 رجوع للمواد", callback_data=f"module:{year}:{mod_idx}")])
        header = f"🎓 <b>{year_label(year)} — {module} - {subject}</b>"
        if not names:
            header += "\n\n📭 لسه مفيش محاضرات هنا."
        await query.edit_message_text(
            header, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── LECTURE: show a preview (leaderboard + your stats) before starting ──
    if query.data.startswith("lecture:"):
        _, year, mod_idx_str, subj_idx_str, lec_idx_str = query.data.split(":")
        mod_idx, subj_idx, lec_idx = int(mod_idx_str), int(subj_idx_str), int(lec_idx_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return

        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        if subj_idx >= len(subjects):
            await query.edit_message_text("⚠️ المادة دي مش موجودة دلوقتي.")
            return
        subject = subjects[subj_idx]
        names = ready_lecture_keys(year, module, subject)
        if lec_idx >= len(names):
            await query.edit_message_text("⚠️ المحاضرة دي مش موجودة دلوقتي.")
            return
        lecture_key = names[lec_idx]
        entry = QUIZ_INDEX[year][lecture_key]
        lr_key = _lr_key(year, lecture_key)

        board = _lecture_leaderboard(lr_key)
        lines = [f"🎓 <b>{year_label(year)} — {module} - {subject}: {entry['name']}</b>\n"]
        if board:
            medals = ["🥇", "🥈", "🥉"]
            lines.append("🏆 <b>أفضل النتائج:</b>")
            for i, row in enumerate(board):
                medal = medals[i] if i < len(medals) else f"{i + 1}."
                lines.append(
                    f"{medal} {html.escape(row['nickname'])} — "
                    f"{row['best_correct']}/{row['best_total']} ({row['best_pct']}%)"
                )
        else:
            lines.append("🏆 محدش خد المحاضرة دي لسه — يلا كن أول واحد!")

        my_result = _get_lecture_results(lr_key).get(str(user_id))
        if my_result:
            lines.append(
                f"\n📌 أحسن نتيجة ليك: {my_result['best_correct']}/{my_result['best_total']} "
                f"({my_result['best_pct']}%) — حاولت {my_result['attempts']} مرة"
            )

        buttons = [
            [InlineKeyboardButton("▶️ ابدأ المحاضرة", callback_data=f"lecturego:{year}:{mod_idx}:{subj_idx}:{lec_idx}")],
            [InlineKeyboardButton("🔙 رجوع للمحاضرات", callback_data=f"subject:{year}:{mod_idx}:{subj_idx}")],
        ]
        await query.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── LECTUREGO: start one-at-a-time delivery of a closed lecture's ready quizzes ──
    if query.data.startswith("lecturego:"):
        _, year, mod_idx_str, subj_idx_str, lec_idx_str = query.data.split(":")
        mod_idx, subj_idx, lec_idx = int(mod_idx_str), int(subj_idx_str), int(lec_idx_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return

        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        if subj_idx >= len(subjects):
            await query.edit_message_text("⚠️ المادة دي مش موجودة دلوقتي.")
            return
        subject = subjects[subj_idx]
        names = ready_lecture_keys(year, module, subject)
        if lec_idx >= len(names):
            await query.edit_message_text("⚠️ المحاضرة دي مش موجودة دلوقتي.")
            return
        lecture_key = names[lec_idx]
        entry = QUIZ_INDEX[year][lecture_key]
        ids   = entry["ids"]

        # Only polls Telegram has confirmed as stopped are actually
        # deliverable — a quiz poll's correct answer isn't known until
        # it's closed, and we need that to rebuild it as our own poll.
        closed_message_ids = {v["message_id"] for v in QUIZ_POLL_STATUS[year].values() if v["closed"]}
        ready_ids     = [mid for mid in ids if mid in closed_message_ids]
        not_ready_cnt = len(ids) - len(ready_ids)

        if not ready_ids:
            await query.edit_message_text(
                "⚠️ محدش قفل أي سؤال في المحاضرة دي لسه — هتتبعت لما الأدمن يوقف التصويت."
                if not_ready_cnt else
                "⚠️ المحاضرة دي مفيهاش أسئلة.",
            )
            return

        if get_randomize_enabled(user_id):
            ready_ids = list(ready_ids)
            random.shuffle(ready_ids)

        auto_next = get_auto_next_enabled(user_id)
        lr_key = _lr_key(year, lecture_key)
        already_attempted = str(user_id) in _get_lecture_results(lr_key)

        # Built once here, scoped to just this lecture's polls, so
        # _advance_lecture_session can do an O(1) lookup by message_id per
        # wrong answer instead of an O(n) scan over every poll ever tracked
        # for the year.
        poll_status_by_mid = {
            v["message_id"]: v
            for v in QUIZ_POLL_STATUS[year].values()
            if v["lecture"] == lecture_key
        }

        session = {
            "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
            "queue": list(ready_ids), "current_poll_id": None, "current_correct_id": None,
            "total": len(ready_ids), "answered": 0, "correct": 0,
            "mode": "auto" if auto_next else "batch",
            "pending_polls": {},
            "award_xp": not already_attempted,   # no XP farming on repeat attempts
            "poll_status_by_mid": poll_status_by_mid,
            "started_at": time.time(),   # see EXTRA_ACHIEVEMENTS' quick_thinker, checked in _finish_lecture_session
            "kind": "lecture",
        }
        LECTURE_SESSIONS[user_id] = session

        await query.edit_message_text(
            f"🎓 <b>{year_label(year)} — {module} - {subject}: {entry['name']}</b> — {len(ready_ids)} سؤال، "
            + ("هيتبعتولك واحد واحد 👇" if auto_next else "هيتبعتولك كلهم دلوقتي 👇")
            + ("\n\n(محاولة تانية — من غير XP)" if already_attempted else ""),
            parse_mode=ParseMode.HTML,
        )

        if auto_next:
            sent = await _deliver_next_lecture_question(context, user_id, session)
        else:
            session["total"] = 0  # corrected below to how many actually go out
            sent_count = await _deliver_all_lecture_questions(context, user_id, session)
            session["total"] = sent_count
            sent = sent_count > 0
        if not sent:
            LECTURE_SESSIONS.pop(user_id, None)
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ المحاضرة دي اتحذفت من القناة، فاتشالت من القايمة.",
            )
            await backup_quiz_to_channel(context, year)
            return

        if not_ready_cnt:
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"⚠️ {not_ready_cnt} Quiz not ready yet"
                    "use /report_issue للتواصل مع أدمن"
                ),
            )
        return

    # ═══════════════════════════════════════════════════════════
    # /edit_quiz — year -> module -> subject -> lecture -> question ->
    # delete / insert-after. Own eqyr:/eqmodule:/eqsubject:/eqlecture:/
    # eqq:/eqdel:/eqins: namespace, deliberately parallel to the
    # yr:/module:/subject:/lecture: browsing flow above rather than
    # reusing it, since every step here needs an admin gate and the final
    # step (lecture) branches completely differently (question list
    # instead of a start/leaderboard preview).
    # ═══════════════════════════════════════════════════════════

    if query.data.startswith("eqyr:") or query.data.startswith("eqmodule:") \
            or query.data.startswith("eqsubject:") or query.data.startswith("eqlecture:") \
            or query.data.startswith("eqq:") or query.data.startswith("eqdel:") \
            or query.data.startswith("eqins:"):
        if not is_admin(update):
            await query.answer("🚫 للأدمن فقط", show_alert=True)
            return

    if query.data == "eqyr_root":
        years = configured_years()
        if not years:
            await query.edit_message_text("📭 مفيش سنين متاحة دلوقتي.")
            return
        buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"eqyr:{y}")] for y in years]
        await query.edit_message_text(
            "✏️ <b>Edit Quiz — اختار السنة:</b>", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data.startswith("eqyr:") and query.data.count(":") == 1:
        year = query.data.split(":")[1]
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if not modules:
            await query.edit_message_text(f"📭 مفيش موديولات متظبطة لـ {year_label(year)} لسه.")
            return
        buttons = [[InlineKeyboardButton(module_label(m), callback_data=f"eqmodule:{year}:{i}")] for i, m in enumerate(modules)]
        buttons.append([InlineKeyboardButton("🔙 رجوع للسنين", callback_data="eqyr_root")])
        await query.edit_message_text(
            f"✏️ <b>{year_label(year)}</b> — اختار الموديول:", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data.startswith("eqmodule:") and query.data.count(":") == 2:
        _, year, mod_idx_str = query.data.split(":")
        mod_idx = int(mod_idx_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        buttons = [
            [InlineKeyboardButton(subject_label(s), callback_data=f"eqsubject:{year}:{mod_idx}:{i}")]
            for i, s in enumerate(subjects)
        ]
        buttons.append([InlineKeyboardButton("🔙 رجوع للموديولات", callback_data=f"eqyr:{year}")])
        await query.edit_message_text(
            f"✏️ <b>{year_label(year)} — {module}</b> — اختار المادة:", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data.startswith("eqsubject:"):
        _, year, mod_idx_str, subj_idx_str = query.data.split(":")
        mod_idx, subj_idx = int(mod_idx_str), int(subj_idx_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        if subj_idx >= len(subjects):
            await query.edit_message_text("⚠️ المادة دي مش موجودة دلوقتي.")
            return
        subject = subjects[subj_idx]
        names = ready_lecture_keys(year, module, subject)
        buttons = [
            [InlineKeyboardButton(
                f"Lecture {QUIZ_INDEX[year][name]['lecture_number'] or (i + 1)}: {QUIZ_INDEX[year][name]['name']}",
                callback_data=f"eqlecture:{year}:{mod_idx}:{subj_idx}:{i}",
            )]
            for i, name in enumerate(names)
        ]
        buttons.append([InlineKeyboardButton("🔙 رجوع للمواد", callback_data=f"eqmodule:{year}:{mod_idx}")])
        header = f"✏️ <b>{year_label(year)} — {module} - {subject}</b>"
        if not names:
            header += "\n\n📭 لسه مفيش محاضرات هنا."
        await query.edit_message_text(
            header, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── EQLECTURE: list this lecture's questions (first 16 chars each) ──
    if query.data.startswith("eqlecture:"):
        _, year, mod_idx_str, subj_idx_str, lec_idx_str = query.data.split(":")
        mod_idx, subj_idx, lec_idx = int(mod_idx_str), int(subj_idx_str), int(lec_idx_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        if subj_idx >= len(subjects):
            await query.edit_message_text("⚠️ المادة دي مش موجودة دلوقتي.")
            return
        subject = subjects[subj_idx]
        names = ready_lecture_keys(year, module, subject)
        if lec_idx >= len(names):
            await query.edit_message_text("⚠️ المحاضرة دي مش موجودة دلوقتي.")
            return
        lecture_key = names[lec_idx]
        entry = QUIZ_INDEX[year][lecture_key]
        ids = entry["ids"]
        if not ids:
            await query.edit_message_text(
                f"✏️ <b>{entry['name']}</b>\n\n📭 مفيش أسئلة في المحاضرة دي.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 رجوع", callback_data=f"eqsubject:{year}:{mod_idx}:{subj_idx}")
                ]]),
            )
            return

        poll_status_by_mid = {v["message_id"]: v for v in QUIZ_POLL_STATUS[year].values() if v["lecture"] == lecture_key}
        # Text list (first 32 chars of each question) + a numbered grid of
        # buttons below it — callback_data still carries the question's own
        # immutable channel message_id (mid), NOT its position in `ids`,
        # since positions shift whenever an earlier question in this same
        # lecture gets deleted (see eqq:/eqdel:/eqins: below, which all
        # look the question up by mid rather than trusting an index).
        lines = []
        number_buttons, row = [], []
        for i, mid in enumerate(ids, 1):
            status = poll_status_by_mid.get(mid)
            preview = html.escape(status["question"][:32]) if status and status.get("question") else "؟؟؟"
            lines.append(f"{i}. {preview}")
            row.append(InlineKeyboardButton(str(i), callback_data=f"eqq:{year}:{mod_idx}:{subj_idx}:{lec_idx}:{mid}"))
            if len(row) == 6:
                number_buttons.append(row)
                row = []
        if row:
            number_buttons.append(row)
        number_buttons.append([InlineKeyboardButton("🔙 رجوع للمحاضرات", callback_data=f"eqsubject:{year}:{mod_idx}:{subj_idx}")])
        await query.edit_message_text(
            f"✏️ <b>{entry['name']}</b> — اختار رقم السؤال اللي عايز تعدله:\n\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(number_buttons),
        )
        return

    # ── EQQ: one question picked — show its preview + action buttons ──
    # Identified by mid (the question's own channel message_id), not by
    # position — see the comment on the eqlecture: button-building above.
    if query.data.startswith("eqq:"):
        _, year, mod_idx_str, subj_idx_str, lec_idx_str, mid_str = query.data.split(":")
        mod_idx, subj_idx, lec_idx, mid = int(mod_idx_str), int(subj_idx_str), int(lec_idx_str), int(mid_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        if subj_idx >= len(subjects):
            await query.edit_message_text("⚠️ المادة دي مش موجودة دلوقتي.")
            return
        subject = subjects[subj_idx]
        names = ready_lecture_keys(year, module, subject)
        if lec_idx >= len(names):
            await query.edit_message_text("⚠️ المحاضرة دي مش موجودة دلوقتي.")
            return
        lecture_key = names[lec_idx]
        entry = QUIZ_INDEX[year][lecture_key]
        ids = entry["ids"]
        if mid not in ids:
            await query.edit_message_text("⚠️ السؤال ده مش موجود دلوقتي — يمكن اتعدل من حتة تانية.")
            return
        q_pos = ids.index(mid)   # display-only (e.g. "3/12") — never used to look anything up
        status = next((v for v in QUIZ_POLL_STATUS[year].values() if v["message_id"] == mid), None)
        preview = html.escape(status["question"]) if status and status.get("question") else "؟؟؟"

        buttons = [
            [InlineKeyboardButton("🗑 Delete this poll", callback_data=f"eqdel:{year}:{mod_idx}:{subj_idx}:{lec_idx}:{mid}")],
            [InlineKeyboardButton("➕ Insert new poll after", callback_data=f"eqins:{year}:{mod_idx}:{subj_idx}:{lec_idx}:{mid}")],
            [InlineKeyboardButton("🔙 رجوع للأسئلة", callback_data=f"eqlecture:{year}:{mod_idx}:{subj_idx}:{lec_idx}")],
        ]
        await query.edit_message_text(
            f"✏️ <b>{entry['name']}</b> — سؤال {q_pos + 1}/{len(ids)}\n\n"
            f"❓ {preview}\n\nاختار الإجراء:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── EQDEL: remove this question from the lecture's index + poll status ──
    # (channel message itself is left untouched — same convention as
    # /quiz_delete for whole lectures.)
    if query.data.startswith("eqdel:"):
        _, year, mod_idx_str, subj_idx_str, lec_idx_str, mid_str = query.data.split(":")
        mod_idx, subj_idx, lec_idx, mid = int(mod_idx_str), int(subj_idx_str), int(lec_idx_str), int(mid_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        if subj_idx >= len(subjects):
            await query.edit_message_text("⚠️ المادة دي مش موجودة دلوقتي.")
            return
        subject = subjects[subj_idx]
        names = ready_lecture_keys(year, module, subject)
        if lec_idx >= len(names):
            await query.edit_message_text("⚠️ المحاضرة دي مش موجودة دلوقتي.")
            return
        lecture_key = names[lec_idx]
        entry = QUIZ_INDEX[year][lecture_key]
        ids = entry["ids"]
        if mid not in ids:
            await query.edit_message_text("⚠️ السؤال ده مش موجود دلوقتي — يمكن اتعدل من حتة تانية.")
            return
        ids.remove(mid)   # by value (mid), not by position — see eqlecture: button comment above
        await save_quiz_index(year)
        for pid in [pid for pid, v in QUIZ_POLL_STATUS[year].items() if v["message_id"] == mid]:
            QUIZ_POLL_STATUS[year].pop(pid, None)
        await save_quiz_poll_status(year)
        await backup_quiz_to_channel(context, year)

        buttons = [[InlineKeyboardButton("🔙 رجوع للأسئلة", callback_data=f"eqlecture:{year}:{mod_idx}:{subj_idx}:{lec_idx}")]]
        await query.edit_message_text(
            f"🗑 <b>اتشال السؤال من {entry['name']}</b>\n"
            "(الرسالة نفسها لسه موجودة في القناة — احذفها يدوي لو عايز)",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # ── EQINS: reopen this lecture in the quiz channel so the admin can ──
    # post new poll(s) right after this question, then -END as usual.
    if query.data.startswith("eqins:"):
        _, year, mod_idx_str, subj_idx_str, lec_idx_str, mid_str = query.data.split(":")
        mod_idx, subj_idx, lec_idx, target_mid = int(mod_idx_str), int(subj_idx_str), int(lec_idx_str), int(mid_str)
        if year not in YEARS or not year_channel_id(year):
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        subjects = ready_subjects(year, module)
        if subj_idx >= len(subjects):
            await query.edit_message_text("⚠️ المادة دي مش موجودة دلوقتي.")
            return
        subject = subjects[subj_idx]
        names = ready_lecture_keys(year, module, subject)
        if lec_idx >= len(names):
            await query.edit_message_text("⚠️ المحاضرة دي مش موجودة دلوقتي.")
            return
        lecture_key = names[lec_idx]
        entry = QUIZ_INDEX[year][lecture_key]
        ids = entry["ids"]
        if target_mid not in ids:
            await query.edit_message_text("⚠️ السؤال ده مش موجود دلوقتي — يمكن اتعدل من حتة تانية.")
            return

        other_current = QUIZ_STATE[year].get("current_lecture")
        if other_current and other_current != lecture_key:
            await query.edit_message_text(
                f"⚠️ فيه محاضرة تانية مفتوحة دلوقتي في القناة (<b>{other_current}</b>) — "
                "لازم تقفلها بـ -END الأول قبل ما تضيف سؤال هنا.",
                parse_mode=ParseMode.HTML,
            )
            return

        entry["closed"] = False
        await save_quiz_index(year)
        QUIZ_STATE[year]["current_lecture"] = lecture_key
        await save_quiz_state(year)
        QUIZ_INSERT_AFTER[year][lecture_key] = target_mid

        await query.edit_message_text(
            f"➕ <b>{entry['name']}</b> اتفتحت تاني للإضافة.\n\n"
            "دلوقتي ابعت السؤال (أو الأسئلة) الجديدة في القناة — هتتحط بعد السؤال اللي اخترته على طول.\n"
            "لما تخلص، ابعت <code>-END</code> في القناة زي المعتاد.",
            parse_mode=ParseMode.HTML,
        )
        return

    # ── RETAKE_WRONG: practice round of just the questions missed in the ──
    # most recently finished lecture. Always awards XP (no already_attempted
    # gating — a retake isn't "the lecture", it's remedial practice), and
    # explicitly never touches the leaderboard/best-score file: see the
    # is_retake branch in _advance_lecture_session.
    if query.data == "retake_wrong":
        staged = RETAKE_STAGING.pop(user_id, None)
        if not staged or not staged["mids"]:
            await query.edit_message_text("⚠️ مفيش أسئلة غلط اتسجلت — يمكن خلصت المراجعة دي قبل كده.")
            return

        year = staged["year"]
        module, subject, lecture_key = staged["module"], staged["subject"], staged["lecture_key"]
        entry = QUIZ_INDEX[year].get(lecture_key, {})
        mids  = staged["mids"]

        auto_next = get_auto_next_enabled(user_id)

        poll_status_by_mid = {
            v["message_id"]: v
            for v in QUIZ_POLL_STATUS[year].values()
            if v["lecture"] == lecture_key
        }

        session = {
            "year": year, "module": module, "subject": subject, "lecture_key": lecture_key,
            "queue": list(mids), "current_poll_id": None, "current_correct_id": None,
            "total": len(mids), "answered": 0, "correct": 0,
            "mode": "auto" if auto_next else "batch",
            "pending_polls": {},
            "award_xp": True,      # retakes always earn XP
            "is_retake": True,     # ...but never touch the leaderboard/results file
            "poll_status_by_mid": poll_status_by_mid,
            "kind": "lecture",
        }
        LECTURE_SESSIONS[user_id] = session

        await query.edit_message_text(
            f"🔁 <b>مراجعة الأسئلة الغلط — {year_label(year)} — {module} - {subject}: {entry.get('name', lecture_key)}</b> — "
            f"{len(mids)} سؤال، "
            + ("هيتبعتولك واحد واحد 👇" if auto_next else "هيتبعتولك كلهم دلوقتي 👇"),
            parse_mode=ParseMode.HTML,
        )

        if auto_next:
            sent = await _deliver_next_lecture_question(context, user_id, session)
        else:
            session["total"] = 0
            sent_count = await _deliver_all_lecture_questions(context, user_id, session)
            session["total"] = sent_count
            sent = sent_count > 0
        if not sent:
            LECTURE_SESSIONS.pop(user_id, None)
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ الأسئلة دي اتحذفت من القناة، فاتشالت من قايمة المراجعة.",
            )
            await backup_quiz_to_channel(context, year)
        return

    # ── START MENU BUTTONS ──────────────────────────────────────
    if query.data == "back_home":
        await query.edit_message_text(
            f"{quizzy_block(QUIZZY_WELCOME_ART, random.choice(QUIZZY_WELCOME_LINES))}\n\n"
            "تحب تعمل أي؟!:",
            parse_mode=ParseMode.HTML,
            reply_markup=start_menu_keyboard(),
        )
        return

    if query.data == "menu_how":
        await query.edit_message_text(
            HOW_TO_USE_TEXT, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Back to Home", callback_data="back_home"),
            ]]),
        )
        return

    if query.data == "view_achievements":
        await _send_achievements(context, user_id, query.message, edit=True)
        return

    if query.data == "menu_mystats":
        await _send_mystats(context, user_id, query.message, edit=True)
        return

    if query.data == "year_leaderboard":
        # year_class is guaranteed set by this point — mandatory onboarding
        # (see _onboarding_gate) means no update reaches here otherwise.
        year_class = get_year_class(user_id)
        rows = _year_leaderboard(year_class)
        title = f"🏆 <b>Leaderboard — {year_class_label(year_class)}</b>"
        if not rows:
            text = f"{title}\n\nمفيش حد جاوب أسئلة محاضرات في السنة دي لسه."
        else:
            lines = [title]
            for i, r in enumerate(rows, 1):
                lines.append(
                    f"{i}# {html.escape(r['name'])} — {r['correct']} ✅ · {r['accuracy']:.0f}% دقة"
                )
            text = "\n".join(lines)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Back to Home", callback_data="back_home"),
            ]]),
        )
        return

    if query.data == "daily_quiz_leaderboard":
        # Kept as an alias for any older button still floating around in
        # chat history — the hub (which "daily_quiz" opens) now always
        # shows the leaderboard itself, so this just routes there too.
        await show_daily_quiz_menu(context, user_id, query.message)
        return

    if query.data == "menu_settings":
        AWAITING_NICKNAME.pop(user_id, None)
        await _send_settings(context, user_id, query.message, edit=True)
        return

    if query.data.startswith("settings_page:"):
        page = int(query.data.split(":")[1])
        await _send_settings(context, user_id, query.message, edit=True, page=page)
        return

    if query.data == "toggle_daily_notifs":
        entry = _get_settings_entry(user_id)
        entry["daily_notifs"] = not entry.get("daily_notifs", True)
        await save_settings()
        await backup_settings_to_channel(context)
        await _send_settings(context, user_id, query.message, edit=True, page=2)
        await _maybe_award_curious(context, user_id)
        return

    if query.data == "toggle_zikr":
        entry = _get_settings_entry(user_id)
        entry["zikr_reminders"] = not entry.get("zikr_reminders", False)
        await save_settings()
        await backup_settings_to_channel(context)
        await _send_settings(context, user_id, query.message, edit=True, page=2)
        await _maybe_award_curious(context, user_id)
        return

    if query.data == "edit_nickname":
        AWAITING_NICKNAME[user_id] = True
        await query.edit_message_text(
            "✏️ ابعت الاسم المستعار اللي عايزه (حتى 32 حرف).\n"
            "⚠️ استخدم اسم لائق 🙊 — هو اللي هيظهر في الـ Leaderboard وقدام زمايلك.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings"),
            ]]),
        )
        return

    # ── onboard_yc: / dqyc: — year/class picker tap, from onboarding ──
    # (right after the first-ever nickname save) or from the Daily Quiz
    # hub prompting for it first — year/class is set once here and can't
    # be changed afterwards (no Settings edit path anymore).
    if query.data.startswith("onboard_yc:") or query.data.startswith("dqyc:"):
        prefix, year_class = query.data.split(":")
        is_onboarding = (prefix == "onboard_yc")
        is_daily_quiz = (prefix == "dqyc")
        if year_class not in YEAR_CLASS_NUMBER:
            await query.edit_message_text("⚠️ الاختيار ده مش متاح.")
            return
        entry = _get_settings_entry(user_id)
        entry["year_class"] = year_class
        await save_settings()
        await backup_settings_to_channel(context)
        if is_onboarding:
            # Quizzy's year quip is no longer a chat message — it went out
            # as a toast on this very tap (see _tap_toast /
            # ONBOARDING_YEAR_QUIPS), so only the confirmation and the
            # bully question remain in the chat. The bully question's two
            # buttons lead to the "just kidding" step — see onboard_bully below.
            await query.edit_message_text(
                f"✅ تمام، {year_class_label(year_class)}.",
            )
            await context.bot.send_message(
                chat_id=user_id,
                text="Do you want me to bully you when you get questions wrong?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("What???", callback_data="onboard_bully:what"),
                    InlineKeyboardButton("No 😭",   callback_data="onboard_bully:no"),
                ]]),
            )
        elif is_daily_quiz:
            await show_daily_quiz_menu(context, user_id, query.message)
        else:
            await _send_settings(context, user_id, query.message, edit=True)
        return

    # ── onboarding steps after the year pick ─────────────────────────
    # bully question -> "just kidding" (onboard_bully) -> either door
    # (onboard_how / onboard_where) -> the admin's special message ->
    # onboard_go, the actual finish line into the real main menu.
    # Either answer to the bully question ("What???" / "No 😭") gets the
    # same follow-up — the choice isn't stored or acted on, it's just the
    # joke's setup. Edited in place so the question doesn't linger.
    if query.data.startswith("onboard_bully:"):
        await query.edit_message_text(
            "Haha i am just kidding (maybe)",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("what is this place?! 🙂", callback_data="onboard_how"),
                InlineKeyboardButton("Where are we?! 🙃",       callback_data="onboard_where"),
            ]]),
        )
        return

    # BOTH "what is this place?! 🙂" (onboard_how) and "Where are we?! 🙃"
    # (onboard_where) deliver the same thing: the admin's special welcome
    # message, filed in the storage vault under ONBOARDING_STORAGE_KEY (see
    # _send_onboarding_special). They're two differently-worded doors into
    # the same room. The 🗣️🗣️🔥 يلا بينا button goes on the LAST message
    # sent, since a copied vault item can't carry its own buttons — see
    # _send_onboarding_special. (The How To Use text is no longer part of
    # onboarding; it's still on the main menu's 🦦 How To Use button.)
    if query.data in ("onboard_how", "onboard_where"):
        try:
            await query.edit_message_reply_markup(reply_markup=None)   # buttons are one-shot
        except Exception:
            pass
        await _send_onboarding_special(context, user_id)
        return

    if query.data == "onboard_go":
        # The random Quizzy welcome line already showed as a toast on this
        # tap (see _tap_toast), so the menu message itself is just
        # the greeting — no ASCII cat block repeated underneath it.
        nickname = get_nickname(user_id)
        greeting = f"يا {html.escape(nickname)}! " if nickname else ""
        await query.edit_message_text(
            f"{greeting}تحب تعمل أي؟!:",
            parse_mode=ParseMode.HTML,
            reply_markup=start_menu_keyboard(),
        )
        return

    if query.data in ("toggle_reactions", "toggle_auto_next", "toggle_randomize", "toggle_achievement_notifs", "toggle_spaced_repetition"):
        key = {
            "toggle_reactions": "reactions",
            "toggle_auto_next": "auto_next",
            "toggle_randomize": "randomize",
            "toggle_achievement_notifs": "achievement_notifs",
            "toggle_spaced_repetition": "spaced_repetition",
        }[query.data]
        page = 2 if key in ("reactions", "achievement_notifs") else 1
        entry = _get_settings_entry(user_id)
        entry[key] = not entry.get(key, True)
        await save_settings()
        await backup_settings_to_channel(context)
        # Spaced Repetition only ever fires in auto-next mode (see
        # _maybe_deliver_spaced_repetition) — warn right away if this
        # toggle just created that mismatch — shown as a toast (_tap_toast),
        # on top of the persistent warning line _send_settings shows while
        # it's in effect.
        # (the mismatch toast for this tap was already shown by _tap_toast)
        await _send_settings(context, user_id, query.message, edit=True, page=page)
        await _maybe_award_curious(context, user_id)
        return

    if query.data == "toggle_question_timer":
        # 3-way cycle: Off -> 60s -> 30s -> Off
        entry = _get_settings_entry(user_id)
        current = entry.get("question_timer", 0)
        entry["question_timer"] = {0: 60, 60: 30, 30: 0}.get(current, 0)
        await save_settings()
        await backup_settings_to_channel(context)
        await _send_settings(context, user_id, query.message, edit=True)
        await _maybe_award_curious(context, user_id)
        return

    # ── Settings: Clear Mistake Bank (with confirmation) ──
    # MISTAKES_BANK holds every user's entries in one file, but each entry
    # is tagged with its owner (see MISTAKES BANK schema note), so this
    # action only ever touches the tapping user's own entries — and, when
    # an admin has set a /daily_module scope, only the ones in that
    # module (other modules' mistakes are left alone). With no scope set
    # it clears all of the user's mistakes, as before. Asks for an
    # explicit tap-to-confirm before wiping them, since it's destructive.
    # The confirm screen, the toast and the deletion all use
    # _scoped_mistakes_bank, so the number the user was shown is the
    # number that gets removed.
    if query.data == "clear_mistakes_bank_ask":
        count = len(_scoped_mistakes_bank(user_id))
        if not count:
            return   # "already empty" toast was shown by _tap_toast
        scope = get_daily_quiz_scope()
        scope_line = f"📚 {year_label(scope['year'])} — {module_label(scope['module'])}\n\n" if scope else ""
        await query.edit_message_text(
            f"⚠️ <b>متأكد إنك عايز تمسح بنك الأخطاء بتاعك؟</b>\n\n"
            f"{scope_line}"
            f"هيتمسح <b>{count}</b> سؤال، والعملية دي مش هترجع تاني.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 أيوه، امسح", callback_data="clear_mistakes_bank_yes")],
                [InlineKeyboardButton("🔙 لأ، رجّعني", callback_data="menu_settings")],
            ]),
        )
        return

    if query.data == "clear_mistakes_bank_yes":
        # Remove only this user's entries in the current /daily_module
        # scope (all of theirs if no scope is set) — the same set the
        # confirm screen counted. Matched by object identity, not just by
        # count, so the removal is exact even if the bank changed between
        # the confirm screen and this tap.
        doomed = {id(m) for m in _scoped_mistakes_bank(user_id)}
        MISTAKES_BANK[:] = [m for m in MISTAKES_BANK if id(m) not in doomed]
        # Patch this user's bucket in the per-user index to match. Other
        # modules' entries stay in it, so it can't just be popped anymore.
        remaining = [m for m in _MISTAKES_BY_USER.get(user_id, []) if id(m) not in doomed]
        if remaining:
            _MISTAKES_BY_USER[user_id] = remaining
        else:
            _MISTAKES_BY_USER.pop(user_id, None)
        await save_mistakes_bank()
        await backup_mistakes_bank_to_channel(context)
        # ("✅ N cleared" toast was shown by _tap_toast, which counts this
        # same set of entries before they're removed here.)
        await _send_settings(context, user_id, query.message, edit=True)
        return

    # ── /quiz_delete confirmation (admin) ────────────────────────────
    # PENDING_QUIZ_DELETE[admin_id] was set by quiz_delete_cmd right
    # before showing the confirm/cancel buttons — same tap-to-confirm
    # pattern as Clear Mistake Bank above, keyed by (year, lecture_key)
    # rather than position so it stays correct even if the index shifted
    # between the confirm screen and this tap.
    if query.data == "quizdel_yes":
        if not is_admin(update):
            await query.answer(MSG_ADMIN_ONLY, show_alert=True)
            return
        pending = PENDING_QUIZ_DELETE.pop(user_id, None)
        if not pending:
            await query.answer("⚠️ الطلب ده مش متاح دلوقتي — جرب /quiz_delete تاني.", show_alert=True)
            return
        year, key = pending
        index = QUIZ_INDEX[year]
        if key not in index:
            await query.edit_message_text("⚠️ المحاضرة دي اتشالت أو اتغيرت أصلاً.")
            return
        removed = index.pop(key)
        await save_quiz_index(year)
        if QUIZ_STATE[year].get("current_lecture") == key:
            QUIZ_STATE[year]["current_lecture"] = None
            await save_quiz_state(year)
        stale_polls = [pid for pid, v in QUIZ_POLL_STATUS[year].items() if v["lecture"] == key]
        for pid in stale_polls:
            QUIZ_POLL_STATUS[year].pop(pid, None)
        await save_quiz_poll_status(year)
        await backup_quiz_to_channel(context, year)
        await query.edit_message_text(
            f"🗑 اتشالت محاضرة من {year_label(year)}: {removed['module']} - {removed['subject']}: {removed['name']}\n"
            "(الرسايل نفسها لسه موجودة في القناة — احذفهم يدوي لو عايز)"
        )
        return

    if query.data == "quizdel_no":
        PENDING_QUIZ_DELETE.pop(user_id, None)
        await query.edit_message_text("🔙 اتلغى — المحاضرة لسه موجودة.")
        return

    # ── /reset_analytics confirmation (admin) ────────────────────────
    if query.data == "reset_analytics_yes":
        if not is_admin(update):
            await query.answer(MSG_ADMIN_ONLY, show_alert=True)
            return
        global _analytics_backup_msg_id, _analytics_dirty
        ANALYTICS.clear()
        await save_analytics()
        _analytics_dirty = False   # disk now matches memory — nothing left for the periodic flush to do
        if _analytics_backup_msg_id and ANALYTICS_GROUP_ID:
            try:
                await context.bot.delete_message(
                    chat_id=ANALYTICS_GROUP_ID,
                    message_id=_analytics_backup_msg_id,
                )
            except Exception:
                pass
        _analytics_backup_msg_id = None
        await query.edit_message_text("🗑 Analytics wiped — local file cleared and backup deleted.")
        return

    if query.data == "reset_analytics_no":
        await query.edit_message_text("🔙 اتلغى — الـ Analytics لسه زي ما هي.")
        return

    if query.data == "menu_quizzes":
        # Same as typing /quiz — sends a fresh message (not an edit) so the
        # welcome message with its buttons stays intact above it.
        years = configured_years()
        if not years:
            await query.message.reply_text("📭 مفيش سنين متاحة دلوقتي.")
            return
        view = _locked_year_modules_view(user_id, years)
        if view:
            text, markup = view
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
            return
        buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"yr:{y}")] for y in years]
        buttons.append([InlineKeyboardButton("Search Content 🔎", callback_data="search_pick_year")])
        await query.message.reply_text(
            "📚 <b>اختار السنة:</b>", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data == "daily_quiz":
        await show_daily_quiz_menu(context, user_id, message=query.message)
        return

    if query.data == "daily_quiz_begin":
        # The Quizzy toast for this tap ("already done today" / late night)
        # was already shown by _tap_toast at the top of button_handler.
        await start_daily_quiz(context, user_id, message=query.message)
        return

    # ── 🧠 Mistakes Bank menu button ──────────────────────────────
    if query.data == "mistakes_bank_menu":
        scope = get_daily_quiz_scope()
        count = len(_scoped_mistakes_bank(user_id))
        # The Quizzy toast for an empty / very full bank was already shown
        # by _tap_toast at the top of button_handler.
        scope_line = f"📚 {year_label(scope['year'])} — {module_label(scope['module'])}\n\n" if scope else ""
        text = f"🧠 <b>بنك الأخطاء</b>\n\n{scope_line}عدد الأسئلة المسجلة: <b>{count}</b>"
        buttons = []
        if count:
            buttons.append([InlineKeyboardButton("🔁 Retake Questions", callback_data="mistakes_retake")])
        buttons.append([InlineKeyboardButton("🏠 Back to Home", callback_data="back_home")])
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if query.data == "mistakes_retake":
        await start_mistakes_retake(context, user_id, message=query.message)
        return

    # ── Admin: /daily_module picker (dqy:/dqm:/dq_scope_off) ─────────
    if query.data.startswith("dqy:"):
        if not is_admin(update):
            await query.edit_message_text(MSG_ADMIN_ONLY)
            return
        year = query.data.split(":")[1]
        if year not in configured_years():
            await query.edit_message_text("⚠️ السنة دي مش متاحة دلوقتي.")
            return
        modules = ready_modules(year)
        if not modules:
            await query.edit_message_text(f"📭 مفيش موديولات متظبطة لـ {year_label(year)} لسه.")
            return
        buttons = [[InlineKeyboardButton(module_label(m), callback_data=f"dqm:{year}:{i}")] for i, m in enumerate(modules)]
        buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="daily_module_years")])
        await query.edit_message_text(
            f"📚 <b>{year_label(year)}</b> — اختار الموديول:", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data == "daily_module_years":
        if not is_admin(update):
            await query.edit_message_text(MSG_ADMIN_ONLY)
            return
        years = configured_years()
        buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"dqy:{y}")] for y in years]
        scope = get_daily_quiz_scope()
        if scope:
            buttons.append([InlineKeyboardButton("🔓 شيل التحديد (رجّع كل المنهج)", callback_data="dq_scope_off")])
        await query.edit_message_text(
            "📚 <b>Daily Quiz — اختار الموديول اللي هيتحدد عليه:</b>",
            parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data.startswith("dqm:"):
        if not is_admin(update):
            await query.edit_message_text(MSG_ADMIN_ONLY)
            return
        _, year, mod_idx_str = query.data.split(":")
        mod_idx = int(mod_idx_str)
        modules = ready_modules(year)
        if mod_idx >= len(modules):
            await query.edit_message_text("⚠️ الموديول ده مش موجود دلوقتي.")
            return
        module = modules[mod_idx]
        await set_daily_quiz_scope(year, module)
        await backup_settings_to_channel(context)
        await query.edit_message_text(
            f"✅ Daily Quiz دلوقتي محدد على: {year_label(year)} — {module_label(module)}\n\n"
            f"(أسئلة الـ Daily Quiz لسنة {year_label(year)} هيتسحبوا من الموديول ده بس)",
            parse_mode=ParseMode.HTML,
        )
        return

    if query.data == "dq_scope_off":
        if not is_admin(update):
            await query.edit_message_text(MSG_ADMIN_ONLY)
            return
        await set_daily_quiz_scope(None, None)
        await backup_settings_to_channel(context)
        await query.edit_message_text("✅ اتشال التحديد — Daily Quiz دلوقتي بيسحب من المنهج كله تاني.")
        return

    # ── /report_issue — user sends a message, admin replies from
# REPORT_ISSUE_GROUP_ID, both sides visible on the same message, and the
# reporter can send follow-ups back into the same thread.
#
# Flow:
#   1. /report_issue -> AWAITING_REPORT_ISSUE[user_id] = time.time(), bot
#      asks for the text (optionally with a photo). Expires after
#      REPORT_ISSUE_AWAIT_TIMEOUT, and /cancel clears it early — either
#      way, a message sent after the wait lapsed is handled normally
#      instead of being swallowed as a report.
#   2. The user's next text (handle()) or photo (handle_image()) is
#      staged as a DRAFT (_stage_report_draft), not sent yet — they get a
#      preview back with "✅ ابعت" / "❌ إلغاء" buttons.
#   3. Tapping "✅ ابعت" (report_draft_send, in button_handler) calls
#      _submit_report, which does what step 2 used to do directly: posts
#      the card to REPORT_ISSUE_GROUP_ID with a "↩️ Reply" button, and
#      records it in REPORT_THREADS keyed by that group message's id —
#      this id is also shown on the card itself as the "Reply ID". A
#      photo, if any, goes out as its own message right after, threaded
#      under the card via reply_to_message_id (see _submit_report's
#      docstring for why it's not just the card's caption). Rejected if
#      more than REPORT_DRAFT_CONFIRM_TIMEOUT has passed since the draft
#      was staged. "❌ إلغاء" (report_draft_cancel) just discards it.
#   4. Admin replies one of two ways:
#        a) Type "-Reply <id> <text>" directly in the group. Preferred —
#           parsed straight out of the message text with no per-user
#           state, so it works even if the group has "remain anonymous"
#           enabled for admins (a typed message then arrives with
#           effective_user = GroupAnonymousBot, not the admin's real id,
#           which silently breaks any flow keyed on real_uid).
#        b) Tap the "↩️ Reply" button -> AWAITING_REPORT_REPLY[admin_id]
#           = {...}, bot asks for the text in the group, and the admin's
#           next message there is picked up in handle(). Kept as a
#           convenience alongside (a), with a same-chat fallback lookup
#           for the anonymous-admin case — see the comment at that check.
#      Either way: appended to the thread via _append_report_message, then
#      _refresh_report_thread posts the FULL thread as a fresh message to
#      BOTH sides (admin group card + reporter's DM, each with its own
#      Reply/Close or Reply button) and collapses whichever full-thread
#      message each side had before — see that function's docstring for
#      why a plain edit isn't enough (no push notification) and why the
#      old message is collapsed rather than left in place (no duplicate
#      thread dumps piling up).
#   5. Reporter taps their own "↩️ Reply" -> AWAITING_USER_FOLLOWUP[user_id]
#      = {...}; their next DM text is appended to the same thread (shown
#      to the admin under their name) and _refresh_report_thread runs
#      again, same as step 4. Blocked once the thread is closed.
#   6. Close just strips the buttons and marks the thread closed in place
#      (no new message on either side — it's not a reply) — no further
#      replies possible from that message (a re-tapped Reply, from either
#      side, is rejected with a toast/message).
#
# REPORT_THREADS is persisted the same way as MISTAKES_BANK: a local JSON
# file plus a pinned backup in REPORT_ISSUE_GROUP_ID, restored on startup
# (see restore_report_threads_from_channel). A restart mid-thread no
# longer loses the ability to keep replying to an old report — the thread
# reloads from the channel backup before polling starts. REPORT_DRAFTS
# (an unconfirmed draft between steps 2 and 3) is NOT persisted this way
# — it's short-lived, in-memory only, and a restart mid-draft just means
# the user re-does /report_issue, same as if the bot had been down when
# they first tried.
# ═══════════════════════════════════════════════════════════════
def _report_reply_keyboard(group_message_id: int, closed: bool) -> InlineKeyboardMarkup:
    if closed:
        return InlineKeyboardMarkup([[InlineKeyboardButton("🔒 Closed", callback_data="report_noop")]])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ Reply", callback_data=f"report_reply:{group_message_id}")],
        [InlineKeyboardButton("✅ Close", callback_data=f"report_close:{group_message_id}")],
    ])

def _report_thread_text(thread: dict, group_message_id: int) -> str:
    """Renders the full report message: the user's identity + original
    text, then every message after it (admin replies AND user follow-ups,
    see AWAITING_USER_FOLLOWUP) in chronological order. group_message_id
    is shown as the reply ID — the number to use with "-Reply <id> <text>"
    (see the plain-text handler in handle()) — distinct from the
    reporter's own Telegram ID shown just above it.

    thread["messages"] is the single source of truth for everything after
    the opening report: [{"from": "admin"|"user", "text": str}, ...] in
    the order they happened. Threads created before follow-ups existed
    only have the older "replies" list (admin-only, no "messages" key at
    all) — that's read here as a fallback so old threads still render,
    but nothing new is ever written to "replies" again; see
    _append_report_message."""
    lines = [
        "📩 <b>New issue report</b>",
        f"👤 {html.escape(thread['name'])}",
        f"🔗 @{html.escape(thread['username'])}" if thread.get("username") else "🔗 (no username)",
        f"🆔 Reporter ID: <code>{thread['user_id']}</code>",
        f"🔖 Reply ID: <code>{group_message_id}</code>  (use <code>-Reply {group_message_id} &lt;text&gt;</code>)",
        "",
        html.escape(thread["user_text"]),
    ]
    messages = thread.get("messages")
    if messages is None:
        # Pre-follow-up thread — every entry in "replies" was an admin
        # message; render it exactly as before.
        messages = [{"from": "admin", "text": r} for r in thread.get("replies", [])]
    for msg in messages:
        lines.append("")
        lines.append("➖➖➖➖➖➖➖➖")
        if msg["from"] == "admin":
            lines.append(f"👨‍💼 <b>Admin:</b>\n{html.escape(msg['text'])}")
        else:
            lines.append(f"👤 <b>{html.escape(thread['name'])}:</b>\n{html.escape(msg['text'])}")
    return "\n".join(lines)

def _append_report_message(thread: dict, sender: str, text: str) -> None:
    """Adds one message (sender is 'admin' or 'user') to the thread's
    unified timeline, migrating an old replies-only thread to the
    "messages" schema on first touch. See _report_thread_text for why."""
    if "messages" not in thread:
        thread["messages"] = [{"from": "admin", "text": r} for r in thread.get("replies", [])]
    thread["messages"].append({"from": sender, "text": text})

def _report_thread_text_for_user(thread: dict) -> str:
    """User-facing rendering of the same timeline _report_thread_text
    builds for the admin side — same messages, same order, but without
    the Reporter ID / Reply ID / "-Reply <id>" lines that only make
    sense in the admin group."""
    lines = ["📩 <b>مشكلتك:</b>", "", html.escape(thread["user_text"])]
    messages = thread.get("messages")
    if messages is None:
        messages = [{"from": "admin", "text": r} for r in thread.get("replies", [])]
    for msg in messages:
        lines.append("")
        lines.append("➖➖➖➖➖➖➖➖")
        if msg["from"] == "admin":
            lines.append(f"👨‍💼 <b>الأدمن:</b>\n{html.escape(msg['text'])}")
        else:
            lines.append(f"👤 <b>انت:</b>\n{html.escape(msg['text'])}")
    return "\n".join(lines)

async def _collapse_old_report_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int | None) -> None:
    """Shrinks a superseded report message once a fresh full-thread
    message has replaced it — deletion is tried first (cleanest), and
    only falls back to editing it down to a single '•' (buttons
    stripped, so a stale Reply/Close can't sit alongside the new
    message's own) if deletion isn't possible for any reason (message
    already gone, too old, permissions, etc.). message_id=None (nothing
    to collapse yet — the very first message on that side) is a no-op."""
    if message_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return
    except Exception:
        pass
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="•", reply_markup=None)
    except Exception:
        pass   # already gone, too old to edit, etc. — fine either way, the new message is what matters now

async def _refresh_report_thread(context: ContextTypes.DEFAULT_TYPE, group_message_id: int, thread: dict) -> bool:
    """Called right after _append_report_message, on every new reply or
    follow-up in either direction. Posts the FULL thread as a brand-new
    message to BOTH sides — admin group and reporter's DM — rather than
    editing the existing one in place: an edit doesn't trigger a push
    notification on Telegram, so a reply sitting in an already-read,
    silently-edited message is easy to miss entirely. Whichever
    full-thread message each side had before (if any) is then collapsed
    via _collapse_old_report_message, so old chat history doesn't turn
    into a wall of duplicate thread dumps — only the latest copy on each
    side is ever left full-length.

    group_message_id is the thread's STABLE key: REPORT_THREADS is keyed
    by the id of the very first card ever sent, and every "-Reply <id>",
    Reply button, and Close button always references that same stable id
    (via _report_reply_keyboard) — never whichever physical message
    happens to be showing it right now. So this is safe to call
    regardless of how many times the thread has already been refreshed.

    Returns whether the reporter's DM send succeeded — callers use this
    to warn the admin in-group when it didn't (e.g. the user blocked the
    bot); the thread itself is still updated and saved either way."""
    closed = thread.get("closed", False)

    # ── Admin-facing card, in REPORT_ISSUE_GROUP_ID ──
    try:
        sent = await context.bot.send_message(
            chat_id=REPORT_ISSUE_GROUP_ID,
            text=_report_thread_text(thread, group_message_id), parse_mode=ParseMode.HTML,
            reply_markup=_report_reply_keyboard(group_message_id, closed=closed),
        )
        await _collapse_old_report_message(context, REPORT_ISSUE_GROUP_ID, thread.get("latest_group_message_id"))
        thread["latest_group_message_id"] = sent.message_id
    except Exception as e:
        print("REPORT THREAD GROUP REFRESH FAILED:", e)

    # ── User-facing DM, to the reporter ──
    user_dm_ok = True
    try:
        sent = await context.bot.send_message(
            chat_id=thread["user_id"],
            text=_report_thread_text_for_user(thread), parse_mode=ParseMode.HTML,
            reply_markup=(
                None if closed else
                InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Reply", callback_data=f"report_user_reply:{group_message_id}")]])
            ),
        )
        await _collapse_old_report_message(context, thread["user_id"], thread.get("latest_user_message_id"))
        thread["latest_user_message_id"] = sent.message_id
    except Exception as e:
        print("REPORT THREAD USER DM FAILED:", e)   # e.g. the user blocked the bot
        user_dm_ok = False

    await save_report_threads()
    await backup_report_threads_to_channel(context)
    return user_dm_ok

def _report_draft_preview_text(text: str, has_photo: bool) -> str:
    photo_note = "\n📎 هيتبعت مع الصورة اللي بعتها." if has_photo else ""
    body = html.escape(text) if text else "<i>(من غير نص — صورة بس)</i>"
    return f"👀 <b>ده اللي هيتبعت للأدمن:</b>\n\n{body}{photo_note}\n\nتمام كده؟"

async def _stage_report_draft(update: Update, context: ContextTypes.DEFAULT_TYPE, real_uid: int, text: str, photo_file_id: str | None) -> None:
    """Parks the user's message as an unconfirmed draft instead of
    posting it straight to REPORT_ISSUE_GROUP_ID, and shows them a
    preview with Send/Cancel buttons (report_draft_send /
    report_draft_cancel in button_handler). Overwrites any earlier
    unconfirmed draft from the same user — only the latest one they
    typed can ever be sent."""
    REPORT_DRAFTS[real_uid] = {
        "text": (text or "").strip(),
        "photo_file_id": photo_file_id,
        "created_at": time.time(),
    }
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ ابعت", callback_data="report_draft_send"),
        InlineKeyboardButton("❌ إلغاء", callback_data="report_draft_cancel"),
    ]])
    await update.message.reply_text(
        _report_draft_preview_text(text, has_photo=bool(photo_file_id)),
        parse_mode=ParseMode.HTML, reply_markup=keyboard,
    )

async def _submit_report(context: ContextTypes.DEFAULT_TYPE, real_uid: int, tg_user, text: str, photo_file_id: str | None) -> bool:
    """Actually posts a confirmed draft to REPORT_ISSUE_GROUP_ID and
    records the thread — the part that used to run directly off the
    user's first message (see the AWAITING_REPORT_ISSUE block in
    handle()), now run only once they've tapped "✅ ابعت" on the
    preview. Returns whether it succeeded.

    The card itself is always a plain text message, even when a photo
    is attached — a photo goes out as its own message right after,
    threaded under the card via reply_to_message_id. Using the photo's
    caption for the growing thread text instead (admin replies, user
    follow-ups) would run into Telegram's 1024-char caption cap on any
    thread that gets a few exchanges long; a plain text card has no
    such limit. This first card is also the thread's first "latest"
    message on the group side — every reply after this one refreshes it
    via _refresh_report_thread instead of editing it in place."""
    name     = " ".join(p for p in (tg_user.first_name, tg_user.last_name) if p).strip() if tg_user else "?"
    username = tg_user.username if tg_user else None
    thread = {
        "user_id": real_uid, "name": name or "?", "username": username,
        "user_text": text, "photo_file_id": photo_file_id, "replies": [], "closed": False,
    }
    try:
        sent = await context.bot.send_message(
            chat_id=REPORT_ISSUE_GROUP_ID,
            text="📩 New issue report — loading…",   # placeholder; fixed up right below once we have the real id
            reply_markup=_report_reply_keyboard(0, closed=False),   # placeholder id, fixed up right below too
        )
    except Exception as e:
        print("REPORT ISSUE SEND FAILED:", e)
        return False
    # Both the displayed Reply ID and the keyboard's callback_data need
    # this message's own id, which we only get back after sending —
    # one edit to fix up both text and keyboard together.
    try:
        await context.bot.edit_message_text(
            chat_id=REPORT_ISSUE_GROUP_ID, message_id=sent.message_id,
            text=_report_thread_text(thread, sent.message_id), parse_mode=ParseMode.HTML,
            reply_markup=_report_reply_keyboard(sent.message_id, closed=False),
        )
    except Exception as e:
        print("REPORT ISSUE ID FIXUP FAILED:", e)
    if photo_file_id:
        try:
            await context.bot.send_photo(
                chat_id=REPORT_ISSUE_GROUP_ID, photo=photo_file_id,
                caption="📎 Attached to the report above.",
                reply_to_message_id=sent.message_id,
            )
        except Exception as e:
            print("REPORT ISSUE PHOTO FORWARD FAILED:", e)
    thread["latest_group_message_id"] = sent.message_id
    REPORT_THREADS[sent.message_id] = thread
    await save_report_threads()
    await backup_report_threads_to_channel(context)
    return True

async def report_issue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not REPORT_ISSUE_GROUP_ID:
        await update.message.reply_text("⚠️ الميزة دي مش متاحة دلوقتي.")
        return
    real_uid = update.effective_user.id if update.effective_user else update.effective_chat.id
    AWAITING_REPORT_ISSUE[real_uid] = time.time()
    await update.message.reply_text(
        "✏️ اكتب مشكلتك أو ملاحظتك في رسالة واحدة (تقدر تبعت صورة معاها كمان لو حابب)، "
        "وهعرضهالك الأول قبل ما تتبعت للأدمن.\n\n"
        "غيرت رأيك؟ ابعت /cancel."
    )

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bails out of a pending image that's waiting for its question, or
    (admin only) a /broadcast composer waiting on the message text, or a
    /report_issue that hasn't been sent/confirmed yet."""
    user_id = update.effective_chat.id
    was_doing_something = bool(PENDING_IMAGE.get(user_id))
    _clear_pending_image(user_id)
    if AWAITING_BROADCAST_MESSAGE.pop(user_id, None):
        was_doing_something = True
    if AWAITING_REPORT_ISSUE.pop(user_id, None) is not None:
        was_doing_something = True
    if REPORT_DRAFTS.pop(user_id, None) is not None:
        was_doing_something = True
    if AWAITING_SEARCH_QUERY.pop(user_id, None) is not None:
        was_doing_something = True
    if was_doing_something:
        await update.message.reply_text(MSG_CANCEL_DONE)
    else:
        await update.message.reply_text(MSG_CANCEL_NOTHING)

# ═══════════════════════════════════════════════════════════════
# START  (also wakes bot from sleep)
# ═══════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    SLEEPING.discard(chat_id)

    if chat_id not in USERS:
        USERS.add(chat_id)
        await save_users()
        await backup_storage_to_channel(context)

    real_uid = update.effective_user.id if update.effective_user else chat_id
    _update_telegram_name(real_uid, update.effective_user)
    nickname = get_nickname(real_uid)

    if nickname is None:
        # First-ever /start (no nickname on file yet, for this specific
        # person): ask for one before showing the main menu at all. Marked
        # "onboarding" (rather than True, same as the Settings ✏️ flow) so
        # the text handler knows to continue into the welcome menu
        # afterwards instead of bouncing back to the Settings screen.
        AWAITING_NICKNAME[real_uid] = "onboarding"
        await update.message.reply_text(
            quizzy_block(
                QUIZZY_AMAZED_ART,
                "Hello there! My name is Quizzy! what's your name? "
                "(Use an appropriate name or Quizzy will bite you 🙊 - you can change it again later )",
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if get_year_class(real_uid) not in YEAR_CLASS_NUMBER:
        # Nickname's set but they never finished picking a year/class
        # (or got interrupted mid-onboarding) — send them back to this
        # step instead of the main menu. Also mandatory, also permanent.
        await update.message.reply_text(
            "What Year/Class are you currently in?\n\n"
            "(⚠️ Set your class correctly, you can NOT change it again later ⚠️)",
            reply_markup=year_class_keyboard("onboard_yc"),
        )
        return

    greeting = f"يا {html.escape(nickname)}! "

    await update.message.reply_text(
        f"{quizzy_block(QUIZZY_WELCOME_ART, random.choice(QUIZZY_WELCOME_LINES))}\n\n"
        f"{greeting}تحب تعمل أي؟!:",
        parse_mode=ParseMode.HTML,
        reply_markup=start_menu_keyboard(),
    )

async def commands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/c — lists every command, admin-only ones only shown to the admin."""
    lines = ["📖 <b>Available commands:</b>\n"]
    lines.append("👤 <b>For everyone</b>")
    lines.append("/start — main menu")
    lines.append("/sleep — pauses the bot temporarily in this chat")
    lines.append("/mystats — your stats (day streak, accuracy, current module's subjects strongest → weakest, achievements)")
    lines.append("⚙️ Settings — from the /start menu: set your nickname")
    lines.append("/cancel — cancels whatever's currently in progress (pending image, etc.)")
    lines.append("/report_issue — send a message straight to the admin")
    lines.append("/quiz — browse lectures (year → module → subject → lecture) and pull their questions")
    lines.append("/time — current time, and when the next 💥Daily Quiz💥 push is")
    lines.append("/c — this list")

    if is_admin(update):
        lines.append("\n🔐 <b>Admin only</b>")
        lines.append("/admincheck")
        lines.append("/health")
        lines.append("/restore")
        lines.append("/broadcast &lt;message&gt;")
        lines.append("/ban &lt;ID&gt; &lt;hours&gt; &lt;reason&gt;")
        lines.append("/unban &lt;ID&gt;")
        lines.append("/backup_now")
        lines.append("/quiz_list &lt;year&gt;")
        lines.append("/quiz_delete &lt;year&gt; &lt;number&gt;")
        lines.append("/daily_module")
        lines.append("/edit_quiz, /quiz_edit")
        years_line = ", ".join(f"{y} ({year_label(y)})" for y in YEAR_ORDER)
        lines.append(f"    year keys: {years_line}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════════════════
# ADMIN HELPERS
# ═══════════════════════════════════════════════════════════════
def is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID

async def admincheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else "?"
    if is_admin(update):
        await update.message.reply_text(
            f"✅ <b>أنت الأدمن!</b>\n"
            f"🆔 Your ID: <code>{uid}</code>\n"
            f"👥 Total users: <b>{len(USERS)}</b>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            f"🚫 مش أدمن\n🆔 Your ID: <code>{uid}</code>",
            parse_mode=ParseMode.HTML,
        )

def _format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, rem   = divmod(seconds, 86400)
    hours, rem  = divmod(rem, 3600)
    minutes, _  = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only at-a-glance dashboard: is the bot up, how many users/
    lectures exist, whether each channel backup is currently trustworthy
    (RESTORE_OK — see the comment where it's defined: False there means
    that system's backups are actively being *refused* right now, not
    just "unchecked"), how many sessions are live in memory, how many
    unhandled errors hit the global error handler in the last 24h, and
    how long this process has been running.

    Two numbers here are explicitly since-last-restart, not lifetime,
    because their backing state is in-memory only (see _ERROR_LOG_TIMES
    and the LECTURE_SESSIONS/DAILY_QUIZ_SESSIONS/MISTAKES_RETAKE_SESSIONS
    dicts) — called out in the footer so a low error count right after a
    restart doesn't get misread as "no errors in the last day"."""
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    lecture_counts = {y: len(QUIZ_INDEX.get(y, {})) for y in YEAR_ORDER}

    backup_rows = [
        ("Analytics", RESTORE_OK.get("analytics", True)),
        ("Settings",  RESTORE_OK.get("settings", True)),
        ("Storage",   RESTORE_OK.get("storage", True)),
    ] + [
        (year_label(y), RESTORE_OK.get(f"quiz_{y}", True)) for y in YEAR_ORDER
    ] + [
        ("Mistakes",  RESTORE_OK.get("mistakes_bank", True)),
        ("Reports",   RESTORE_OK.get("report_threads", True)),
    ]
    label_width = max(len(label) for label, _ in backup_rows)
    backup_lines = "\n".join(
        f"{label.ljust(label_width)}  {'✅' if ok else '❌'}" for label, ok in backup_rows
    )

    active_sessions = len(LECTURE_SESSIONS) + len(DAILY_QUIZ_SESSIONS) + len(MISTAKES_RETAKE_SESSIONS)

    now = time.time()
    errors_24h = sum(1 for t in _ERROR_LOG_TIMES if now - t < 24 * 3600)

    uptime = _format_uptime(time.monotonic() - _BOT_STARTED_AT)

    lecture_lines = "\n".join(
        f"📚 {year_label(y)} lectures: {lecture_counts[y]}" for y in YEAR_ORDER
    )

    text = (
        f"🟢 Bot: ONLINE\n"
        f"👥 Users: {len(USERS)}\n"
        f"{lecture_lines}\n\n"
        f"💾 Backups:\n"
        f"<code>{backup_lines}</code>\n\n"
        f"⚠️ Active sessions: {active_sessions}\n"
        f"❌ Errors last 24h: {errors_24h}\n"
        f"⏱ Uptime: {uptime}\n\n"
        f"<i>Sessions and error count are since the last restart — both reset when the process does.</i>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════════════════
# /restore — admin-only, on-demand re-run of the same pinned-backup
# restore logic that normally only happens once at startup, per system.
# Doesn't touch anything backup-related beyond what restore_*_from_channel
# already does: reads whatever's CURRENTLY PINNED in that system's
# channel (the Bot API has no way to list or browse older, unpinned
# backup messages — see the backup_*_to_channel functions' comments on
# why old backups are kept, just unpinned, instead of deleted) and
# overwrites the local file with it, same as boot does.
#
# Practical use: if a bad backup got pinned (e.g. RESTORE_OK was False
# for a while and then something manually re-pinned an old/wrong file),
# re-pin the RIGHT backup message in the channel yourself first, THEN run
# /restore for that system — it'll pick up whatever is pinned at the
# moment you run it, not necessarily the newest one ever taken.
# ═══════════════════════════════════════════════════════════════
_RESTORE_TARGETS = {
    "analytics":       ("Analytics",        lambda app: restore_analytics_from_channel(app)),
    "settings":        ("Settings",         lambda app: restore_settings_from_channel(app)),
    "storage":         ("Storage",          lambda app: restore_storage_from_channel(app)),
    "quiz_y1":         ("Quiz Index — Y1",  lambda app: restore_quiz_from_channel(app, "y1")),
    "quiz_y2":         ("Quiz Index — Y2",  lambda app: restore_quiz_from_channel(app, "y2")),
    "quiz_y3":         ("Quiz Index — Y3",  lambda app: restore_quiz_from_channel(app, "y3")),
    "mistakes_bank":   ("Mistakes Bank",    lambda app: restore_mistakes_bank_from_channel(app)),
    "report_threads":  ("Report Threads",   lambda app: restore_report_threads_from_channel(app)),
}

# ── RESET actions — "create a new/blank file" for one system ─────────
# Offered only from the /restore flow, only when restore_go finds
# nothing pinned to restore from (status "no_backup"). Each action
# wipes the system's in-memory state to blank, saves that blank state
# locally, then pushes it as a brand-new pinned backup — so afterwards
# there IS something pinned, exactly as if the system had started
# fresh. context here is the update's ContextTypes.DEFAULT_TYPE (has
# .bot), same as what the backup_*_to_channel functions expect —
# not the `app` that the restore functions above take.
async def _reset_analytics_system(context):
    ANALYTICS.clear()
    await save_analytics()
    RESTORE_OK["analytics"] = True
    await backup_analytics_to_channel(context)

async def _reset_settings_system(context):
    SETTINGS.clear()
    await save_settings()
    RESTORE_OK["settings"] = True
    await backup_settings_to_channel(context)

async def _reset_storage_system(context):
    USERS.clear()
    STORAGE_INDEX.clear()
    await save_users()
    await save_storage_index()
    RESTORE_OK["storage"] = True
    await backup_storage_to_channel(context)

async def _reset_quiz_system(context, year: str):
    QUIZ_INDEX[year].clear()
    QUIZ_STATE[year].clear()
    QUIZ_POLL_STATUS[year].clear()
    await save_quiz_index(year)
    await save_quiz_state(year)
    await save_quiz_poll_status(year)
    RESTORE_OK[f"quiz_{year}"] = True
    await backup_quiz_to_channel(context, year)

async def _reset_mistakes_bank_system(context):
    MISTAKES_BANK.clear()
    _reindex_mistakes_bank()
    await save_mistakes_bank()
    RESTORE_OK["mistakes_bank"] = True
    await backup_mistakes_bank_to_channel(context)

async def _reset_report_threads_system(context):
    REPORT_THREADS.clear()
    await save_report_threads()
    RESTORE_OK["report_threads"] = True
    await backup_report_threads_to_channel(context)

_RESET_ACTIONS = {
    "analytics":       lambda context: _reset_analytics_system(context),
    "settings":        lambda context: _reset_settings_system(context),
    "storage":         lambda context: _reset_storage_system(context),
    "quiz_y1":         lambda context: _reset_quiz_system(context, "y1"),
    "quiz_y2":         lambda context: _reset_quiz_system(context, "y2"),
    "quiz_y3":         lambda context: _reset_quiz_system(context, "y3"),
    "mistakes_bank":   lambda context: _reset_mistakes_bank_system(context),
    "report_threads":  lambda context: _reset_report_threads_system(context),
}

def _restore_picker_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"restore_go:{key}")]
        for key, (label, _) in _RESTORE_TARGETS.items()
    ]
    return InlineKeyboardMarkup(rows)

async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return
    await update.message.reply_text(
        "🔄 اختار الـ system اللي عايز تعمله restore من آخر نسخة مثبتة (pinned) في القناة بتاعته:",
        reply_markup=_restore_picker_keyboard(),
    )

# ═══════════════════════════════════════════════════════════════
# BROADCAST COMMAND  (admin only)
# ═══════════════════════════════════════════════════════════════
# Interactive composer: pick an audience, set a message, preview it
# exactly as recipients will see it, check the live estimated-recipient
# count, then SEND with a progress bar + rolling ETA. State lives in
# BROADCAST_DRAFTS (see the STATE section near the top for the field
# shapes) — one in-progress draft per admin, RAM-only like every other
# AWAITING_*/PENDING_* flow in this file.
BROADCAST_ACTIVE_WINDOW_DAYS = 7   # "Active users" = engaged with the bot at least once in this many days
BROADCAST_PROGRESS_EDIT_INTERVAL = 2.0   # seconds between progress-bar edits — Telegram's edit-rate limits are per-chat, so this only needs to be sane, not aggressive
BROADCAST_SEND_DELAY             = 0.05  # seconds between sends — a light throttle against Telegram's global flood limits on a big broadcast

BROADCAST_AUDIENCE_ORDER  = ["all", "y1", "y2", "y3", "active", "inactive"]
BROADCAST_AUDIENCE_LABELS = {
    "all": "All users", "y1": "Year 1", "y2": "Year 2", "y3": "Year 3",
    "active": "Active users", "inactive": "Inactive users",
}

def _broadcast_audience_user_ids(audience: str) -> list:
    """Resolves an audience key to the user_ids it currently matches.
    y1/y2/y3 read each user's own locked year_class (get_year_class) —
    mandatory since onboarding (see _onboarding_gate), so this is a
    straight equality check, no guessing needed. active/inactive split
    on whether ANALYTICS' last_active_date falls within
    BROADCAST_ACTIVE_WINDOW_DAYS of today (UTC, matching _today()/
    _record_activity elsewhere) — a user who's never been active at all
    counts as inactive."""
    if audience == "all":
        return list(USERS)
    if audience in ("y1", "y2", "y3"):
        return [uid for uid in USERS if get_year_class(uid) == audience]
    if audience in ("active", "inactive"):
        from datetime import timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=BROADCAST_ACTIVE_WINDOW_DAYS)).strftime("%Y-%m-%d")
        def _is_active(uid):
            last = ANALYTICS.get(str(uid), {}).get("last_active_date")
            return bool(last) and last >= cutoff
        want_active = (audience == "active")
        return [uid for uid in USERS if _is_active(uid) == want_active]
    return []

def _broadcast_composer_view(admin_id: int) -> tuple:
    """Renders the /broadcast composer screen: an audience picker
    (radio-style — one selected at a time, marked with ✅), the current
    message (or a prompt to set one), and a live estimated-recipient
    count for whichever audience is currently selected. Shared by
    broadcast_cmd and every bcaud:/bcmsg/bccancel button in
    button_handler so the screen re-renders identically no matter which
    one triggered it."""
    draft    = BROADCAST_DRAFTS.setdefault(admin_id, {"audience": "all", "text": None})
    audience = draft["audience"]
    text     = draft["text"]
    count    = len(_broadcast_audience_user_ids(audience))

    lines = ["📡 <b>BROADCAST</b>", "", "👥 <b>Audience</b>"]
    for i, key in enumerate(BROADCAST_AUDIENCE_ORDER):
        branch = "└─" if i == len(BROADCAST_AUDIENCE_ORDER) - 1 else "├─"
        mark   = "✅ " if key == audience else ""
        lines.append(f"{branch} {mark}{BROADCAST_AUDIENCE_LABELS[key]}")
    lines.append("")
    lines.append("📝 <b>Message</b>")
    if text:
        preview = html.escape(text[:200]) + ("…" if len(text) > 200 else "")
        lines.append(preview)
    else:
        lines.append("<i>⚠️ لسه مفيش رسالة — دوس ✏️ Set Message تحت</i>")
    lines.append("")
    lines.append(f"📊 <b>Estimated:</b> {count:,} recipient(s)")

    buttons, row = [], []
    for key in BROADCAST_AUDIENCE_ORDER:
        mark  = "✅ " if key == audience else ""
        row.append(InlineKeyboardButton(f"{mark}{BROADCAST_AUDIENCE_LABELS[key]}", callback_data=f"bcaud:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    msg_row = [InlineKeyboardButton("✏️ Set Message", callback_data="bcmsg")]
    if text:
        msg_row.append(InlineKeyboardButton("👁 Preview", callback_data="bcpreview"))
    buttons.append(msg_row)

    action_row = []
    if text and count:   # SEND only offered once there's actually something, to someone, to send
        action_row.append(InlineKeyboardButton("🚀 SEND", callback_data="bcsend"))
    action_row.append(InlineKeyboardButton("❌ CANCEL", callback_data="bccancel"))
    buttons.append(action_row)

    return "\n".join(lines), InlineKeyboardMarkup(buttons)

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return
    admin_id = update.effective_user.id

    # Old shortcut still works: /broadcast <message>, or reply to a
    # message with /broadcast — pre-fills the composer's message so the
    # admin doesn't have to retype it via ✏️ Set Message. Audience stays
    # whatever it was last set to (defaulting to "all" the first time).
    prefilled = None
    if context.args:
        prefilled = " ".join(context.args)
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        prefilled = update.message.reply_to_message.text

    draft = BROADCAST_DRAFTS.setdefault(admin_id, {"audience": "all", "text": None})
    if prefilled:
        draft["text"] = prefilled
    AWAITING_BROADCAST_MESSAGE.pop(admin_id, None)

    body, markup = _broadcast_composer_view(admin_id)
    await update.message.reply_text(body, parse_mode=ParseMode.HTML, reply_markup=markup)

async def _send_broadcast(context: ContextTypes.DEFAULT_TYPE, status_message, audience: str, text: str, recipients: list) -> None:
    """Sends text to every id in recipients, editing status_message into
    a live progress bar + rolling ETA (re-estimated from the actual
    send rate so far, refreshed at most every
    BROADCAST_PROGRESS_EDIT_INTERVAL seconds) and finishing with the
    same success/failed/blocked summary the old one-shot /broadcast
    used to show, plus the audience and total duration."""
    total = len(recipients)
    success, failed, blocked = 0, 0, []
    started   = time.monotonic()
    last_edit = 0.0

    def _bar(done: int) -> str:
        filled = int((done / total) * 20) if total else 20
        return "█" * filled + "░" * (20 - filled)

    def _fmt_duration(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"

    async def _update_progress(done: int, force: bool = False):
        nonlocal last_edit
        now = time.monotonic()
        if not force and now - last_edit < BROADCAST_PROGRESS_EDIT_INTERVAL:
            return
        last_edit = now
        elapsed = now - started
        rate    = done / elapsed if elapsed > 0 else 0
        eta     = (total - done) / rate if rate > 0 else 0
        pct     = int(done / total * 100) if total else 100
        try:
            await status_message.edit_text(
                f"📡 <b>بيتبعت...</b>\n\n"
                f"[{_bar(done)}] {pct}%\n"
                f"{done}/{total} — ⏳ متبقي تقريبًا {_fmt_duration(eta)}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass   # a failed progress-bar edit should never interrupt the actual send loop below

    await _update_progress(0, force=True)

    for i, uid in enumerate(recipients, 1):
        try:
            await context.bot.send_message(chat_id=uid, text=text, parse_mode=ParseMode.HTML)
            success += 1
        except Forbidden:
            # The user actually blocked the bot (or deleted their account) —
            # this is the only case where removing them from USERS is safe.
            failed += 1
            blocked.append(uid)
        except Exception as e:
            # Any other error (network blip, rate limit, Telegram hiccup) is
            # NOT proof the user blocked us — keep them in USERS so a
            # transient failure doesn't silently and permanently unsubscribe
            # a real, still-active user.
            failed += 1
            print(f"Broadcast failed for {uid} (not removed — not a block):", e)
        await _update_progress(i)
        if BROADCAST_SEND_DELAY:
            await asyncio.sleep(BROADCAST_SEND_DELAY)

    if blocked:
        for uid in blocked:
            USERS.discard(uid)
        await save_users()
        await backup_storage_to_channel(context)

    summary = (
        f"✅ <b>Broadcast اتبعت!</b>\n\n"
        f"👥 الجمهور: <b>{BROADCAST_AUDIENCE_LABELS.get(audience, audience)}</b>\n"
        f"📨 المستهدفين: <b>{total}</b>\n"
        f"✔️ نجح: <b>{success}</b>\n"
        f"❌ فشل / بلوك: <b>{failed}</b>\n"
        f"⏱ المدة: <b>{_fmt_duration(time.monotonic() - started)}</b>"
    )
    if blocked:
        summary += f"\n🗑 تم حذف {len(blocked)} يوزر بلوك البوت من القائمة"
    try:
        await status_message.edit_text(summary, parse_mode=ParseMode.HTML)
    except Exception:
        pass

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /ban <user_id> <hours> <reason> — blocks that user from
    using the bot for the given number of hours. What's actually saved
    is the *unban* moment itself (banned_until, an epoch timestamp in
    their settings — see get_ban_info), not the duration, so it's a
    plain comparison against time.time() on every update (see
    _ban_gate) rather than needing a scheduled unban job. A second
    /ban on the same user just overwrites banned_until/ban_reason."""
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    args = context.args
    if len(args) < 3:
        await update.message.reply_text(
            "⚠️ استخدام:\n<code>/ban ID عدد_الساعات السبب</code>\n"
            "مثال: <code>/ban 123456789 24 سبام</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        target_id = int(args[0])
        hours     = float(args[1])
    except ValueError:
        await update.message.reply_text("⚠️ الـ ID وعدد الساعات لازم يكونوا أرقام.")
        return
    if hours <= 0:
        await update.message.reply_text("⚠️ عدد الساعات لازم يكون أكبر من صفر.")
        return

    reason = " ".join(args[2:])
    until  = time.time() + hours * 3600

    entry = _get_settings_entry(target_id)
    entry["banned_until"] = until
    entry["ban_reason"]   = reason
    await save_settings()
    await backup_settings_to_channel(context)

    until_label = datetime.fromtimestamp(until, DAILY_QUIZ_TZ).strftime("%Y-%m-%d %I:%M %p")
    await update.message.reply_text(
        f"🚫 <b>{target_id}</b> اتعمله بان لمدة {hours:g} ساعة.\n"
        f"⏰ هيترفع البان: {until_label}\n"
        f"📝 السبب: {html.escape(reason)}",
        parse_mode=ParseMode.HTML,
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                f"🚫 اتعمللك بان من البوت لمدة {hours:g} ساعة.\n"
                f"📝 السبب: {html.escape(reason)}\n"
                f"⏰ هيترفع البان: {until_label}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass  # they may have blocked the bot, or never opened a DM with it — not fatal to the ban itself

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /unban <user_id> — clears banned_until/ban_reason early,
    instead of waiting out the timer set by /ban. Safe to run on someone
    who isn't currently banned (or whose ban already lifted on its own)
    — it just reports that and does nothing further."""
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "⚠️ استخدام:\n<code>/unban ID</code>", parse_mode=ParseMode.HTML,
        )
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ الـ ID لازم يكون رقم.")
        return

    until, _ = get_ban_info(target_id)
    if until is None:
        await update.message.reply_text(f"ℹ️ <b>{target_id}</b> مش متبنن أصلاً.", parse_mode=ParseMode.HTML)
        return

    entry = _get_settings_entry(target_id)
    entry["banned_until"] = None
    entry["ban_reason"]   = None
    await save_settings()
    await backup_settings_to_channel(context)

    await update.message.reply_text(f"✅ اتشال البان عن <b>{target_id}</b>.", parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="✅ اتشال البان عنك — تقدر تستخدم البوت تاني.",
        )
    except Exception:
        pass  # same best-effort DM as ban_cmd — not fatal if it fails

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
ACHIEVEMENT_CATEGORY_LABEL = {
    "questions_answered": "📚 Questions Answered",
    "correct_streak":     "🔥 Correct Streak",
    "lectures_completed": "📖 Lectures Completed",
    "xp_levels":          "⭐ XP / Levels",
    "daily_quiz":         "📅 Daily Quiz",
    "daily_streak":       "🔥 Daily Streak",
    "achievement_collector": "🔍 Achievement Collector",
}

async def _send_achievements(context: ContextTypes.DEFAULT_TYPE, user_id: int, reply_target, edit: bool = False) -> None:
    """Full achievements breakdown — every tiered category (all its tiers,
    varies by category — see ACHIEVEMENTS) plus the one-off Extras
    section. Unlocked ones show their real name/threshold/description;
    locked ones are masked with ???? (mystery-box style — no spoilers on
    what's still ahead). Shared by the /mystats 'Achievements' button
    (only entry point for now). reply_target is a Message — edit=True
    rewrites it in place (button flow); edit=False sends a fresh reply
    (there's nothing bot-owned to edit yet, e.g. a freshly typed
    command)."""
    entry = _get_entry(user_id)
    ach   = entry.get("achievements", {})

    lines = ["🏆 <b>كل الإنجازات</b>\n"]
    for key, tiers in ACHIEVEMENTS.items():
        current = ach.get(key, 0)
        label   = ACHIEVEMENT_CATEGORY_LABEL.get(key, key)
        lines.append(f"{label} ({current}/{len(tiers)})")
        for i, (threshold, name, xp_bonus, emoji, quip) in enumerate(tiers):
            tier = i + 1
            if tier <= current:
                name = _personalize_ach_text(name, user_id)
                quip = _personalize_ach_text(quip, user_id)
                if key == "achievement_collector":
                    lines.append(f"  ✅ {name} — x{xp_bonus} XP")
                else:
                    lines.append(f"  ✅ {name} — {threshold}+")
                lines.append(f"     💬 <i>{quip}</i>")
            else:
                lines.append("  🔒 ???????? — ???")
        lines.append("")

    extras = ach.get("extras", {})
    lines.append(f"🌚 Extras ({sum(1 for v in extras.values() if v)}/{len(EXTRA_ACHIEVEMENTS)})")
    for key, (name, emoji, xp_bonus, desc, quip) in EXTRA_ACHIEVEMENTS.items():
        if extras.get(key):
            lines.append(f"  ✅ {emoji} {name} — {desc}")
            lines.append(f"     💬 <i>{_personalize_ach_text(quip, user_id)}</i>")
        else:
            lines.append("  🔒 ???????? — ????????")

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Back to Home", callback_data="back_home")]])
    send = reply_target.edit_text if edit else reply_target.reply_text
    await send("\n".join(lines).strip(), parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def _send_mystats(context: ContextTypes.DEFAULT_TYPE, user_id: int, reply_target, edit: bool = False) -> None:
    """Builds and sends the /mystats profile card to reply_target (an
    update.message or a callback_query.message — both support
    reply_text/edit_text). Shared by the /mystats command and the
    main-menu button. edit=True rewrites reply_target in place instead of
    sending a new message — only valid for a bot-owned message (button
    flow), not a freshly typed command."""
    entry = ANALYTICS.get(str(user_id))
    if not entry or not entry.get("last_active_date"):
        send = reply_target.edit_text if edit else reply_target.reply_text
        await send("bro you didn't even start anything yet 😂, go take a quiz first 💥")
        return

    streak      = entry.get("streak", 0)
    # streak_best is new — fall back to streak itself for an entry that
    # predates it and hasn't gone through _get_entry's backfill yet (e.g.
    # right after a restore, before this user's first action this run).
    streak_best = entry.get("streak_best", streak)
    lec_answered = entry.get("lecture_questions_answered", 0)
    lec_correct  = entry.get("lecture_questions_correct", 0)
    accuracy     = (lec_correct / lec_answered * 100) if lec_answered else 0.0
    xp    = entry.get("xp", 0)
    level = entry.get("level", 0)
    xp_multiplier = entry.get("xp_multiplier", 1.0)
    title = _level_title(level)

    xp_start, xp_end = _level_xp_range(level)
    xp_into_level    = xp - xp_start
    xp_needed        = xp_end - xp_start
    bar_filled       = int((xp_into_level / xp_needed) * 10) if xp_needed else 10
    bar              = "█" * bar_filled + "░" * (10 - bar_filled)

    nickname = get_nickname(user_id) or "—"
    year_class = get_year_class(user_id)
    year_line = (
        f"{year_class[1:]} (Class {YEAR_CLASS_NUMBER[year_class]})"
        if year_class in YEAR_CLASS_NUMBER else "—"
    )

    mistake_count = _user_mistake_count(user_id)

    # Current module's subjects, strongest -> weakest. "Current module" is
    # the admin-set /daily_module scope when there is one, else the module
    # of the user's own latest answer — see _current_module_for_stats.
    # Omitted entirely (no empty header) until they've answered something
    # in that module.
    stats_module = _current_module_for_stats(entry)
    ranking      = _subject_ranking(entry, stats_module)
    if ranking:
        subj_lines = []
        for i, (subject, pct, correct, answered) in enumerate(ranking):
            marker = "💪" if i == 0 and len(ranking) > 1 else ("⚠️" if i == len(ranking) - 1 and len(ranking) > 1 else "▫️")
            subj_lines.append(
                f"  {marker} {html.escape(subject_label(subject))}: <b>{pct:.0f}%</b> ({correct}/{answered})"
            )
        subjects_block = (
            f"📈 <b>{html.escape(stats_module)}</b> — strongest → weakest\n"
            + "\n".join(subj_lines) + "\n\n"
        )
    else:
        subjects_block = ""

    # achievements summary
    ach        = entry.get("achievements", {})
    ach_lines  = []
    icons      = {
        "questions_answered": "📚", "correct_streak": "🔥",
        "lectures_completed": "📖", "xp_levels": "⭐",
        "daily_quiz": "📅", "daily_streak": "🔥",
        "achievement_collector": "🔍",
    }
    for key, emoji in icons.items():
        tier = ach.get(key, 0)
        if tier:
            name = _personalize_ach_text(ACHIEVEMENTS[key][tier - 1][1], user_id)
            emoji = ACHIEVEMENTS[key][tier - 1][3]   # tier 5 swaps to 🪄 for "The Quizician"
            if key == "achievement_collector":
                mult = ACHIEVEMENTS[key][tier - 1][2]
                ach_lines.append(f"  {emoji} {name} (x{mult})")
            else:
                ach_lines.append(f"  {emoji} {name} {'⭐' * tier}")
    for key, unlocked in ach.get("extras", {}).items():
        if unlocked:
            name, emoji, _xp, _desc, _quip = EXTRA_ACHIEVEMENTS[key]
            ach_lines.append(f"  {emoji} {name}")

    ach_text = "\n".join(ach_lines) if ach_lines else "  لسه مفيش إنجازات"

    # Daily Quiz leaderboard medals — lifetime top-3 finishes, see
    # _finalize_daily_leaderboard. Always shows all three, 0 included,
    # so it reads as a running tally rather than only appearing once
    # someone's actually won something.
    medals = entry.get("daily_medals", {"gold": 0, "silver": 0, "bronze": 0})
    medals_line = f"🥇({medals['gold']}) 🥈({medals['silver']}) 🥉({medals['bronze']})"

    send = reply_target.edit_text if edit else reply_target.reply_text
    await send(
        f"╔══════════════════╗\n"
        f"     🌐     YOUR PROFILE      🌐\n"
        f"╚══════════════════╝\n"
        f"Nickname: {html.escape(nickname)}\n"
        f"Role: Student\n"
        f"Year: {year_line}\n\n"
        f"🏅 Level: {level} — <i>{title}</i>\n"
        f"✨ XP: {xp:,}  [{bar}]  → {xp_end:,}"
        + (f"  (x{xp_multiplier} 🔍)" if xp_multiplier != 1.0 else "") + "\n\n"
        f"🔥 Current streak: {streak} days\n"
        f"🏅 Best streak: {streak_best} days\n\n"
        f"📚 Questions\n"
        f"   Answered: {lec_answered:,}\n"
        f"   Correct: {lec_correct:,}\n"
        f"   Accuracy: {accuracy:.1f}%\n\n"
        f"{subjects_block}"
        f"🧠 Mistakes Bank\n"
        f"  current: {mistake_count} questions\n\n"
        f"Your medals\n"
        f"{medals_line}\n\n"
        f"🏆 <b>Achievements</b>\n{ach_text}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏆 Achievements", callback_data="view_achievements"),
            InlineKeyboardButton("🏠 Back to Home",  callback_data="back_home"),
        ]]),
    )

async def _send_settings(context: ContextTypes.DEFAULT_TYPE, user_id: int, reply_target, edit: bool = False, page: int = 1) -> None:
    """Builds and sends the Settings screen to reply_target (an
    update.message or a callback_query.message). Mirrors _send_mystats:
    edit=True rewrites reply_target in place (button flow), edit=False
    sends a fresh reply. page 2 is the "➡️ More Settings" overflow page —
    Reactions / Achievement Alerts / Daily Notification / Hourly Zikr;
    page 1 is everything else (Username, Year/Class, Auto-Next,
    Randomize, Spaced Repetition, Question Timer)."""
    if page == 2:
        text = (
            "⚙️ <b>الإعدادات — صفحة 2</b>\n\n"
            "🎭 <b>Reactions</b>: البوت يرد بإيموجي عشوائي على رسايلك.\n"
            "🏆 <b>Achievement Alerts</b>: تنبيه لما تفتح achievement جديد.\n"
            "🔔 <b>Daily Notification</b>: تنبيه يومي الساعة 2 الضهر لما الـ Daily Quiz يتجدد.\n"
            "📿 <b>Hourly Zikr</b>: تذكير بالزكر كل ساعة."
        )
    else:
        nickname = get_nickname(user_id)
        nick_line = f"<b>{html.escape(nickname)}</b>" if nickname else "<i>مش متسجل — دوس تحت تحطه</i>"
        yc_line = html.escape(year_class_label(get_year_class(user_id)))
        spaced_rep_on = get_spaced_repetition_enabled(user_id)
        auto_next_on  = get_auto_next_enabled(user_id)
        text = (
            f"⚙️ <b>الإعدادات</b>\n\n"
            f"👤 الاسم المستعار: {nick_line}\n"
            f"⚠️ استخدم اسم لائق 🙊 — هو اللي هيظهر في الـ Leaderboard وقدام زمايلك.\n\n"
            f"📚 <b>Year/Class</b>: {yc_line}\n"
            "⏭️ <b>Auto-Next</b>: الأسئلة تتبعت واحد واحد بدل ما تتبعت كلها مرة واحدة.\n"
            "🔀 <b>Randomize</b>: ترتيب الأسئلة يبقى عشوائي كل مرة.\n"
            "🔁 <b>Spaced Repetition</b>: بيعيد سؤال غلطت فيه بعد شوية عشان يثبت في ذاكرتك."
            + ("\n⚠️ لازم الـ Auto-Next يكون شغال عشان الميزة دي تشتغل." if spaced_rep_on and not auto_next_on else "")
            + "\n⏱️ <b>Question Timer</b>: وقت محدد لكل سؤال قبل ما يتقفل تلقائي."
        )

    send = reply_target.edit_text if edit else reply_target.reply_text
    await send(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=settings_menu_keyboard(user_id, page=page),
    )

async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Analytics are keyed by the person's real Telegram user id — not the
    # chat id — so this shows the same numbers whether /mystats is run in
    # a DM or inside a group/channel the bot is in.
    user_id = update.effective_user.id if update.effective_user else update.effective_chat.id
    await _send_mystats(context, user_id, update.message)


async def reset_analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: asks for confirmation, then wipes all analytics data
    locally and deletes the pinned backup in the analytics group. The
    actual wipe happens in button_handler's reset_analytics_yes branch
    once the admin taps to confirm — this is irreversible, unlike most
    other admin actions here."""
    if update.effective_chat.id != ADMIN_ID:
        return
    await update.message.reply_text(
        f"⚠️ <b>متأكد إنك عايز تمسح كل الـ Analytics؟</b>\n\n"
        f"دلوقتي فيه بيانات <b>{len(ANALYTICS)}</b> يوزر، والعملية دي مش هترجع تاني.",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 أيوه، امسح كل حاجة", callback_data="reset_analytics_yes")],
            [InlineKeyboardButton("🔙 لأ، سيبها", callback_data="reset_analytics_no")],
        ]),
    )

async def restore_analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: manually re-pull analytics.json from the pinned backup
    in ANALYTICS_GROUP_ID — the same restore that runs automatically on
    startup. Use this if the local file ever gets wiped, corrupted, or
    out of sync with the backup, without needing to restart the bot."""
    if not (update.effective_user and update.effective_user.id == ADMIN_ID):
        return
    before = len(ANALYTICS)
    await restore_analytics_from_channel(context)
    await update.message.reply_text(
        f"♻️ Restored from pinned backup.\n"
        f"Users on file: <b>{len(ANALYTICS)}</b> (was {before} before restore).",
        parse_mode=ParseMode.HTML,
    )

async def import_analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: import analytics from an uploaded .json file. Reply to
    the file's message with /import_analytics. This is the fallback for
    when the pinned backup itself is missing/corrupted — e.g. importing a
    copy you saved elsewhere. Merges into (does not wipe) existing data,
    then re-saves and re-pins so the channel backup reflects the import."""
    global _analytics_dirty
    if not (update.effective_user and update.effective_user.id == ADMIN_ID):
        return
    reply = update.message.reply_to_message
    doc   = reply.document if reply else None
    if not doc:
        await update.message.reply_text(
            "⚠️ Reply to the analytics .json file with /import_analytics."
        )
        return
    try:
        tg_file  = await context.bot.get_file(doc.file_id)
        raw      = await tg_file.download_as_bytearray()
        imported = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(imported, dict):
            raise ValueError("File doesn't look like an analytics export (expected a JSON object).")
    except Exception as e:
        await update.message.reply_text(f"❌ Import failed: {e}")
        return
    cleaned = _clean_analytics_dict(imported)
    if not cleaned:
        await update.message.reply_text(
            "❌ Import failed: nothing in that file looked like a valid analytics entry "
            "(expected {\"<telegram_user_id>\": {...}, ...}). Wrong file?"
        )
        return
    ANALYTICS.update(cleaned)
    await save_analytics()
    _analytics_dirty = False   # disk now matches memory — nothing left for the periodic flush to do
    await backup_analytics_to_channel(context)
    dropped = len(imported) - len(cleaned)
    note = f" ({dropped} malformed entr{'y' if dropped == 1 else 'ies'} skipped)" if dropped else ""
    await update.message.reply_text(
        f"✅ Imported <b>{len(cleaned)}</b> user(s){note} — merged into current data, "
        f"saved locally, and re-pinned to the backup channel.\n"
        f"Total users on file now: <b>{len(ANALYTICS)}</b>.",
        parse_mode=ParseMode.HTML,
    )

async def _post_init(app):
    """Runs once after the bot connects, before polling starts — restores
    the storage-group and each year's quiz-channel indexes from their
    pinned backup messages, so a wiped/switched local disk doesn't orphan
    content that's still sitting safely in the channels themselves."""
    # asyncio.to_thread() (used by every save_*() function's disk write)
    # runs on Python's DEFAULT ThreadPoolExecutor, sized
    # min(32, cpu_count()+4) — on a small Railway instance (1-2 vCPUs)
    # that's as few as 5-6 threads, shared across the ENTIRE bot. Every
    # save_*() call (which fires on essentially every answered question —
    # XP, streak, mistakes-bank updates) queues behind that tiny pool
    # once concurrent students exceed it. Under load-testing (see the
    # load-test harness from this conversation), this was the leading
    # suspect for latency going strongly super-linear with concurrency
    # (p95 growing ~44x for a 7x increase in users) rather than roughly
    # linearly, since it's a real queueing bottleneck, not a CPU-bound
    # one — these are disk-I/O-bound calls (fsync), so a pool much larger
    # than the CPU core count is appropriate and safe, not wasteful.
    #
    # Done here, not at module level: this is the first point in the
    # file guaranteed to be running ON the actual event loop PTB will use
    # for the rest of the bot's life (_post_init is an async callback PTB
    # itself awaits during startup, per its own post_init contract) — so
    # asyncio.get_running_loop() here is guaranteed correct, unlike
    # calling asyncio.get_event_loop() at bare module-import time, which
    # can silently attach to the wrong loop object depending on Python
    # version and how run_polling() manages its own loop internally.
    import concurrent.futures as _cf
    asyncio.get_running_loop().set_default_executor(
        _cf.ThreadPoolExecutor(max_workers=64, thread_name_prefix="quizician-io")
    )

    await restore_storage_from_channel(app)
    for y in configured_years():
        await restore_quiz_from_channel(app, y)
    await restore_analytics_from_channel(app)
    await restore_settings_from_channel(app)
    await restore_lecture_results_from_channel(app)
    await restore_mistakes_bank_from_channel(app)
    await restore_report_threads_from_channel(app)
    await restore_sessions_from_channel(app)

    if app.job_queue is None:
        print(
            "⚠️ No JobQueue available — periodic backup reconciliation, the "
            "analytics/settings/mistakes-bank flushes, stale-session cleanup, "
            "session persistence, the Daily Quiz push, the hourly Zikr "
            "reminder, and the daily zipped backup export are disabled. "
            "Local analytics/settings/mistakes-bank changes from hot paths "
            "(poll answers, Daily Quiz starts, wrong answers) will only hit "
            "disk on the next immediate-save call site "
            "(restore/reset/import/toggle) or on a clean shutdown, not every "
            "60s. Install with: pip install \"python-telegram-bot[job-queue]\""
        )
    else:
        app.job_queue.run_repeating(
            _reconcile_backups_job, interval=BACKUP_RECONCILE_INTERVAL, first=BACKUP_RECONCILE_INTERVAL,
        )
        app.job_queue.run_repeating(
            _flush_analytics_job, interval=ANALYTICS_FLUSH_INTERVAL, first=ANALYTICS_FLUSH_INTERVAL,
        )
        app.job_queue.run_repeating(
            _flush_settings_job, interval=SETTINGS_FLUSH_INTERVAL, first=SETTINGS_FLUSH_INTERVAL,
        )
        app.job_queue.run_repeating(
            _flush_mistakes_bank_job, interval=MISTAKES_BANK_FLUSH_INTERVAL, first=MISTAKES_BANK_FLUSH_INTERVAL,
        )
        app.job_queue.run_repeating(
            _cleanup_stale_sessions_job, interval=STALE_SESSION_CHECK_INTERVAL, first=STALE_SESSION_CHECK_INTERVAL,
        )
        app.job_queue.run_repeating(
            _sessions_backup_job, interval=SESSIONS_BACKUP_MIN_INTERVAL, first=SESSIONS_BACKUP_MIN_INTERVAL,
        )
        app.job_queue.run_repeating(
            _sessions_stale_sweep_job, interval=SESSIONS_STALE_SWEEP_INTERVAL, first=SESSIONS_STALE_SWEEP_INTERVAL,
        )
        app.job_queue.run_daily(
            _daily_quiz_push_job, time=dt_time(hour=DAILY_QUIZ_HOUR, minute=DAILY_QUIZ_MIN, tzinfo=DAILY_QUIZ_TZ),
        )
        app.job_queue.run_repeating(
            _zikr_push_job, interval=3600, first=_next_top_of_hour_delay(DAILY_QUIZ_TZ),
        )
        app.job_queue.run_daily(
            _daily_backup_export_job,
            time=dt_time(hour=DAILY_BACKUP_EXPORT_HOUR, minute=DAILY_BACKUP_EXPORT_MIN, tzinfo=DAILY_QUIZ_TZ),
        )

async def _flush_analytics_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic tick for the hot-path debounce described above
    _analytics_dirty: writes analytics.json only if a poll answer marked it
    dirty since the last tick. No-ops (no deepcopy, no I/O) on a quiet tick."""
    await _flush_analytics_if_dirty()

async def _flush_settings_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic tick for the hot-path debounce described above
    _settings_dirty: writes settings.json only if a Daily Quiz start (or
    other dirty-marking call site) changed it since the last tick."""
    await _flush_settings_if_dirty()

async def _flush_mistakes_bank_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic tick for the hot-path debounce described above
    _mistakes_bank_dirty: writes mistakes_bank.json only if a wrong answer
    (record_mistake) changed it since the last tick."""
    await _flush_mistakes_bank_if_dirty()

# ═══════════════════════════════════════════════════════════════
# STALE SESSION CLEANUP — a student who leaves mid-lecture (closes the
# app, loses signal, just gets distracted) and never comes back leaves
# LECTURE_SESSIONS[user_id] (or the Daily Quiz / Mistakes Retake
# equivalent) sitting in memory forever — nothing removes it on its own.
# The question-timer fix (see poll_update_handler) only closes a session
# out once ITS poll actually closes, and with the timer set to Off (the
# default), a poll never closes by itself at all.
#
# Not a crash or lockout risk — confirmed: starting a fresh lecture just
# overwrites the old entry (LECTURE_SESSIONS[user_id] = session, no
# "already in a lecture" guard anywhere), and @_serialize_per_user's
# lock is released between updates, never held across a whole session.
# It IS a genuine slow memory leak, though, and a stale poll left open
# means Telegram would still silently accept a vote on it days later
# even though nothing's listening for it anymore by then (whatever
# session exists for that user_id won't match that old poll_id).
#
# STALE_SESSION_IDLE_SECONDS is deliberately generous — this must never
# fire on someone taking a normal break mid-lecture who fully intends to
# come back and finish; it's only meant to catch sessions that are,
# realistically, abandoned for good.
# ═══════════════════════════════════════════════════════════════
STALE_SESSION_IDLE_SECONDS   = 6 * 3600  # 6 hours of no activity before a session is reclaimed
STALE_SESSION_CHECK_INTERVAL = 3600      # check once an hour

async def _cleanup_stale_sessions_job(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    for sessions in (LECTURE_SESSIONS, DAILY_QUIZ_SESSIONS, MISTAKES_RETAKE_SESSIONS):
        for user_id, session in list(sessions.items()):
            if session.get("mode") == "batch":
                # Several polls can be open at once with no single
                # current_delivered_at — use the OLDEST of them (the
                # longest-idle question) as this session's age, and
                # close out every one of them, not just one.
                pending = session.get("pending_polls", {})
                if not pending:
                    continue
                timestamps = [t for (_, _, _, t) in pending.values() if t is not None]
                delivered_at = min(timestamps) if timestamps else None
                message_ids = [mid_ for (_, mid_, _, _) in pending.values()]
            else:
                delivered_at = session.get("current_delivered_at")
                message_ids = [session["current_message_id"]] if session.get("current_message_id") else []

            if delivered_at is None or now - delivered_at < STALE_SESSION_IDLE_SECONDS:
                continue

            sessions.pop(user_id, None)
            for message_id in message_ids:
                try:
                    await context.bot.stop_poll(chat_id=user_id, message_id=message_id)
                except Exception:
                    pass  # already closed/deleted/blocked — any of these are fine, nothing to do
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⏳ الجلسة اتقفلت لعدم النشاط لفترة طويلة — ابدأ تاني لما تكون جاهز.",
                )
            except Exception:
                pass  # blocked the bot, deactivated account, etc. — skip silently, same as broadcast_cmd

async def _post_shutdown(app):
    """Runs once on a clean shutdown (PTB's own stop-signal handling calls
    this before the process exits) — flushes any analytics still sitting in
    memory from the last (< ANALYTICS_FLUSH_INTERVAL)-second window, so a
    normal restart/redeploy never loses data. Only a hard crash (killed
    process, power loss) can still lose that window; a clean stop cannot."""
    await _flush_analytics_if_dirty()
    await _flush_settings_if_dirty()
    await _flush_mistakes_bank_if_dirty()
    await _flush_sessions_if_changed()

# ── Backup reconciliation ────────────────────────────────────────
# Every backup_*_to_channel() call above is reactive and fire-and-forget:
# it fires once, right after a data change, and any failure in the
# upload/pin/delete-old-pin sequence is only logged, never retried.
# Almost always fine — but a dropped pin call or a delete that silently
# fails (message already gone, a transient timeout, etc.) can leave the
# channel's pinned message out of sync with what's actually on disk,
# and nothing would notice until the NEXT change came along to trigger
# another reactive backup.
#
# This job runs on a short timer instead of waiting for the next change:
# every BACKUP_RECONCILE_INTERVAL seconds, for each backup system
# (analytics, settings, lecture results, storage, and each configured
# year's quiz index), it checks whether the channel's current pin still
# matches the caption marker we expect. If the pin is missing, or belongs
# to a different marker (e.g. our own backup got unpinned by someone, or a
# delete-old-pin call left a stale one pinned instead), it just re-runs
# that system's normal backup_*_to_channel() — which re-uploads,
# re-pins, and cleans up the old message the same way it always does.
# Cheap: one get_chat per system per tick, and the throttle inside each
# backup_*_to_channel() call means this never spams uploads if
# everything's already fine.
BACKUP_RECONCILE_INTERVAL = 5  # seconds

async def _reconcile_backups_job(context: ContextTypes.DEFAULT_TYPE):
    checks = [
        ("analytics",       ANALYTICS_GROUP_ID,       ANALYTICS_BACKUP_MARKER,       backup_analytics_to_channel),
        ("settings",        SETTINGS_GROUP_ID,        SETTINGS_BACKUP_MARKER,        backup_settings_to_channel),
        ("lecture_results", LECTURE_RESULTS_GROUP_ID, LECTURE_RESULTS_BACKUP_MARKER, backup_lecture_results_to_channel),
        ("mistakes_bank",   MISTAKES_BANK_GROUP_ID,   MISTAKES_BANK_BACKUP_MARKER,   backup_mistakes_bank_to_channel),
        ("storage",         STORAGE_GROUP_ID,         STORAGE_BACKUP_MARKER,         backup_storage_to_channel),
        # force=True: the pin being out of sync is exactly why we're here —
        # skip both backup_sessions_to_channel's throttle and its
        # unchanged-content skip so the re-upload actually happens.
        ("sessions",        SESSIONS_GROUP_ID,        SESSIONS_BACKUP_MARKER,
         (lambda ctx: backup_sessions_to_channel(ctx, force=True))),
    ]
    # One quiz check per configured year, each hitting its own channel.
    for y in configured_years():
        checks.append((
            f"quiz_{y}", year_channel_id(y), QUIZ_BACKUP_MARKER,
            (lambda ctx, year=y: backup_quiz_to_channel(ctx, year)),
        ))
    for key, group_id, marker, backup_fn in checks:
        if not group_id or not RESTORE_OK.get(key, True):
            continue   # not configured, or restore already failed this session — leave it alone
        try:
            chat   = await context.bot.get_chat(group_id)
            pinned = chat.pinned_message
            in_sync = bool(pinned and pinned.document and (pinned.caption or "") == marker)
        except Exception as e:
            print(f"BACKUP RECONCILE — couldn't check {key}: {e}")
            continue
        if not in_sync:
            print(f"BACKUP RECONCILE — {key} pin out of sync, re-backing up.")
            try:
                await backup_fn(context)
            except Exception as e:
                print(f"BACKUP RECONCILE — re-backup of {key} failed: {e}")

# ═══════════════════════════════════════════════════════════════
# PIN SERVICE-MESSAGE CLEANUP
#
# Every pin_chat_message() call in the backup system (and the pins made
# when quiz-channel questions/lectures get pinned) causes Telegram to post
# a "The Quizician pinned a file" service message in that chat.
# disable_notification only silences the push notification, it doesn't
# stop the message itself — so we delete it on sight in the bot's own
# chats. Restricted to chats the bot actually pins in; if a human admin
# pins something else in one of these, its service message gets deleted
# too, since Telegram doesn't tell us who did the pinning.
# ═══════════════════════════════════════════════════════════════
BACKUP_CHAT_IDS = [c for c in (
    STORAGE_GROUP_ID, *QUIZ_CHANNEL_IDS, ANALYTICS_GROUP_ID,
    SETTINGS_GROUP_ID, LECTURE_RESULTS_GROUP_ID, SESSIONS_GROUP_ID,
) if c]

async def delete_pin_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg:
        return
    try:
        await context.bot.delete_message(chat_id=msg.chat_id, message_id=msg.message_id)
    except Exception as e:
        print(f"PIN SERVICE MESSAGE DELETE ERROR: {e}")

# concurrent_updates(256): by default PTB processes updates one at a time,
# globally — every poll answer/button tap/message from every user queues
# behind whichever one is currently being handled. Fine at low volume, but
# on a night with 500+ people answering quizzes at once it means everyone
# queues behind everyone else, even though their updates touch completely
# unrelated per-user state. 256 lets that many updates run concurrently
# (each user's own updates are still serialized against each other — see
# @_serialize_per_user below); Telegram's own rate limits are still
# enforced by AIORateLimiter regardless of how many run at once locally.
#
# The thread-pool-size fix for asyncio.to_thread() (every save_*()
# function's disk write) lives in _post_init, not here — it needs a
# guaranteed-running event loop to attach to (asyncio.get_running_loop()),
# and at this point in the file the loop PTB will actually run polling on
# doesn't necessarily exist yet / isn't necessarily the one
# asyncio.get_event_loop() would return this early. See _post_init.
app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .rate_limiter(AIORateLimiter())
    .concurrent_updates(256)
    .post_init(_post_init)
    .post_shutdown(_post_shutdown)
    .build()
)

# ═══════════════════════════════════════════════════════════════
# GLOBAL ERROR HANDLER
#
# PTB already catches exceptions per-update internally so one bad update
# can't take down the whole bot — but without this, the traceback just
# goes to stderr and nobody finds out. This posts a compact report to
# ERROR_LOG_GROUP_ID instead. Wrapped in its own try/except since the
# last thing an error handler should do is raise another error.
# ═══════════════════════════════════════════════════════════════
def _describe_update(update: object) -> str:
    """A short, human-readable line describing what was happening when
    this update came in — who, and what they did (typed a command,
    tapped a button, sent a poll answer, ...) — so the errors channel
    reads like 'Kareem tapped button toggle_reactions' instead of making
    a reader reconstruct that from the raw update JSON below it. Falls
    back to a plain label if update isn't a normal Update (e.g. an error
    raised from a job_queue task, which has no update at all)."""
    if not isinstance(update, Update):
        return "(no update — this came from a background job/task, not a live user update)"

    who = "unknown user"
    if update.effective_user:
        u = update.effective_user
        uname = f"@{u.username}" if u.username else (u.full_name or "no name")
        who = f"{uname} (id {u.id})"

    if update.callback_query:
        action = f"tapped button: {update.callback_query.data!r}"
    elif update.message and update.message.text:
        action = f"sent message: {update.message.text[:120]!r}"
    elif update.message and update.message.poll:
        action = "sent a poll"
    elif update.poll_answer:
        action = f"answered poll {update.poll_answer.poll_id}"
    elif update.message:
        action = "sent a non-text message (photo/document/etc.)"
    else:
        action = "sent an unrecognized update type"

    chat_kind = f" in {update.effective_chat.type} chat {update.effective_chat.id}" if update.effective_chat else ""
    return f"{who} {action}{chat_kind}"

def _likely_cause(exc: BaseException) -> str | None:
    """A short plain-English guess at what caused this, based on common
    failure patterns in a python-telegram-bot app — matches on exception
    type, and for a few Telegram-specific ones, on substrings Telegram's
    API is known to send back. Purely heuristic: meant to save a first
    read-through of the traceback, not to replace it, so it's fine (and
    expected) for this to return None on anything it doesn't recognize —
    the full traceback is always still attached below it."""
    msg = str(exc).lower()

    if isinstance(exc, Forbidden):
        return "The bot tried to message someone who blocked it, left the chat, or kicked the bot — nothing to fix in the code, just an unreachable user."
    if isinstance(exc, RetryAfter):
        return f"Hit Telegram's flood/rate limit — sent too many requests too fast (retry after {getattr(exc, 'retry_after', '?')}s). Usually transient."
    if isinstance(exc, BadRequest):
        if "message is not modified" in msg:
            return "Tried to edit a message with identical text/markup — Telegram rejects no-op edits. Usually harmless."
        if "message to edit not found" in msg or "message can't be edited" in msg:
            return "The message the bot tried to edit was deleted, too old, or never existed (e.g. a stale button left on an old message)."
        if "query is too old" in msg or "query id is invalid" in msg or "response timeout expired" in msg:
            return "A button was tapped after its callback query expired — usually a very old message, or the bot restarted since the button was shown."
        if "chat not found" in msg:
            return "Tried to message a chat/group ID the bot isn't actually a member of (or that ID is wrong)."
        if "not enough rights" in msg or "have no rights" in msg:
            return "The bot isn't an admin (or lacks a specific permission) in that group/channel."
        return "Telegram rejected the request outright — check the exact parameters of the call against the message above."
    # NOTE: BadRequest is (surprisingly) a subclass of NetworkError in
    # python-telegram-bot, so this catch-all MUST come after the
    # BadRequest check above, or every BadRequest gets misclassified as
    # a transient network blip instead of getting its specific message.
    if isinstance(exc, (TimedOut, NetworkError)):
        return "Telegram's API was slow or briefly unreachable — usually transient, not a code bug."
    if isinstance(exc, KeyError):
        return f"Code expected key {exc} in a dict that didn't have it — often a data-shape mismatch (a settings/analytics/mistakes-bank entry missing a field) or a callback_data referencing something that no longer exists."
    if isinstance(exc, IndexError):
        return "Code indexed into a list/string shorter than expected — often an off-by-one, or content split into fewer parts than assumed."
    if isinstance(exc, AttributeError) and "nonetype" in msg:
        return "Code called a method/attribute on something that was None — usually a lookup (dict.get, a file/record read) that came back empty and wasn't checked before use."
    if isinstance(exc, json.JSONDecodeError):
        return "Malformed JSON was being parsed — check whichever file or channel backup was being loaded at the time."
    if isinstance(exc, ValueError):
        return "A conversion or parse step got a value it couldn't handle (e.g. int()/float() on non-numeric text, or unexpected formatting)."
    if isinstance(exc, TypeError):
        return "A function got an argument of the wrong type or count — often None sneaking in where a real value was expected."
    return None

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    tb_string = "".join(traceback.format_exception(
        None, context.error, context.error.__traceback__
    ))
    print("UNHANDLED ERROR:", tb_string)

    if not ERROR_LOG_GROUP_ID:
        return

    who_what   = _describe_update(update)
    cause      = _likely_cause(context.error)
    cause_line = f"🕵️ <b>Likely cause:</b> {html.escape(cause)}\n\n" if cause else ""

    update_str = update.to_dict() if isinstance(update, Update) else str(update)
    # Telegram messages cap at 4096 chars — keep well under that even
    # with the two new lines above (who/what + likely cause) added.
    report = (
        f"🚨 <b>Bot error</b>\n"
        f"👤 <b>Who/what:</b> {html.escape(who_what)}\n"
        f"<b>{type(context.error).__name__}:</b> {html.escape(str(context.error))}\n\n"
        f"{cause_line}"
        f"<b>Update:</b>\n<code>{html.escape(str(update_str))[:900]}</code>\n\n"
        f"<b>Traceback:</b>\n<code>{html.escape(tb_string)[-1800:]}</code>"
    )
    try:
        await context.bot.send_message(
            chat_id=ERROR_LOG_GROUP_ID, text=report, parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        print(f"ERROR LOG GROUP SEND FAILED: {e}")
        return   # not counted below — it never actually reached the errors channel

    # Only reaches here once the report has actually landed in the errors
    # channel, so /health's "errors last 24h" always matches what's really
    # sitting there — not what the handler merely attempted to send.
    now = time.time()
    _ERROR_LOG_TIMES.append(now)
    cutoff = now - _ERROR_LOG_MAX_AGE_SECONDS
    while _ERROR_LOG_TIMES and _ERROR_LOG_TIMES[0] < cutoff:
        _ERROR_LOG_TIMES.pop(0)

app.add_error_handler(global_error_handler)

async def _ban_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs in an earlier handler group than everything else (see the
    group=-1 registrations right below), so it sees every update first.
    A currently-banned user (get_ban_info/is_banned) gets a short notice
    with their remaining time + reason instead of whatever they tried to
    do, and ApplicationHandlerStop keeps the update from ever reaching
    the real handlers in group 0. A user whose ban has lifted (or who
    was never banned) just falls through untouched."""
    user = update.effective_user
    if not user:
        return
    until, reason = get_ban_info(user.id)
    if until is None:
        return
    hours_left = (until - time.time()) / 3600
    text = (
        f"🚫 انت متبنن من البوت لسه.\n"
        f"⏰ هيترفع البان بعد {hours_left:.1f} ساعة.\n"
        f"📝 السبب: {html.escape(reason or '—')}"
    )
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)
    raise ApplicationHandlerStop

app.add_handler(MessageHandler(filters.ALL, _ban_gate), group=-1)
app.add_handler(CallbackQueryHandler(_ban_gate), group=-1)

async def _onboarding_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same group=-1 pattern as _ban_gate, registered right after it, so
    it also sees every update before the real handlers in group 0.
    Nickname + year/class are now mandatory before ANYTHING else works.
    Explicitly let through: /start itself (so onboarding is always
    reachable), the nickname reply while AWAITING_NICKNAME is set (so
    the text handler can actually save it), and the onboarding
    year/class taps and the steps after (onboard_yc:*, onboard_bully:*,
    onboard_how, onboard_where, onboard_go) — every
    other update gets a nudge back to /start instead of its real
    handler. Channel posts (no effective_user) are never gated, same as
    _ban_gate."""
    user = update.effective_user
    if not user:
        return
    real_uid = user.id

    if update.message and update.message.text and update.message.text.startswith("/start"):
        return
    if update.message and AWAITING_NICKNAME.get(real_uid):
        return
    if update.callback_query:
        data = update.callback_query.data or ""
        if data.startswith(("onboard_yc:", "onboard_bully:")) or data in ("onboard_how", "onboard_where", "onboard_go"):
            return

    if get_nickname(real_uid) is not None and get_year_class(real_uid) in YEAR_CLASS_NUMBER:
        return

    text = "⚠️ لازم تعمل /start الأول وتسجل اسمك وسنتك/فرقتك قبل أي حاجة تانية."
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)
    raise ApplicationHandlerStop

app.add_handler(MessageHandler(filters.ALL, _onboarding_gate), group=-1)
app.add_handler(CallbackQueryHandler(_onboarding_gate), group=-1)

app.add_handler(MessageHandler(
    filters.StatusUpdate.PINNED_MESSAGE & filters.Chat(BACKUP_CHAT_IDS),
    delete_pin_service_message,
))

app.add_handler(CommandHandler("start",          start))
app.add_handler(CommandHandler("c",              commands_cmd))
app.add_handler(CommandHandler("cancel",         cancel_cmd))
app.add_handler(CommandHandler("report_issue",   report_issue_cmd))
app.add_handler(CommandHandler("sleep",          sleep_cmd))
app.add_handler(CommandHandler("admincheck",     admincheck_cmd))
app.add_handler(CommandHandler("health",         health_cmd))
app.add_handler(CommandHandler("restore",        restore_cmd))
app.add_handler(CommandHandler("broadcast",      broadcast_cmd))
app.add_handler(CommandHandler("ban",            ban_cmd))
app.add_handler(CommandHandler("unban",          unban_cmd))
app.add_handler(CommandHandler("mystats",           mystats_cmd))
app.add_handler(CommandHandler("restore_analytics", restore_analytics_cmd))
app.add_handler(CommandHandler("import_analytics",  import_analytics_cmd))
app.add_handler(CommandHandler("reset_analytics",   reset_analytics_cmd))
app.add_handler(CommandHandler("backup_now",     backup_now_cmd))
# Quiz channel
app.add_handler(CommandHandler("quiz",            quiz_lectures_cmd))
app.add_handler(CommandHandler("daily_module",     daily_module_cmd))
app.add_handler(CommandHandler("time",             time_cmd))
app.add_handler(CommandHandler("quiz_list",       quiz_list_cmd))
app.add_handler(CommandHandler("quiz_delete",     quiz_delete_cmd))
app.add_handler(CommandHandler("edit_quiz",       edit_quiz_cmd))
app.add_handler(CommandHandler("quiz_edit",       edit_quiz_cmd))

# Poll handler before text handler (forwarded OR own quiz polls) —
# excludes the quiz channel, which has its own dedicated handler below.
app.add_handler(MessageHandler(filters.POLL & ~filters.Chat(QUIZ_CHANNEL_IDS), handle_poll))

# Quiz channel indexing — lecture titles, "-END", and quiz polls posted
# there get filed by handle_quiz_channel_message, not treated as a user's
# own quiz-building activity. Must be registered before the generic
# text/poll handlers below.
app.add_handler(MessageHandler(
    filters.Chat(QUIZ_CHANNEL_IDS) & (filters.POLL | filters.TEXT), handle_quiz_channel_message
))

# Storage group indexing — anything posted in the vault group gets filed by
# its caption's password word. Must be checked before the generic photo
# handler below so vault posts don't get mistaken for quiz images.
STORAGE_MEDIA_FILTER = (
    filters.PHOTO | filters.VIDEO | filters.Document.ALL
    | filters.AUDIO | filters.VOICE | filters.ANIMATION | filters.Sticker.ALL
)
app.add_handler(MessageHandler(
    filters.Chat(STORAGE_GROUP_ID) & STORAGE_MEDIA_FILTER, handle_storage_message
))
# Plain-text vault posts — only the reserved onboarding key is filed (see
# handle_storage_text_message); everything else in that group is ignored.
app.add_handler(MessageHandler(
    filters.Chat(STORAGE_GROUP_ID) & filters.TEXT & ~filters.COMMAND, handle_storage_text_message
))

# Image handler (photos) — excludes the storage group
app.add_handler(MessageHandler(
    filters.PHOTO & ~filters.Chat(STORAGE_GROUP_ID), handle_image
))

# PDF handler — captioned PDFs in a private DM are parsed as manual MCQs;
# excludes the storage group and quiz channel.
app.add_handler(MessageHandler(
    filters.Document.PDF & ~filters.Chat(STORAGE_GROUP_ID) & ~filters.Chat(QUIZ_CHANNEL_IDS), handle_document
))

# Inline buttons
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(PollHandler(poll_update_handler))
app.add_handler(PollAnswerHandler(handle_poll_answer))

# Text handler last — excludes the storage group and the quiz channel
app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND & ~filters.Chat(STORAGE_GROUP_ID) & ~filters.Chat(QUIZ_CHANNEL_IDS), handle
))

def _print_startup_banner():
    """Cosmetic-only console banner on launch — pure stdout, no side
    effects, runs once right before polling starts. Reveals the QUIZ! art
    line by line, then decrypts each boot-check line from random
    characters (adds well under a second total, doesn't meaningfully
    delay startup). Note: the flicker relies on carriage-return overwrite,
    which only collapses cleanly in a live/interactive terminal — on a
    non-interactive log stream (e.g. Railway's log viewer) each frame may
    render as its own line instead of overwriting in place."""
    import time, sys
    PURPLE, ORANGE, GREEN, DIM, BOLD, RESET = (
        "\033[38;5;135m", "\033[38;5;208m", "\033[92m", "\033[90m", "\033[1m", "\033[0m"
    )

    cat_lines = [
        "                         /\\_/\\",
        "                        ( ⌒.⌒ )",
    ]
    quiz_lines = [
        " ██████╗  ██╗   ██╗ ██╗ ███████╗ ██╗",
        "██╔═══██╗ ██║   ██║ ██║ ╚══███╔╝ ██║",
        "██║   ██║ ██║   ██║ ██║   ███╔╝  ██║",
        "██║▄▄ ██║ ██║   ██║ ██║  ███╔╝   ╚═╝",
        "╚██████╔╝ ╚██████╔╝ ██║ ███████╗ ██╗",
        " ╚══▀▀═╝   ╚═════╝  ╚═╝ ╚══════╝ ╚═╝",
    ]
    boot_lines = [
        "[ OK ] question bank engine loaded",
        "[ OK ] quiz channel index mounted",
        "[ OK ] XP + achievements module warmed up",
        "[ OK ] analytics backup channel linked",
        "[ OK ] handshake with Telegram Bot API...",
    ]

    def _decode_line(text: str, color: str) -> None:
        """Call-of-Duty-style decrypt flicker: random characters settle
        into the real text a few characters at a time, left to right."""
        charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*"
        n = len(text)
        revealed = 0
        while revealed < n:
            frame = [
                text[i] if (i < revealed or text[i] == " ") else random.choice(charset)
                for i in range(n)
            ]
            sys.stdout.write("\r" + color + "".join(frame) + RESET)
            sys.stdout.flush()
            time.sleep(0.08)
            revealed += 3
        sys.stdout.write("\r" + color + text + RESET + "\n")
        sys.stdout.flush()

    print()
    print(f"{DIM}{'─' * 42}{RESET}")
    for line in cat_lines:
        print(f"{ORANGE}{line}{RESET}")
        time.sleep(0.12)
    for line in quiz_lines:
        print(f"{PURPLE}{line}{RESET}")
        time.sleep(0.08)
    print()

    print(f"{DIM}{'─' * 42}{RESET}")
    for line in boot_lines:
        _decode_line(line, GREEN)
    print(f"{DIM}{'─' * 42}{RESET}")
    print(f"{BOLD}{GREEN}>> Quizzician v5.5 online — listening for updates{RESET}")
    print(f"{DIM}{'─' * 42}{RESET}\n")

_print_startup_banner()
app.run_polling()
