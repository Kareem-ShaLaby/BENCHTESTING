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
import tempfile
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
#          telegram_username via _update_telegram_name)
#  927   SETTINGS — per-user personalization (nickname, reactions,
#          auto_next, randomize)
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
#          content (build_daily_quiz_questions, start_mistakes_retake).
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
# 2212   STATE (in-memory dicts: LECTURE_SESSIONS, QUIZ_POLL_STATUS, etc. —
#          all purely in-memory, NOT persisted/restored across a restart;
#          a crash mid-finals-night silently drops everyone's active
#          quiz/lecture session)
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
# 2555   PROGRESS MESSAGE BUILDER
# 2619   KEYBOARD HELPERS (main menu, settings menu, etc.)
# 2706   MENU TEXT CONTENT
# 2731   PDF BUILDER
# 2868   DOCX BUILDER
# 3127   REACTIONS (react_random, lecture-answer streak reactions)
# 3183   SLEEP / WAKE COMMANDS
# 3195   PASSIVE ANSWER BACKFILL / LECTURE DELIVERY + SESSION LOGIC
#          — _deliver_next_lecture_question, _deliver_all_lecture_questions,
#            handle_poll_answer (@_serialize_per_user), _advance_lecture_session
#            (records mistakes into MISTAKES_BANK via record_mistake(user_id, mid, ...))
# 3567   FORWARDED POLL HANDLER
# 3606   QUESTION REVIEW / EDIT (after a question lands in the PDF buffer)
# 3752   IMAGE HANDLER (PDF mode only)
# 3917   STORAGE GROUP — AUTO-INDEXING
# 4307   TEXT MESSAGE HANDLER (includes /start's onboarding nickname prompt)
# 4564   INLINE BUTTON HANDLER (button_handler, @_serialize_per_user — all
#          callback_data routing, including lecture preview/leaderboard,
#          lecture start, and settings toggles)
# 5280   PDF/DOCX EXPORT (single source of truth, called from both PDF
#          commands and quiz-channel exports)
# 5397   PDF COMMANDS
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
#          seconds instead of waiting for the next real data change.
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

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Image as RLImage
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

try:
    from docx import Document as DocxDocument
    from docx.shared import Pt, RGBColor, Inches, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    DOCX_AVAILABLE = True
except ImportError:
    # python-docx (and its lxml dependency) not installed — DOCX export is
    # simply disabled until it's installed; everything else works fine.
    DOCX_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════
# FONT SETUP
# ═══════════════════════════════════════════════════════════════
_POPPINS_REG  = "/usr/share/fonts/truetype/google-fonts/Poppins-Regular.ttf"
_POPPINS_BOLD = "/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"

FONT_NAME      = "Helvetica"
FONT_NAME_BOLD = "Helvetica-Bold"

if os.path.exists(_POPPINS_REG) and os.path.exists(_POPPINS_BOLD):
    try:
        pdfmetrics.registerFont(TTFont("Poppins",      _POPPINS_REG))
        pdfmetrics.registerFont(TTFont("Poppins-Bold", _POPPINS_BOLD))
        FONT_NAME      = "Poppins"
        FONT_NAME_BOLD = "Poppins-Bold"
        print("Poppins font loaded")
    except Exception as e:
        print(f"Poppins load error: {e} — using Helvetica")
else:
    import time as _time, sys as _sys
    _text = "(LOADING...)"
    _delay = 2.0 / len(_text)
    for _ch in _text:
        _sys.stdout.write(_ch)
        _sys.stdout.flush()
        _time.sleep(_delay)
    print()

BOT_TOKEN = os.environ["BOT_TOKEN"]  # set this in Railway's Variables tab — never hardcode it
# NOTE: AIORateLimiter (used below when building `app`) needs the extra:
#   pip install "python-telegram-bot[rate-limiter]"
# Add that to requirements.txt too, or the import at the top of this file fails.

# Portable temp dir: tempfile.gettempdir() respects $TMPDIR, so this resolves
# to a writable path on both Railway (/tmp) and Termux ($PREFIX/tmp) — a
# hardcoded "/tmp" fails on Android, which has no writable /tmp.
IMG_BASE_DIR  = os.path.join(tempfile.gettempdir(), "quizician_imgs")
FONT_BASE_DIR = os.path.join(tempfile.gettempdir(), "quizician_fonts")

# Preset fonts bundled with the bot itself (not user-uploaded) — put the
# Preset fonts bundled with the bot itself (not user-uploaded) — put the
# actual font files in a `fonts/` folder next to bot.py in the repo. Either
# .ttf or .otf works fine (reportlab and Word both handle either format
# equally well) — just match the base filename below, extension doesn't
# matter. Path is anchored to this script's own location, not the working
# directory, so it resolves correctly regardless of where the process is
# launched from.
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
FONTS_DIR = os.path.join(BASE_DIR, "fonts")

def _find_font_file(base_name: str):
    """Looks for base_name with either extension in FONTS_DIR. Returns the
    path if found, else None — a missing file is handled gracefully
    wherever this is used, not treated as an error at import time."""
    for ext in (".otf", ".ttf", ".OTF", ".TTF"):
        path = os.path.join(FONTS_DIR, base_name + ext)
        if os.path.exists(path):
            return path
    return None

BUNDLED_FONTS = {
    "Comic Sans": {
        "regular": _find_font_file("ComicSans"),
        "bold":    _find_font_file("ComicSans-Bold"),
    },
    "Canva Sans": {
        "regular": _find_font_file("CanvaSans"),
        "bold":    _find_font_file("CanvaSans-Bold"),
    },
    "Times New Roman": {
        "regular": _find_font_file("TimesNewRoman"),
        "bold":    _find_font_file("TimesNewRoman-Bold"),
    },
    "Amaranth": {
        "regular": _find_font_file("Amaranth"),
        "bold":    _find_font_file("Amaranth-Bold"),
    },
}

# ── Replace with YOUR Telegram numeric user ID ──────────────────
# To find it: message @userinfobot on Telegram → it replies with your ID
ADMIN_ID = 940770584

# ── PDF/DOCX export whitelist ────────────────────────────────────
# /pdf_start (and therefore the whole PDF-collection flow — font/bg setup,
# gen_pdf/gen_docx export buttons) is only usable by the IDs listed here.
# Empty set = nobody but you has added their ID yet; add numeric Telegram
# user IDs (same way as ADMIN_ID above) as you approve people.
PDF_ALLOWED_USER_IDS: set[int] = set()

def _pdf_access_allowed(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    return uid is not None and (uid == ADMIN_ID or uid in PDF_ALLOWED_USER_IDS)

# ── Replace with your private GROUP's chat ID ────────────────────
# 1. Create the group, add this bot to it as a member (admin not required
#    unless you want it to survive being demoted/re-added later).
# 2. Send any message in the group, then send /storage_id in the SAME
#    group — the bot will reply with the chat ID (a negative number,
#    e.g. -1001234567890). Paste it below.
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
#   2. Forward any message from that channel to the bot in a private DM,
#      then send /quiz_channel_id right after — the bot replies with the ID.
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
            "Endocrine":      ["Bio", "Physio", "Patho", "Histo", "Pharma"],
            "Genitourinary":  ["Anatomy", "Physio", "Histo", "Patho", "Micro"],
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
# the bot as admin, send /storage_id inside it, paste the ID below.
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

# ── Curriculum structure for the quiz channels ─────────────────────
# Each year in YEARS (above) has its own "modules" dict in this same shape.
# Lecture titles posted in a year's quiz channel must be formatted as:
#   "<Module> - <Subject> Lecture <number>: <name>"
#   e.g. "Endocrine - Physio Lecture 3: Insulin Signaling"
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

# ── PDF collection flow ───────────────────────────────────────
MSG_PDF_ACCESS_DENIED = "🚫 مميزة PDF/DOCX مش متاحة لحسابك دلوقتي."
MSG_PDF_ASK_NAME = (
    "✏️ <b>اكتب اسم التوحفة الفنية (الملف) اللي عايزه:</b>\n"
    "<i>Lecture 1 Anatomy Questions</i>"
)
MSG_PDF_EMPTY = "❌ لا يوجد أسئلة محفوظة"
MSG_EXPORT_EMPTY = "❌ لا يوجد أسئلة محفوظة بعد"
MSG_EXPORT_GENERATING = "⏳ جاري توليد {kind} لـ {count} عنصر..."
MSG_PDF_GENERATING = "⏳ جاري توليد PDF لـ {count} عنصر..."
MSG_PDF_CAPTION = "📄 {count} سؤال — {name} ❤️\n\n <i>{quizzy_line}</i>"
MSG_DOCX_CAPTION = "📝 {count} سؤال — {name} ❤️\n\n <i>{quizzy_line}</i>"
MSG_DOCX_UNAVAILABLE = (
    "❌ DOCX export مش متاح دلوقتي (python-docx مش متثبت). "
    "استخدم PDF Export بدل كده، أو ثبّت python-docx وأعد التشغيل."
)
MSG_PDF_CLEARED = "🗑 تم قرار إزالة يا دولي"
MSG_EXPORT_CLEARED_ALL = "🗑 تم قرار إزاله يا دولي"
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
#   "streak":            int,
#   "streak_best":       int,
#   "last_active_date":  "YYYY-MM-DD" | null,
#   "pdfs_exported":     int,
#   "lecture_questions_answered":   int,
#   "lecture_questions_correct":    int,
#   "lecture_questions_incorrect":  int,
#   "lecture_correct_streak_current": int,
#   "lecture_correct_streak_best":    int,
#   "xp":                int,
#   "level":             int,
#   "achievements": {
#       "questions": 0-5,   # tiers unlocked
#       "streak":    0-5,
#       "pdfs":      0-5,
#       "speed":     0-5
#   }
# }
# ═══════════════════════════════════════════════════════════════
ANALYTICS_FILE          = "analytics.json"
ANALYTICS_BACKUP_MARKER = "🗄 QUIZICIAN_ANALYTICS_BACKUP"


# ── XP per action ────────────────────────────────────────────
XP_PER_QUESTION   = 10
XP_PDF_EXPORT     = 25   # flat bonus on top of per-question XP

# ── Level curve: level = floor(sqrt(xp / XP_LEVEL_FACTOR)) ──
XP_LEVEL_FACTOR   = 50   # level 1 = 50xp, 5 = 1250xp, 10 = 5000xp

# ── Achievement definitions ───────────────────────────────────
# Each achievement has 5 tiers: (threshold, label, xp_bonus, emoji)
# stat_key → the actual analytics entry field each category's threshold is
# checked against (kept separate from the ACHIEVEMENTS dict key so the
# lookup doesn't silently break if either name changes later).
ACHIEVEMENT_STAT_FIELD = {
    "questions":         "questions_created",
    "streak":            "streak",
    "pdfs":              "pdfs_exported",
    "speed":             "_session_max",
    "lecture_questions": "lecture_questions_answered",
    "lecture_streak":    "lecture_correct_streak_best",
}
ACHIEVEMENTS = {
    "questions": [
        (1,    "صانع الأسئلة I",    25,  "❓"),
        (100,  "صانع الأسئلة II",   75,  "❓"),
        (250,  "صانع الأسئلة III", 150,  "❓"),
        (500,  "صانع الأسئلة IV",  300,  "❓"),
        (1000, "صانع الأسئلة V",   600,  "❓"),
    ],
    "streak": [
        (3,   "ملتزم I",    20,  "🔥"),
        (7,   "ملتزم II",   50,  "🔥"),
        (30,  "ملتزم III", 150,  "🔥"),
        (100, "ملتزم IV",  400,  "🔥"),
        (365, "ملتزم V",   1000, "🔥"),
    ],
    "pdfs": [
        (1,  "صانع PDF I",    30,  "📚"),
        (5,  "صانع PDF II",   80,  "📚"),
        (10, "صانع PDF III", 175,  "📚"),
        (25, "صانع PDF IV",  400,  "📚"),
        (50, "صانع PDF V",   800,  "📚"),
    ],
    "speed": [
        (5,  "سريع I",    20,  "⚡"),
        (10, "سريع II",   50,  "⚡"),
        (20, "سريع III", 125,  "⚡"),
        (30, "سريع IV",  250,  "⚡"),
        (50, "سريع V",   500,  "⚡"),
    ],
    "lecture_questions": [
        (1,    "طالب مجتهد I",    25,  "🎓"),
        (100,  "طالب مجتهد II",   75,  "🎓"),
        (250,  "طالب مجتهد III", 150,  "🎓"),
        (500,  "طالب مجتهد IV",  300,  "🎓"),
        (1000, "طالب مجتهد V",   600,  "🎓"),
    ],
    "lecture_streak": [
        (5,  "دقة I",    20,  "🎯"),
        (10, "دقة II",   50,  "🎯"),
        (20, "دقة III", 125,  "🎯"),
        (30, "دقة IV",  250,  "🎯"),
        (50, "دقة V",   500,  "🎯"),
    ],
}

LEVEL_TITLES = {
    0:  "مبتدئ",
    1:  "متعلم",
    3:  "نشيط",
    5:  "محترف",
    8:  "خبير",
    12: "أستاذ",
    17: "أسطورة",
}

def _level_title(level: int) -> str:
    title = LEVEL_TITLES[0]
    for threshold, t in LEVEL_TITLES.items():
        if level >= threshold:
            title = t
    return title

def _xp_to_level(xp: int) -> int:
    import math
    return int(math.floor(math.sqrt(xp / XP_LEVEL_FACTOR)))

def _level_xp_range(level: int) -> tuple[int, int]:
    """(xp_start, xp_end) for this level."""
    return level ** 2 * XP_LEVEL_FACTOR, (level + 1) ** 2 * XP_LEVEL_FACTOR

def _blank_entry() -> dict:
    return {
        "questions_created": 0,
        "streak":            0,
        "streak_best":       0,
        "last_active_date":  None,
        "pdfs_exported":     0,
        "lecture_questions_answered":   0,
        "lecture_questions_correct":    0,
        "lecture_questions_incorrect":  0,
        "lecture_correct_streak_current": 0,
        "lecture_correct_streak_best":    0,
        "xp":                0,
        "level":             0,
        "achievements":      {k: 0 for k in ACHIEVEMENTS},
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
    for k in ("questions_created", "streak", "streak_best", "pdfs_exported", "lecture_questions_answered",
              "lecture_questions_correct", "lecture_questions_incorrect", "xp", "level"):
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
ANALYTICS_BACKUP_MIN_INTERVAL = 300  # seconds

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
    return entry

def _year_leaderboard(year_class: str, limit: int = 15) -> list[dict]:
    """Top users in one Year/Class cohort (SETTINGS' year_class — see the
    onboarding question, NOT the quiz-browsing YEARS), ranked by all-time
    lecture_questions_correct, highest first (ties broken by fewer
    incorrect, so someone who got there more efficiently ranks above
    someone who needed more attempts to reach the same correct count).
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
        rows.append({
            "user_id":  uid,
            "name":     get_nickname(uid) or entry.get("telegram_name") or f"مستخدم #{uid % 10000}",
            "correct":  correct,
            "incorrect": entry.get("lecture_questions_incorrect", 0),
            "level":    entry.get("level", 0),
        })
    rows.sort(key=lambda r: (-r["correct"], r["incorrect"]))
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
    """Add XP, recalculate level. Returns new level if levelled up, else 0."""
    entry["xp"] += amount
    new_level    = _xp_to_level(entry["xp"])
    levelled_up  = new_level > entry["level"]
    entry["level"] = new_level
    return new_level if levelled_up else 0

def _check_achievements(entry: dict, stat_key: str) -> list[dict]:
    """Check one stat against its achievement tiers. Returns list of newly
    unlocked tiers as dicts with keys: name, emoji, xp_bonus, tier (1-5)."""
    field    = ACHIEVEMENT_STAT_FIELD.get(stat_key, stat_key)
    value    = entry.get(field, 0)
    tiers    = ACHIEVEMENTS[stat_key]
    current  = entry["achievements"][stat_key]
    unlocked = []
    for i, (threshold, name, xp_bonus, emoji) in enumerate(tiers):
        tier = i + 1
        if tier <= current:
            continue
        if value >= threshold:
            entry["achievements"][stat_key] = tier
            unlocked.append({"name": name, "emoji": emoji,
                              "xp_bonus": xp_bonus, "tier": tier})
            _award_xp(entry, xp_bonus)
        else:
            break   # tiers are ordered — no point checking higher ones
    return unlocked

async def _record_activity(user_id: int, questions_delta: int = 0,
                     pdfs_delta: int = 0, session_questions: int = 0,
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
    entry["pdfs_exported"]     += pdfs_delta

    # ── XP for raw actions ───────────────────────────────────
    xp_earned = questions_delta * XP_PER_QUESTION + pdfs_delta * XP_PDF_EXPORT
    new_level  = _award_xp(entry, xp_earned) if xp_earned else 0

    # ── achievement checks ───────────────────────────────────
    newly_unlocked = []
    newly_unlocked += _check_achievements(entry, "questions")
    newly_unlocked += _check_achievements(entry, "streak")
    newly_unlocked += _check_achievements(entry, "pdfs")

    # speed: session_questions is how many were made in one PDF session
    if session_questions:
        # temporarily set a "session_max" so we can check the speed tier
        # using the same threshold logic; we don't persist session count
        prev = entry.get("_session_max", 0)
        if session_questions > prev:
            entry["_session_max"] = session_questions
            newly_unlocked += _check_achievements(entry, "speed")

    # level-up might also happen from achievement XP bonuses
    final_level = _xp_to_level(entry["xp"])
    if final_level > entry["level"]:
        entry["level"] = final_level
        new_level = final_level

    if persist:
        await save_analytics()
    return {"achievements": newly_unlocked, "level_up": new_level}

async def _announce_events(context, chat_id: int, events: dict, settings_uid: int | None = None):
    """Send achievement unlocks and level-up notifications to chat_id (the
    delivery target — a DM or, for a PDF export, possibly a group chat).
    settings_uid is whose achievement_notifs setting to check; defaults to
    chat_id itself, since every call site except the PDF exporter has the
    delivery target and the real Telegram user be the same id. Level-ups
    are a separate, more significant event and always sent regardless."""
    if settings_uid is None:
        settings_uid = chat_id
    msgs = []

    if get_achievement_notifs_enabled(settings_uid):
        for ach in events.get("achievements", []):
            stars = "⭐" * ach["tier"]
            msgs.append(
                f"{ach['emoji']} <b>إنجاز جديد!</b>\n"
                f"<b>{ach['name']}</b> {stars}\n"
                f"<i>+{ach['xp_bonus']} XP</i>"
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
RESTORE_OK = {"analytics": True, "settings": True, "storage": True, "lecture_results": True, "mistakes_bank": True, "report_threads": True}
RESTORE_OK.update({f"quiz_{y}": True for y in YEARS})  # one flag per year's quiz index

async def _run_restore_with_retries(app, key: str, label: str, do_restore, not_found_hint: str | None = None):
    """Runs do_restore() (an async no-arg callable doing the actual
    get_chat/get_file/parse work for one system) up to
    RESTORE_MAX_ATTEMPTS times with a short backoff between attempts —
    these failures are almost always a transient Telegram API timeout,
    so a couple retries clear most of them without ever bothering the
    admin. Only if every attempt fails do we DM the admin and mark this
    system unsynced for the session (see RESTORE_OK)."""
    for attempt in range(1, RESTORE_MAX_ATTEMPTS + 1):
        try:
            await do_restore()
            RESTORE_OK[key] = True
            return
        except Exception as e:
            hint = not_found_hint if (not_found_hint and "chat not found" in str(e).lower()) else None
            print(f"{label.upper()} RESTORE ERROR (attempt {attempt}/{RESTORE_MAX_ATTEMPTS}):", hint or e)
            if attempt < RESTORE_MAX_ATTEMPTS:
                await asyncio.sleep(RESTORE_RETRY_DELAY_BASE * attempt)
            else:
                RESTORE_OK[key] = False
                await _notify_admin_sync_failure(app, label, hint or e)

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

async def restore_analytics_from_channel(app):
    global _analytics_backup_msg_id
    if not ANALYTICS_GROUP_ID:
        return

    async def _do():
        global _analytics_backup_msg_id
        chat   = await app.bot.get_chat(ANALYTICS_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            return
        if (pinned.caption or "") != ANALYTICS_BACKUP_MARKER:
            return
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        restored = json.loads(bytes(raw).decode("utf-8"))
        ANALYTICS.update(_clean_analytics_dict(restored))
        await save_analytics()
        _analytics_backup_msg_id = pinned.message_id
        print(f"Restored analytics: {len(ANALYTICS)} user(s).")

    await _run_restore_with_retries(app, "analytics", "Analytics", _do)



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
#   "language": str,  # "ar" | "en" — UI language, see get_language() /
#                     # STRINGS / t() in the I18N section. Defaults to "ar"
#                     # (matches the bot's current all-Arabic behavior);
#                     # nothing reads this yet beyond the Settings toggle
#                     # itself until strings get migrated onto t().
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
        "language": "ar",      # "ar" | "en" — see get_language() / I18N section
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

# Same debounce pattern as analytics — local save_settings() always
# happens immediately; only the channel mirror is throttled.
_last_settings_backup_at: float = 0.0
SETTINGS_BACKUP_MIN_INTERVAL = 300  # seconds

def _get_settings_entry(user_id: int) -> dict:
    key   = str(user_id)
    entry = SETTINGS.setdefault(key, _blank_settings_entry())
    # backfill missing keys for users created before this system
    for k, v in _blank_settings_entry().items():
        entry.setdefault(k, v)
    return entry

def get_nickname(user_id: int) -> str | None:
    return SETTINGS.get(str(user_id), {}).get("nickname")

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
    return f"{year_label(year_class)} (Class {YEAR_CLASS_NUMBER[year_class]})"

SUPPORTED_LANGUAGES = ("ar", "en")

def get_language(user_id: int) -> str:
    # Defaults to "ar" for anyone not yet in SETTINGS (or missing the
    # key) — matches _blank_settings_entry()'s default and the bot's
    # current all-Arabic behavior, so nobody's language silently changes
    # just because this field is new.
    return SETTINGS.get(str(user_id), {}).get("language", "ar")

# ═══════════════════════════════════════════════════════════════
# I18N — bilingual string lookup (infrastructure only for now)
#
# The bot's ~350 user-facing messages are still hardcoded Arabic
# throughout the handlers below — this section is just the scaffolding
# (the setting, the toggle, and the lookup helper) for migrating them
# onto STRINGS/t() gradually, one message at a time, rather than a
# single big-bang rewrite. Nothing is translated yet.
#
# To migrate a message:
#   1. Add a key to STRINGS below, e.g.:
#        "mystats_no_data": {
#            "ar": "📊 لسه معندكش إحصائيات. ابعت أسئلة وهتظهر هنا!",
#            "en": "📊 No stats yet. Send some questions and they'll show up here!",
#        },
#   2. Replace the hardcoded literal at its call site with
#        t("mystats_no_data", user_id)
#      — or t("key", user_id, name=x) if the original string had an
#      f-string value baked in; use {name} inside the STRINGS text and
#      pass name=x as a kwarg, the same way str.format works.
#
# Deliberately ONE shared dict (not one file per language) — see the
# /report_issue-adjacent conversation this came out of: two files means
# two things to keep in sync by hand, and it's easy for one language to
# quietly fall behind. Keeping ar/en side by side per key makes a
# missing translation obvious at a glance instead of a silent gap in a
# second file nobody's looking at.
STRINGS: dict[str, dict[str, str]] = {
    # populated incrementally as messages get migrated — see the how-to above
}

def t(key: str, user_id: int, **kwargs) -> str:
    """Looks up STRINGS[key] for this user's language (get_language) and
    fills in any {placeholder} kwargs, the same way str.format works.
    Falls back to Arabic if this key hasn't been given an "en" entry yet
    (so migrating one string at a time never breaks anything for users
    who've already switched to English), and falls back to a visibly
    broken placeholder — not a crash, not a silent blank — if the key
    doesn't exist in STRINGS at all, so a typo'd key is obvious in the
    chat immediately instead of quietly showing nothing."""
    lang  = get_language(user_id)
    entry = STRINGS.get(key)
    if not entry:
        return f"[[missing string: {key}]]"
    text = entry.get(lang) or entry.get("ar") or f"[[missing string: {key}]]"
    return text.format(**kwargs) if kwargs else text

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

async def restore_settings_from_channel(app):
    global _settings_backup_msg_id
    if not SETTINGS_GROUP_ID:
        return

    async def _do():
        global _settings_backup_msg_id
        chat   = await app.bot.get_chat(SETTINGS_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            return
        if (pinned.caption or "") != SETTINGS_BACKUP_MARKER:
            return
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        SETTINGS.update(json.loads(bytes(raw).decode("utf-8")))
        await save_settings()
        _settings_backup_msg_id = pinned.message_id
        print(f"Restored settings: {len(SETTINGS)} user(s).")

    await _run_restore_with_retries(app, "settings", "Settings", _do)

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
LECTURE_RESULTS_BACKUP_MIN_INTERVAL = 300  # seconds

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

# Same debounce pattern as lecture results — local save always happens
# immediately; only the channel mirror is throttled.
_last_mistakes_bank_backup_at: float = 0.0
MISTAKES_BANK_BACKUP_MIN_INTERVAL = 300  # seconds

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
    await save_mistakes_bank()
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

async def restore_mistakes_bank_from_channel(app):
    global _mistakes_bank_backup_msg_id
    if not MISTAKES_BANK_GROUP_ID:
        return

    async def _do():
        global _mistakes_bank_backup_msg_id
        chat   = await app.bot.get_chat(MISTAKES_BANK_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            return
        if (pinned.caption or "") != MISTAKES_BANK_BACKUP_MARKER:
            return
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        restored = json.loads(bytes(raw).decode("utf-8"))
        clean    = [m for m in restored if _is_valid_mistake_entry(m)]
        if len(clean) != len(restored):
            print(f"MISTAKES BANK: dropped {len(restored) - len(clean)} malformed entr(y/ies) from the channel backup on restore.")
        MISTAKES_BANK[:] = clean
        _reindex_mistakes_bank()
        await save_mistakes_bank()
        _mistakes_bank_backup_msg_id = pinned.message_id
        print(f"Restored mistakes bank: {len(MISTAKES_BANK)} question(s).")

    await _run_restore_with_retries(app, "mistakes_bank", "Mistakes bank", _do)

# ═══════════════════════════════════════════════════════════════
# DAILY QUIZ — 💥Daily Quiz💥: 10 questions total — up to 3 pulled from the
# user's own MISTAKES_BANK entries, topped up with random questions from
# random subjects (any subject can contribute more than one — this is not
# a one-per-subject pick) so the run is always 10 long even when the
# user's mistakes bank is empty or short. Both slices are restricted to the
# admin-set /daily_module scope when one is set (see get_daily_quiz_scope),
# or span every configured year/module otherwise. Pushed to everyone at 2pm Cairo time
# once a day (see the job_queue.run_daily call in MAIN); the push itself
# is just a button — tapping it is what actually starts the quiz and is
# gated to once per person per day via each user's settings
# "daily_quiz_last_date".
#
# Deliberately its own session type (DAILY_QUIZ_SESSIONS), separate from
# LECTURE_SESSIONS, rather than shoehorned into the lecture-session shape:
# a lecture session's dead-poll pruning, legacy-content recovery, and
# result-recording are all keyed to one specific year+lecture_key, which
# doesn't make sense for a session mixing many years/lectures/subjects at
# once. A Daily Quiz question is fully self-contained (question/options/
# correct_id baked in directly, same shape as a MISTAKES_BANK entry) so
# delivery never needs to touch any year's live channel/state at all.
# ═══════════════════════════════════════════════════════════════
DAILY_QUIZ_SESSIONS = {}   # user_id -> {"queue": [question dict, ...], "current_poll_id",
                           #             "current_correct_id", "current_message_id",
                           #             "total", "answered", "correct", "xp_earned"}

DAILY_QUIZ_TOTAL_COUNT    = 10  # total questions in one Daily Quiz run — always
                                 # topped up from the random pool so a run is
                                 # this long even when the mistakes bank is empty
DAILY_QUIZ_MISTAKES_COUNT = 3   # cap on how many of those can come from the mistakes bank

# Push time for the daily 💥Daily Quiz💥 button (see job_queue.run_daily in
# MAIN, and next_daily_quiz_time() / /time below — all three read from
# these two so the schedule only ever needs to change in one place).
DAILY_QUIZ_TZ   = ZoneInfo("Africa/Cairo")
DAILY_QUIZ_HOUR = 14
DAILY_QUIZ_MIN  = 0

def next_daily_quiz_time() -> datetime:
    """The next upcoming 2pm-Cairo push moment — today's if it hasn't
    happened yet, otherwise tomorrow's."""
    now = datetime.now(DAILY_QUIZ_TZ)
    today_push = now.replace(hour=DAILY_QUIZ_HOUR, minute=DAILY_QUIZ_MIN, second=0, microsecond=0)
    return today_push if now < today_push else today_push + timedelta(days=1)

_DAILY_QUIZ_POOL_CACHE: dict = {"pool": None, "built_at": 0.0, "scope_key": None}
_DAILY_QUIZ_POOL_CACHE_TTL_SECONDS = 120
# Rebuilding this pool means: for every (module, subject) pair, scanning
# the ENTIRE year's QUIZ_INDEX to find lectures matching that pair (see
# ready_lecture_keys), on top of a QUIZ_POLL_STATUS scan. That's fine once
# — it's expensive when 700 students all tap "Daily Quiz" inside the same
# push window and each one triggers a fresh rebuild. The pool doesn't
# depend on which student is asking, so it's cached for a couple of
# minutes; a lecture that gets closed mid-window just joins the pool the
# next time the cache refreshes rather than instantly, which is fine for
# a once-a-day quiz. Invalidated early if the admin changes /daily_module
# scope, so a scope change is never stuck behind a stale cache.
def _daily_quiz_subject_pool() -> dict:
    """Every ready (closed-poll) question mid, across all subjects,
    grouped by (year, module, subject) — the pool build_daily_quiz_questions
    draws its 7 random questions from (any subject can contribute more
    than one; this is just how the mids are organized so a scope filter
    can narrow it before picking). Normally spans every configured
    year/module; if an admin has set a scope via /daily_module, narrowed
    to just that one module. Cached briefly — see _DAILY_QUIZ_POOL_CACHE_TTL_SECONDS."""
    scope = get_daily_quiz_scope()
    scope_key = (scope["year"], scope["module"]) if scope else None

    now = time.monotonic()
    cache = _DAILY_QUIZ_POOL_CACHE
    if (cache["pool"] is not None
            and cache["scope_key"] == scope_key
            and now - cache["built_at"] < _DAILY_QUIZ_POOL_CACHE_TTL_SECONDS):
        return cache["pool"]

    years = [scope["year"]] if scope else configured_years()

    pool = {}   # (year, module, subject) -> [mid, ...]
    for year in years:
        if year not in configured_years():
            continue   # scoped year's channel got unconfigured since — skip rather than crash
        closed_message_ids = {v["message_id"] for v in QUIZ_POLL_STATUS[year].values() if v["closed"]}
        modules = [scope["module"]] if scope else ready_modules(year)
        for module in modules:
            for subject in ready_subjects(year, module):
                mids = []
                for lecture_key in ready_lecture_keys(year, module, subject):
                    ids = QUIZ_INDEX[year][lecture_key]["ids"]
                    mids.extend(mid for mid in ids if mid in closed_message_ids)
                if mids:
                    pool[(year, module, subject)] = mids

    cache["pool"] = pool
    cache["built_at"] = now
    cache["scope_key"] = scope_key
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

async def build_daily_quiz_questions(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> list:
    """The full DAILY_QUIZ_TOTAL_COUNT-question set for one Daily Quiz run:
    up to DAILY_QUIZ_MISTAKES_COUNT from this user's own mistakes bank,
    then topped up with random questions pulled from random subjects (a
    subject can contribute more than one — this is NOT one-per-subject) so
    the run is always DAILY_QUIZ_TOTAL_COUNT questions long — even when
    this user's mistakes bank is empty or short, the random pool fills the
    rest. Both slices respect the admin-set /daily_module scope, if any.
    Falls short of DAILY_QUIZ_TOTAL_COUNT gracefully if there isn't enough
    ready random content yet — callers just get a shorter (or empty)
    list."""
    mistakes = _scoped_mistakes_bank(user_id)
    mistake_sample = random.sample(mistakes, k=min(DAILY_QUIZ_MISTAKES_COUNT, len(mistakes))) if mistakes else []
    questions = await _resolve_mistakes(context, mistake_sample) if mistake_sample else []

    # Whatever the mistakes bank didn't cover (including all of it, when
    # empty) gets filled from the random pool below, so the total is always
    # DAILY_QUIZ_TOTAL_COUNT rather than a fixed random count + leftover mistakes.
    subject_pool = _daily_quiz_subject_pool()
    # Flatten to one (year, module, subject, mid) tuple per ready question,
    # so picking random_needed is a plain random sample over individual
    # questions — not a pick-a-subject-then-one-question-from-it scheme,
    # which is what was capping this to one question per subject before.
    all_mids = [
        (year, module, subject, mid)
        for (year, module, subject), mids in subject_pool.items()
        for mid in mids
    ]
    random.shuffle(all_mids)

    # One poll-status index per distinct year touched, built once here
    # rather than _snapshot_from_mid scanning QUIZ_POLL_STATUS[year] fresh
    # for every one of up to random_needed questions.
    status_by_mid_by_year: dict = {}

    for year, module, subject, mid in all_mids:
        if len(questions) >= DAILY_QUIZ_TOTAL_COUNT:
            break
        if year not in status_by_mid_by_year:
            status_by_mid_by_year[year] = _poll_status_index(year)
        snap = await _snapshot_from_mid(context, year, mid, module, subject, status_by_mid_by_year[year])
        if snap:
            questions.append(snap)

    random.shuffle(questions)
    return questions

async def _deliver_next_daily_question(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict) -> bool:
    """Same idea as _deliver_next_lecture_question, but for a self-
    contained Daily Quiz question dict — no mid/channel lookups needed,
    everything required is already sitting in the queue entry. Sets
    session['current_*']. Returns whether a question went out."""
    if not session["queue"]:
        session["current_poll_id"] = None
        session["current_correct_id"] = None
        session["current_message_id"] = None
        return False
    q = session["queue"].pop(0)
    timer_seconds = get_question_timer_seconds(user_id)
    try:
        msg = await context.bot.send_poll(
            chat_id=user_id, question=q["question"], options=q["options"],
            type="quiz", correct_option_id=q["correct_option_id"], is_anonymous=False,
            explanation=(q.get("explanation") or None),
            open_period=(timer_seconds or None),
        )
    except Exception as e:
        print(f"Couldn't send daily quiz question: {e}")
        return await _deliver_next_daily_question(context, user_id, session)   # try the next one
    session["current_poll_id"]    = msg.poll.id
    session["current_correct_id"] = q["correct_option_id"]
    session["current_message_id"] = msg.message_id
    return True

async def _advance_daily_quiz_session(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict, is_correct: bool, message_id: int | None):
    """Daily Quiz's counterpart to _advance_lecture_session: same XP
    (15/5/+25 completion) and same lecture_questions/lecture_streak
    achievement tracking (a Daily Quiz question is still practice, so it
    counts toward those same stats) — but no lecture_key, so no dead-poll
    pruning, no legacy-content recovery, and no _record_lecture_result/
    leaderboard involvement at all; there's no single lecture for this to
    be an "attempt" of."""
    session["answered"] += 1
    session["correct"] = session.get("correct", 0) + (1 if is_correct else 0)

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

    events["achievements"] += _check_achievements(user_entry, "lecture_questions")
    events["achievements"] += _check_achievements(user_entry, "lecture_streak")

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
            f"💥 <b>خلصت الـ Daily Quiz!</b>\n\n"
            f"✅ صح: {correct}\n"
            f"❌ غلط: {incorrect}\n"
            f"📊 نسبة: {pct}%\n"
            f"📝 عدد الأسئلة: {session['answered']}/{total}\n"
            f"✨ XP: <b>+{session['xp_earned']}</b>\n\n"
            f"{_next_daily_quiz_line()}"
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
        DAILY_QUIZ_SESSIONS.pop(user_id, None)

def get_daily_quiz_last_date(user_id: int) -> str | None:
    return SETTINGS.get(str(user_id), {}).get("daily_quiz_last_date")

# ── Admin-set Daily Quiz scope ──────────────────────────────────
# By default both the random-questions slice and the mistakes-bank
# slice draw from every configured year/module. An admin can narrow both
# to one specific module (e.g. whatever's currently being taught) via
# /daily_module — see _daily_quiz_subject_pool and _scoped_mistakes_bank.
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

async def start_daily_quiz(context: ContextTypes.DEFAULT_TYPE, user_id: int, message=None) -> None:
    """Shared by the 💥Daily Quiz💥 button and (if ever wanted) any other
    entry point. message, if given, gets edited with the "starting..."
    line instead of a fresh message being sent (matches the lecture-start
    button pattern). Once-per-day gating happens here, keyed off the
    caller's local calendar date at the time they tap — not the push
    time — so someone who gets the 2pm ping but taps it at 11pm still
    only gets today's quiz once."""
    today = _today()
    if get_daily_quiz_last_date(user_id) == today:
        text = f" !خلصت الكويز اليومي بتاع النهاردة خلاص\n\n{_next_daily_quiz_line()}"
        if message:
            await message.edit_text(text)
        else:
            await context.bot.send_message(chat_id=user_id, text=text)
        return

    questions = await build_daily_quiz_questions(context, user_id)
    if not questions:
        text = "check again later :/"
        if message:
            await message.edit_text(text)
        else:
            await context.bot.send_message(chat_id=user_id, text=text)
        return

    entry = _get_settings_entry(user_id)
    entry["daily_quiz_last_date"] = today
    await save_settings()
    await backup_settings_to_channel(context)

    session = {
        "queue": questions, "current_poll_id": None, "current_correct_id": None,
        "current_message_id": None, "total": len(questions), "answered": 0, "correct": 0,
    }
    DAILY_QUIZ_SESSIONS[user_id] = session

    text = f"💥 <b>Daily Quiz</b> — {len(questions)} أس~لة من مواد مختلفة 👇"
    if message:
        await message.edit_text(text, parse_mode=ParseMode.HTML)
    else:
        await context.bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.HTML)

    sent = await _deliver_next_daily_question(context, user_id, session)
    if not sent:
        DAILY_QUIZ_SESSIONS.pop(user_id, None)
        await context.bot.send_message(chat_id=user_id, text="⚠️ حصلت مشكلة في تجهيز الأسئلة — جرب تاني.")

async def _daily_quiz_push_job(context: ContextTypes.DEFAULT_TYPE):
    """The 2pm-Cairo push (see job_queue.run_daily in MAIN): just a
    button in each user's chat, not an auto-started quiz — tapping it is
    what calls start_daily_quiz and applies the once-per-day gate."""
    for uid in list(USERS):
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

async def _advance_mistakes_retake_session(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict, is_correct: bool, message_id: int | None):
    """Mistakes-retake counterpart to _advance_daily_quiz_session — same
    XP/streak/achievement bookkeeping, its own completion summary text."""
    session["answered"] += 1
    session["correct"] = session.get("correct", 0) + (1 if is_correct else 0)

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

    events["achievements"] += _check_achievements(user_entry, "lecture_questions")
    events["achievements"] += _check_achievements(user_entry, "lecture_streak")

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

async def restore_storage_from_channel(app):
    """Runs once on startup — rebuilds USERS + STORAGE_INDEX from the
    storage group's pinned backup if the local cache is missing/stale."""
    if not STORAGE_GROUP_ID:
        return

    async def _do():
        chat   = await app.bot.get_chat(STORAGE_GROUP_ID)
        pinned = chat.pinned_message
        if pinned and pinned.document and (pinned.caption or "") == STORAGE_BACKUP_MARKER:
            tg_file = await app.bot.get_file(pinned.document.file_id)
            raw     = await tg_file.download_as_bytearray()
            payload = json.loads(bytes(raw).decode("utf-8"))
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
    await _run_restore_with_retries(app, "storage", "Storage", _do, not_found_hint=not_found_hint)

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

# Matches "<Subject> Lecture <number>", e.g. "Physio Lecture 3"
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
            "مثال: <code>Endocrine - Physio Lecture 3: Insulin Signaling</code>"
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
            "مثال: <code>Physio Lecture 3</code>"
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

async def restore_quiz_from_channel(app, year: str):
    """Runs once on startup per year — rebuilds that year's lecture/quiz
    index from its quiz channel's pinned backup if the local cache is
    missing/stale."""
    channel_id = year_channel_id(year)
    if not channel_id:
        return

    async def _do():
        chat   = await app.bot.get_chat(channel_id)
        pinned = chat.pinned_message
        if pinned and pinned.document and (pinned.caption or "") == QUIZ_BACKUP_MARKER:
            tg_file = await app.bot.get_file(pinned.document.file_id)
            raw     = await tg_file.download_as_bytearray()
            payload = json.loads(bytes(raw).decode("utf-8"))
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
    await _run_restore_with_retries(app, f"quiz_{year}", f"Quiz index ({year})", _do, not_found_hint=not_found_hint)

# ═══════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════
PDF_BUFFER             = {}    # user_id -> list of item dicts
PDF_NAMES              = {}    # user_id -> str
AWAITING_NAME          = {}    # user_id -> True
PDF_FONT_PATH          = {}    # user_id -> path to a regular-weight .ttf/.otf, or absent for the default font
PDF_FONT_BOLD_PATH     = {}    # user_id -> path to that font's bold weight, if one's available (presets only —
                                # a single user upload has no bold companion, so bold text just reuses it)
PDF_BG_IMAGE_PATH      = {}    # user_id -> path to an uploaded per-page background image, or absent for none
AWAITING_FONT          = {}    # user_id -> True, while the PDF-setup flow is waiting on a font file/skip
AWAITING_BG            = {}    # user_id -> True, while the PDF-setup flow is waiting on a background image/skip
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

PROGRESS_MSG_ID        = {}    # user_id -> message_id of the live progress message
PENDING_IMAGE          = {}    # user_id -> local path of an image awaiting its question
CLARIFY_QUEUE          = {}    # user_id -> list of PDF_BUFFER indices awaiting a correct-answer tap
POLL_WATCH             = {}    # poll_id -> (user_id, item_index) for passive auto-detection
PENDING_EDIT           = {}    # user_id -> {"index": int, "field": "q"/"title"/"content"/"option", "opt_index": int?}
                                # awaiting free-text replacement for one field of a just-added question
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

# ── /report_issue support ────────────────────────────────────────
# REPORT_THREADS mirrors the MISTAKES_BANK persistence pattern exactly:
# local JSON file, plus a pinned backup in REPORT_ISSUE_GROUP_ID that gets
# replaced (upload + pin + delete old pin) on every change and restored
# from on startup. See restore_report_threads_from_channel /
# backup_report_threads_to_channel below, and their registration
# alongside every other system's restore/backup calls near MAIN.
AWAITING_REPORT_ISSUE  = {}    # user_id -> True, while waiting on the user's issue text after /report_issue
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

_report_threads_backup_msg_id: int | None = None
_last_report_threads_backup_at: float = 0.0
REPORT_THREADS_BACKUP_MIN_INTERVAL = 300   # seconds — same debounce as mistakes bank; local save is never throttled

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

async def restore_report_threads_from_channel(app):
    global _report_threads_backup_msg_id
    if not REPORT_ISSUE_GROUP_ID:
        return

    async def _do():
        global _report_threads_backup_msg_id
        chat   = await app.bot.get_chat(REPORT_ISSUE_GROUP_ID)
        pinned = chat.pinned_message
        if not pinned or not pinned.document:
            return
        if (pinned.caption or "") != REPORT_THREADS_BACKUP_MARKER:
            return
        tg_file = await app.bot.get_file(pinned.document.file_id)
        raw     = await tg_file.download_as_bytearray()
        restored = json.loads(bytes(raw).decode("utf-8"))
        REPORT_THREADS.clear()
        REPORT_THREADS.update({int(k): v for k, v in restored.items()})
        await save_report_threads()
        _report_threads_backup_msg_id = pinned.message_id
        print(f"Restored report threads: {len(REPORT_THREADS)} thread(s).")

    await _run_restore_with_retries(app, "report_threads", "Report threads", _do)

# ═══════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════
MAX_QUESTIONS_PER_MSG = 50
TELEGRAM_Q_LIMIT      = 300   # max chars in poll question field
TELEGRAM_DESC_LIMIT   = 200   # max chars in poll description (shown above question)
TELEGRAM_EX_LIMIT     = 200   # max chars in poll explanation (shown after answering)
PDF_MAX_IMG_WIDTH     = 13 * cm

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

def parse_written_question(block: str):
    block = strip_spoiler_markers(block)
    lines = [l.rstrip() for l in block.split("\n") if l.strip()]
    if len(lines) < 2:
        return None
    title = re.sub(r'[\""\']+$', "", lines[0]).strip()
    content_lines = lines[1:]
    content = "\n".join(content_lines).strip()
    if not content:
        return None
    if content.startswith(".") and content.endswith("."):
        content = content[1:-1].strip()
        return title, content
    if content:
        return title, content
    return None

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

def options_too_long(options: list) -> bool:
    """Check if any single option exceeds Telegram's 100-char option limit."""
    return any(len(o) > 100 for o in options)

def make_letter_only_options(count: int) -> list:
    """Return ['A', 'B', 'C', ...] for poll when answers are too long."""
    return [string.ascii_uppercase[i] for i in range(count)]

def _cleanup_images(user_id: int):
    import shutil
    img_dir = os.path.join(IMG_BASE_DIR, str(user_id))
    if os.path.exists(img_dir):
        shutil.rmtree(img_dir, ignore_errors=True)

def _clear_pending_image(user_id: int):
    """Drop any image that's still waiting for a question, deleting its file."""
    path = PENDING_IMAGE.pop(user_id, None)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

def _clear_pending_edit(user_id: int):
    """Drop any pending 'send new text for this field' state for this user."""
    PENDING_EDIT.pop(user_id, None)

def _clear_clarify_queue(user_id: int):
    """Drop any pending 'choose the correct answer' queue/watches for this user."""
    CLARIFY_QUEUE.pop(user_id, None)
    stale_poll_ids = [pid for pid, (uid, _) in POLL_WATCH.items() if uid == user_id]
    for pid in stale_poll_ids:
        POLL_WATCH.pop(pid, None)

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

    This is the ONLY place that builds/sends quiz polls in non-PDF mode, so
    forwarded polls, typed MCQs, and image-paired MCQs all share one code path.
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
# PROGRESS MESSAGE BUILDER
# ═══════════════════════════════════════════════════════════════
def build_progress_text(items: list, latest_label: str = "") -> str:
    count   = len(items)
    bar_len = 4   # smaller block = the bar fills up faster (2 items = 50% full)

    if count == 0:
        filled = 0
    else:
        filled = count % bar_len or bar_len   # land on a full bar, not an empty one
    bar = "█" * filled + "░" * (bar_len - filled)

    type_counts = {"mcq": 0, "written": 0, "image": 0}
    for it in items:
        t = it.get("type", "mcq")
        if t in type_counts:
            type_counts[t] += 1

    breakdown = []
    if type_counts["mcq"]:
        breakdown.append(f"❓ {type_counts['mcq']} MCQ")
    if type_counts["written"]:
        breakdown.append(f"📝 {type_counts['written']} Written")
    if type_counts["image"]:
        breakdown.append(f"🖼 {type_counts['image']} Image")

    text = (
        f"📄 <b>PDF Collection Mode</b>\n"
        f"<code>{bar}</code>\n"
        f"Collected: <b>{count}</b> item{'s' if count != 1 else ''}"
    )
    if breakdown:
        text += f"\n{' · '.join(breakdown)}"
    return text

async def update_progress(context, user_id: int, chat_id: int, latest_label: str = ""):
    """Edit the existing progress message, or send a new one and store its id."""
    items    = PDF_BUFFER.get(user_id, [])
    text     = build_progress_text(items, latest_label)
    keyboard = export_keyboard()
    msg_id   = PROGRESS_MSG_ID.get(user_id)

    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return
        except Exception:
            pass  # message too old / deleted — fall through to send new

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    PROGRESS_MSG_ID[user_id] = sent.message_id

# ═══════════════════════════════════════════════════════════════
# KEYBOARD HELPERS
# ═══════════════════════════════════════════════════════════════
def export_keyboard():
    row = [InlineKeyboardButton("📄 Export as PDF", callback_data="gen_pdf")]
    if DOCX_AVAILABLE:
        row.append(InlineKeyboardButton("📝 Export as DOCX", callback_data="gen_docx"))
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton("✏️ Edit a Question", callback_data="edit_pick")],
        [InlineKeyboardButton("🗑 Clear & Cancel", callback_data="clear_pdf")],
    ])

def _item_preview_label(item: dict, max_len: int = 40) -> str:
    """First few words of a buffered item, for the edit-picker list."""
    if item["type"] == "written":
        text = item.get("title", "")
    elif item["type"] == "image":
        text = item.get("caption") or "(صورة من غير نص)"
    else:
        text = item.get("q", "")
    text = text.strip()
    return text[:max_len] + ("…" if len(text) > max_len else "")

def edit_pick_keyboard(items: list) -> InlineKeyboardMarkup:
    """Numbered grid (1, 2, 3...) — one button per buffered question, each
    jumping straight into the existing per-question edit menu (same
    revedit:{index}:open flow the old per-confirmation '✏️ تعديل' button
    used to open)."""
    buttons = [
        InlineKeyboardButton(str(i + 1), callback_data=f"revedit:{i}:open")
        for i in range(len(items))
    ]
    rows = [buttons[i:i + 6] for i in range(0, len(buttons), 6)]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="edit_pick_back")])
    return InlineKeyboardMarkup(rows)

def font_prompt_keyboard() -> InlineKeyboardMarkup:
    """Preset font buttons (bundled .otf files, see BUNDLED_FONTS) plus
    Skip — shown alongside the option to just upload a font file instead."""
    names = list(BUNDLED_FONTS.keys())
    rows  = [
        [InlineKeyboardButton(names[i], callback_data=f"font_preset:{i}") for i in range(0, 2)],
        [InlineKeyboardButton(names[i], callback_data=f"font_preset:{i}") for i in range(2, 4)],
        [InlineKeyboardButton("⏭ Skip", callback_data="font_skip")],
    ]
    return InlineKeyboardMarkup(rows)

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

def settings_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    def _tag(on: bool) -> str:
        return "🟢 On" if on else "🔴 Off"
    reactions  = get_reactions_enabled(user_id)
    auto_next  = get_auto_next_enabled(user_id)
    randomize  = get_randomize_enabled(user_id)
    ach_notifs = get_achievement_notifs_enabled(user_id)
    spaced_rep = get_spaced_repetition_enabled(user_id)
    timer      = get_question_timer_seconds(user_id)
    timer_tag  = "🔴 Off" if timer == 0 else f"🟢 {timer}s"
    yc_label   = year_class_label(get_year_class(user_id))
    lang       = get_language(user_id)
    lang_tag   = "English" if lang == "en" else "العربية"
    rows = [
        [InlineKeyboardButton("✏️ Edit Nickname", callback_data="edit_nickname")],
        [InlineKeyboardButton(f"📚 Year/Class: {yc_label}", callback_data="edit_year_class")],
        [InlineKeyboardButton(f"🌐 Language: {lang_tag}", callback_data="toggle_language")],
        [InlineKeyboardButton(f"🎭 Reactions: {_tag(reactions)}", callback_data="toggle_reactions")],
        [InlineKeyboardButton(f"⏭️ Auto-Next: {_tag(auto_next)}", callback_data="toggle_auto_next")],
        [InlineKeyboardButton(f"🔀 Randomize: {_tag(randomize)}", callback_data="toggle_randomize")],
        [InlineKeyboardButton(f"🏆 Achievement Alerts: {_tag(ach_notifs)}", callback_data="toggle_achievement_notifs")],
        [InlineKeyboardButton(f"🔁 Spaced Repetition: {_tag(spaced_rep)}", callback_data="toggle_spaced_repetition")],
        [InlineKeyboardButton(f"⏱️ Question Timer: {timer_tag}", callback_data="toggle_question_timer")],
        [InlineKeyboardButton("🗑 Clear Mistake Bank", callback_data="clear_mistakes_bank_ask")],
        [InlineKeyboardButton("🏠 Back to Home",  callback_data="back_home")],
    ]
    return InlineKeyboardMarkup(rows)

# ═══════════════════════════════════════════════════════════════
# MENU TEXT CONTENT
# ═══════════════════════════════════════════════════════════════
HOW_TO_USE_TEXT = (
    "📚 <b>How To Use — Quizician Bot</b>\n\n"
    "<b>1) Normal MCQ</b>\n"
    "<code>Question?\n"
    "a) Option A\n"
    "b) Option B z   ← mark correct with z\n"
    "c) Option C\n"
    "ex: Explanation here (optional)</code>\n\n"
    "<b>2) Single-line MCQ</b>\n"
    "<code>Question? a) A b) B z c) C</code>\n\n"
    "<b>3) Written / Flashcard</b>\n"
    "<code>Title\n"
    ".answer line 1\n"
    "answer line 2.</code>\n"
    "<i>Wrap the answer between dots.</i>\n\n"
    "<b>4) Forwarded Quiz Polls</b>\n"
    "Forward any Telegram quiz — the bot re-sends it with the correct answer preserved.\n\n"
    "<b>5) PDF / DOCX Mode</b>\n"
    "Use /pdf_start, collect items, then export.\n\n"
    "😴 /sleep — mute the bot until /start"
)

# ═══════════════════════════════════════════════════════════════
# PDF BUILDER
# ═══════════════════════════════════════════════════════════════
def build_pdf(items: list, doc_title: str = "questions", font_path: str = None,
              font_bold_path: str = None, bg_image_path: str = None) -> BytesIO:
    buffer = BytesIO()

    # Custom font: registered under a name unique to this call so two users'
    # uploaded fonts (built around the same time) can never clobber each
    # other in reportlab's global font registry. If a genuine bold weight
    # file is available (bundled presets only), register it separately so
    # bold text — the question itself — actually renders heavier than the
    # options, not just as the same glyphs relabeled "bold". Falls back to
    # reusing the regular file for bold otherwise (e.g. a plain user
    # upload, which never comes with a bold companion).
    font_name, font_name_bold = FONT_NAME, FONT_NAME_BOLD
    if font_path and os.path.exists(font_path):
        try:
            custom_name = f"CustomFont_{abs(hash(font_path)) % 10**8}"
            pdfmetrics.registerFont(TTFont(custom_name, font_path))
            font_name = font_name_bold = custom_name
            if font_bold_path and os.path.exists(font_bold_path):
                custom_bold_name = f"CustomFontBold_{abs(hash(font_bold_path)) % 10**8}"
                pdfmetrics.registerFont(TTFont(custom_bold_name, font_bold_path))
                font_name_bold = custom_bold_name
        except Exception as e:
            print(f"Custom PDF font load error: {e} — using default")

    def draw_header(canvas, doc):
        canvas.saveState()
        if bg_image_path and os.path.exists(bg_image_path):
            try:
                canvas.drawImage(
                    bg_image_path, 0, 0, width=A4[0], height=A4[1],
                    preserveAspectRatio=False, mask="auto",
                )
            except Exception as e:
                print(f"PDF background image draw error: {e}")
        canvas.setStrokeColor(colors.HexColor("#CFD8DC"))
        canvas.setLineWidth(0.5)
        canvas.line(2 * cm, A4[1] - 1.65 * cm, A4[0] - 2 * cm, A4[1] - 1.65 * cm)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2.5*cm, bottomMargin=2*cm,
    )

    Q_STYLE = ParagraphStyle(
        "QStyle", fontName=font_name_bold, fontSize=12, leading=16,
        textColor=colors.HexColor("#1A1A2E"), spaceAfter=6, spaceBefore=14,
    )
    OPT_STYLE = ParagraphStyle(
        "OptStyle", fontName=font_name, fontSize=11, leading=15,
        textColor=colors.HexColor("#1A1A2E"), leftIndent=14, spaceAfter=3,
    )
    OPT_CORRECT = ParagraphStyle(
        "OptCorrect", fontName=font_name_bold, fontSize=11, leading=15,
        textColor=colors.HexColor("#1B5E20"), leftIndent=14, spaceAfter=3,
    )
    WRITTEN_TITLE = ParagraphStyle(
        "WTitle", fontName=font_name_bold, fontSize=12, leading=16,
        textColor=colors.HexColor("#1A1A2E"), spaceAfter=4, spaceBefore=14,
    )
    WRITTEN_BODY = ParagraphStyle(
        "WBody", fontName=font_name, fontSize=11, leading=15,
        textColor=colors.HexColor("#37474F"), leftIndent=14, spaceAfter=6,
    )
    NUM_STYLE = ParagraphStyle(
        "NumStyle", fontName=font_name_bold, fontSize=9,
        textColor=colors.HexColor("#90A4AE"), spaceAfter=2,
    )
    IMG_CAPTION = ParagraphStyle(
        "ImgCaption", fontName=font_name, fontSize=9, leading=12,
        textColor=colors.HexColor("#78909C"), spaceAfter=6, spaceBefore=4,
    )

    HR_COLOR = colors.HexColor("#CFD8DC")
    story    = []


    for idx, item in enumerate(items, 1):
        q_num_label = f"~Q{idx}" if item.get("type") == "mcq" and item.get("correct") is None else f"Q{idx}"
        story.append(Paragraph(q_num_label, NUM_STYLE))

        if item["type"] == "mcq":
            story.append(Paragraph(item["q"], Q_STYLE))
            if item.get("image"):
                try:
                    img = RLImage(item["image"])
                    if img.imageWidth > PDF_MAX_IMG_WIDTH:
                        scale          = PDF_MAX_IMG_WIDTH / img.imageWidth
                        img.drawWidth  = PDF_MAX_IMG_WIDTH
                        img.drawHeight = img.imageHeight * scale
                    story.append(Spacer(1, 6))
                    story.append(img)
                    story.append(Spacer(1, 6))
                except Exception as e:
                    story.append(Paragraph(f"[Image error: {e}]", WRITTEN_BODY))
            for i, opt in enumerate(item["options"]):
                if i == item["correct"]:
                    story.append(Paragraph(f"✓  {opt}", OPT_CORRECT))
                else:
                    story.append(Paragraph(f"     {opt}", OPT_STYLE))

        elif item["type"] == "written":
            story.append(Paragraph(item["title"], WRITTEN_TITLE))
            for line in item["content"].split("\n"):
                line = line.strip()
                if line:
                    story.append(Paragraph(f"• {line}", WRITTEN_BODY))

        elif item["type"] == "image":
            img_path = item["path"]
            try:
                img = RLImage(img_path)
                if img.imageWidth > PDF_MAX_IMG_WIDTH:
                    scale          = PDF_MAX_IMG_WIDTH / img.imageWidth
                    img.drawWidth  = PDF_MAX_IMG_WIDTH
                    img.drawHeight = img.imageHeight * scale
                story.append(Spacer(1, 8))
                story.append(img)
                if item.get("caption"):
                    story.append(Paragraph(f"📷 {item['caption']}", IMG_CAPTION))
                story.append(Spacer(1, 4))
            except Exception as e:
                story.append(Paragraph(f"[Image error: {e}]", WRITTEN_BODY))

        if idx < len(items):
            story.append(Spacer(1, 6))
            story.append(HRFlowable(width="100%", thickness=0.5, color=HR_COLOR, spaceAfter=4))

    doc.build(story, onFirstPage=draw_header, onLaterPages=draw_header)
    buffer.seek(0)
    return buffer

# ═══════════════════════════════════════════════════════════════
# DOCX BUILDER  — pure Python, no Node.js
# ═══════════════════════════════════════════════════════════════
def _hex_to_rgb(hex_color: str):
    """Convert 'RRGGBB' string to RGBColor."""
    h = hex_color.lstrip("#")
    return RGBColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def _add_paragraph(doc, text: str, bold=False, size_pt=11,
                   color_hex="1A1A2E", indent_cm=0,
                   space_before=0, space_after=6,
                   align=WD_ALIGN_PARAGRAPH.LEFT, font_name: str = None) -> None:
    p   = doc.add_paragraph()
    p.alignment = align
    pf  = p.paragraph_format
    pf.space_before = Pt(space_before)
    pf.space_after  = Pt(space_after)
    if indent_cm:
        pf.left_indent = Cm(indent_cm)
    run = p.add_run(text)
    run.bold        = bold
    run.font.size   = Pt(size_pt)
    run.font.color.rgb = _hex_to_rgb(color_hex)
    if font_name:
        # Explicit override — used when this run's weight (bold, e.g. the
        # question text) needs a genuinely different font file than the
        # rest of the document's base 'Normal' style, not just a fake-bold
        # of the same face. Same w:cs handling as _set_style_font, since
        # Arabic renders off the complex-script slot specifically.
        run.font.name = font_name
        rpr = run._element.get_or_add_rPr()
        rFonts = rpr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = OxmlElement("w:rFonts")
            rpr.append(rFonts)
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rFonts.set(qn(attr), font_name)
    return p

def _add_horizontal_rule(doc):
    """Add a thin bottom border to simulate a horizontal rule."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after  = Pt(4)
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"),   "single")
    bottom.set(qn("w:sz"),    "4")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "CFD8DC")
    pBdr.append(bottom)
    pPr.append(pBdr)

def _set_header_border(para):
    """Add bottom border to the header paragraph."""
    pPr   = para._p.get_or_add_pPr()
    pBdr  = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"),   "single")
    bottom.set(qn("w:sz"),    "4")
    bottom.set(qn("w:space"), "4")
    bottom.set(qn("w:color"), "CFD8DC")
    pBdr.append(bottom)
    pPr.append(pBdr)

def _set_style_font(style, font_name: str) -> None:
    """Sets a style's font across every script slot Word actually checks —
    python-docx's high-level Font.name only touches ascii/hAnsi, but Arabic
    (and this bot is Arabic-heavy) renders off the w:cs slot specifically,
    so that has to be set explicitly or the custom font silently never
    applies to any Arabic text at all."""
    style.font.name = font_name
    rpr = style.element.get_or_add_rPr()
    rFonts = rpr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = OxmlElement("w:rFonts")
        rpr.append(rFonts)
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        rFonts.set(qn(attr), font_name)

def _add_docx_page_background(doc, image_path: str) -> None:
    """Inserts image_path into every section's header as a full-page image
    anchored behind the text (not a plain inline header image, and not
    Word's native w:background element — that one's web-layout-only and
    typically doesn't survive printing or PDF export). Lets python-docx's
    own add_picture() handle the fiddly, error-prone part (embedding the
    image + registering its relationship) and only rewrites the outer
    wp:inline wrapper into a wp:anchor positioned to cover the page."""
    for section in doc.sections:
        section.header.is_linked_to_previous = False
        header = section.header
        p   = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
        run = p.add_run()
        run.add_picture(image_path, width=section.page_width, height=section.page_height)

        drawing = run._element.find(qn("w:drawing"))
        inline  = drawing.find(qn("wp:inline"))
        extent  = inline.find(qn("wp:extent"))
        docpr   = inline.find(qn("wp:docPr"))
        graphic = inline.find(qn("a:graphic"))

        anchor = OxmlElement("wp:anchor")
        for attr, val in (
            ("distT", "0"), ("distB", "0"), ("distL", "0"), ("distR", "0"),
            ("simplePos", "0"), ("relativeHeight", "0"), ("behindDoc", "1"),
            ("locked", "0"), ("layoutInCell", "1"), ("allowOverlap", "1"),
        ):
            anchor.set(attr, val)

        simple_pos = OxmlElement("wp:simplePos")
        simple_pos.set("x", "0")
        simple_pos.set("y", "0")

        pos_h = OxmlElement("wp:positionH")
        pos_h.set("relativeFrom", "page")
        pos_h_off = OxmlElement("wp:posOffset")
        pos_h_off.text = "0"
        pos_h.append(pos_h_off)

        pos_v = OxmlElement("wp:positionV")
        pos_v.set("relativeFrom", "page")
        pos_v_off = OxmlElement("wp:posOffset")
        pos_v_off.text = "0"
        pos_v.append(pos_v_off)

        effect_extent = OxmlElement("wp:effectExtent")
        for attr in ("l", "t", "r", "b"):
            effect_extent.set(attr, "0")

        wrap_none = OxmlElement("wp:wrapNone")
        cnv_graphic_frame_pr = OxmlElement("wp:cNvGraphicFramePr")

        for el in (simple_pos, pos_h, pos_v, extent, effect_extent, wrap_none, docpr, cnv_graphic_frame_pr, graphic):
            anchor.append(el)

        drawing.remove(inline)
        drawing.append(anchor)

def build_docx(items: list, doc_title: str = "questions", font_path: str = None,
               font_bold_path: str = None, bg_image_path: str = None) -> BytesIO:
    doc = DocxDocument()

    # Custom font: unlike the PDF export, .docx can't embed the actual font
    # file — Word only renders this correctly if the reader's own machine
    # happens to already have a font by this exact name installed. Best
    # effort: apply the regular weight as the doc's base style so everything
    # inherits it, and — if a genuine bold weight file is available (bundled
    # presets only) — pass its name through to the specific bold call sites
    # below so the question text renders as an actually different, heavier
    # face rather than just a fake-bold of the regular one.
    font_bold_display_name = None
    if font_path and os.path.exists(font_path):
        try:
            font_display_name = TTFont("Probe", font_path).face.name or os.path.splitext(os.path.basename(font_path))[0]
            _set_style_font(doc.styles["Normal"], font_display_name)
            if font_bold_path and os.path.exists(font_bold_path):
                font_bold_display_name = TTFont("Probe", font_bold_path).face.name \
                    or os.path.splitext(os.path.basename(font_bold_path))[0]
        except Exception as e:
            print(f"Custom DOCX font apply error: {e}")

    # ── Page margins ──────────────────────────────────────────
    for section in doc.sections:
        section.top_margin    = Cm(2.0)
        section.bottom_margin = Cm(2.0)
        section.left_margin   = Cm(2.0)
        section.right_margin  = Cm(2.0)

    if bg_image_path and os.path.exists(bg_image_path):
        try:
            _add_docx_page_background(doc, bg_image_path)
        except Exception as e:
            print(f"DOCX background image error: {e}")

    # ── Header ────────────────────────────────────────────────
    header_para = doc.add_paragraph()
    header_para.paragraph_format.space_after = Pt(8)
    _set_header_border(header_para)

    # ── Items ─────────────────────────────────────────────────
    for idx, item in enumerate(items, 1):

        # Q-number label
        q_num_label = f"~Q{idx}" if item.get("type") == "mcq" and item.get("correct") is None else f"Q{idx}"
        _add_paragraph(doc, q_num_label, bold=True, size_pt=8,
                       color_hex="90A4AE", space_before=10, space_after=2,
                       font_name=font_bold_display_name)

        if item["type"] == "mcq":
            _add_paragraph(doc, item["q"], bold=True, size_pt=12,
                           color_hex="1A1A2E", space_before=0, space_after=4,
                           font_name=font_bold_display_name)
            if item.get("image") and os.path.exists(item["image"]):
                try:
                    p = doc.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p.paragraph_format.space_before = Pt(4)
                    p.paragraph_format.space_after  = Pt(6)
                    run = p.add_run()
                    run.add_picture(item["image"], width=Inches(5.5))
                except Exception as e:
                    _add_paragraph(doc, f"[Image error: {e}]",
                                   size_pt=10, color_hex="B71C1C")
            for i, opt in enumerate(item["options"]):
                correct = (i == item["correct"])
                _add_paragraph(
                    doc,
                    ("✓  " if correct else "     ") + opt,
                    bold=correct, size_pt=11,
                    color_hex="1B5E20" if correct else "1A1A2E",
                    indent_cm=0.7, space_after=3,
                    font_name=(font_bold_display_name if correct else None),
                )

        elif item["type"] == "written":
            _add_paragraph(doc, item["title"], bold=True, size_pt=12,
                           color_hex="1A1A2E", space_before=0, space_after=4,
                           font_name=font_bold_display_name)
            for line in item["content"].split("\n"):
                line = line.strip()
                if line:
                    _add_paragraph(doc, f"• {line}", bold=False, size_pt=11,
                                   color_hex="37474F", indent_cm=0.7, space_after=3)

        elif item["type"] == "image":
            img_path = item.get("path", "")
            if img_path and os.path.exists(img_path):
                try:
                    p = doc.add_paragraph()
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p.paragraph_format.space_before = Pt(6)
                    p.paragraph_format.space_after  = Pt(4)
                    run = p.add_run()
                    run.add_picture(img_path, width=Inches(5.5))
                    if item.get("caption"):
                        cap = _add_paragraph(
                            doc, f"📷 {item['caption']}",
                            bold=False, size_pt=9, color_hex="78909C",
                            space_after=4,
                        )
                        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                except Exception as e:
                    _add_paragraph(doc, f"[Image error: {e}]",
                                   size_pt=10, color_hex="B71C1C")
            else:
                _add_paragraph(doc, "[Image file not found]",
                               size_pt=10, color_hex="B71C1C")

        # Divider between items
        if idx < len(items):
            _add_horizontal_rule(doc)

    # ── Save to BytesIO ───────────────────────────────────────
    buffer = BytesIO()
    doc.save(buffer)
    buffer.seek(0)
    return buffer

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
async def poll_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll = update.poll

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

    if poll is None or not poll.correct_option_ids:
        return

    watch = POLL_WATCH.pop(poll.id, None)
    if not watch:
        return
    user_id, item_index = watch

    items = PDF_BUFFER.get(user_id)
    if not items or item_index >= len(items) or items[item_index]["correct"] is not None:
        return  # buffer changed, or already resolved manually — skip

    item = items[item_index]
    correct_id = poll.correct_option_ids[0]
    item["correct"] = correct_id

    queue = CLARIFY_QUEUE.get(user_id, [])
    if item_index in queue:
        queue.remove(item_index)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"✅ الكويز الأصلي لسؤال Q{item_index + 1} اتقفل وتليجرام بعت الإجابة الصح تلقائي: "
                f"{item['options'][correct_id]}"
            ),
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
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=probe.message_id)
            except Exception:
                pass
            if not (question and options and correct_id is not None):
                continue  # still couldn't recover real quiz content — skip it

        timer_seconds = get_question_timer_seconds(user_id)
        try:
            msg = await context.bot.send_poll(
                chat_id=user_id, question=question, options=options,
                type="quiz", correct_option_id=correct_id, is_anonymous=False,
                explanation=(explanation or None),
                open_period=(timer_seconds or None),
            )
        except Exception as e:
            print(f"Couldn't send lecture question {mid}: {e}")
            continue

        session["current_poll_id"]    = msg.poll.id
        session["current_correct_id"] = correct_id
        session["current_message_id"] = msg.message_id
        session["current_mid"]        = mid
        return True

    session["current_poll_id"]    = None
    session["current_correct_id"] = None
    session["current_message_id"] = None
    session["current_mid"]        = None
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
            session["current_correct_id"], session["current_message_id"], session["current_mid"],
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
        chosen     = answer.option_ids[0] if answer.option_ids else None
        is_correct = chosen is not None and chosen == daily_session.get("current_correct_id")
        await _advance_daily_quiz_session(
            context, user_id, daily_session, is_correct, daily_session.get("current_message_id"),
        )
        return

    retake_session = MISTAKES_RETAKE_SESSIONS.get(user_id)
    if retake_session and retake_session.get("current_poll_id") == poll_id:
        chosen     = answer.option_ids[0] if answer.option_ids else None
        is_correct = chosen is not None and chosen == retake_session.get("current_correct_id")
        await _advance_mistakes_retake_session(
            context, user_id, retake_session, is_correct, retake_session.get("current_message_id"),
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
        correct_id, message_id, mid = pending.pop(poll_id)
        chosen     = answer.option_ids[0] if answer.option_ids else None
        is_correct = chosen is not None and chosen == correct_id
        await _advance_lecture_session(context, user_id, session, is_correct, message_id, mid)
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

    chosen     = answer.option_ids[0] if answer.option_ids else None
    is_correct = chosen is not None and chosen == session.get("current_correct_id")
    await _advance_lecture_session(
        context, user_id, session, is_correct,
        session.get("current_message_id"), session.get("current_mid"),
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
            chat_id=user_id, question=status["question"], options=status["options"],
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
    XP/streak/achievement bookkeeping that only applies to a real answer."""
    total     = session["total"]
    correct   = session["correct"]
    incorrect = session["answered"] - correct
    pct       = round(correct / session["answered"] * 100) if session["answered"] else 0
    year = session["year"]
    lecture_name = QUIZ_INDEX[year].get(session["lecture_key"], {}).get("name", session["lecture_key"])
    is_retake = session.get("is_retake", False)
    if not is_retake:
        # Retakes are practice, not a new attempt at the lecture proper —
        # they never touch the leaderboard or best-score file.
        await _record_lecture_result(user_id, _lr_key(year, session["lecture_key"]), correct, session["answered"])
        await backup_lecture_results_to_channel(context)
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

async def _advance_lecture_session(context: ContextTypes.DEFAULT_TYPE, user_id: int, session: dict, is_correct: bool, message_id: int | None = None, mid: int | None = None):
    """Called once handle_poll_answer confirms the user answered their
    current lecture question, and whether it was right. Awards XP —
    15 correct, 5 incorrect — silently (no per-question message) and
    immediately forwards the next question. Once the lecture runs out,
    tacks on a completion bonus and sends a single results summary with the
    right/wrong count and the XP total accumulated across the lecture."""
    session["answered"] += 1
    session["correct"] = session.get("correct", 0) + (1 if is_correct else 0)
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
            if await record_mistake(user_id, mid, year, session["module"], session["subject"]):
                await backup_mistakes_bank_to_channel(context)

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

    events["achievements"] += _check_achievements(user_entry, "lecture_questions")
    events["achievements"] += _check_achievements(user_entry, "lecture_streak")

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
async def _ask_next_clarification(context, user_id: int, chat_id: int):
    """Pop-free peek at the front of the clarify queue and ask about it with
    inline A/B/C… buttons. Skips (and drops) any stale entries whose buffer
    item no longer exists (e.g. buffer was cleared mid-queue)."""
    queue = CLARIFY_QUEUE.get(user_id)
    while queue:
        item_index = queue[0]
        items = PDF_BUFFER.get(user_id)
        if not items or item_index >= len(items) or items[item_index]["correct"] is not None:
            queue.pop(0)  # stale or already resolved — skip it
            continue

        item        = items[item_index]
        q_num       = item_index + 1
        first_words = " ".join(item["q"].split()[:5])
        options_txt = "\n".join(item["options"])  # already "A) ..." labeled

        buttons = [
            InlineKeyboardButton(string.ascii_uppercase[i], callback_data=f"clarify:{item_index}:{i}")
            for i in range(len(item["options"]))
        ]
        rows = [buttons[i:i + 6] for i in range(0, len(buttons), 6)]

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"❓ <b>Choose the correct answer</b>\n"
                f"for Q{q_num}: {first_words}…\n\n{options_txt}"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    # queue exhausted — nothing left to ask
    CLARIFY_QUEUE.pop(user_id, None)

# ═══════════════════════════════════════════════════════════════
# QUESTION REVIEW / EDIT — after a question lands in the PDF buffer
# (correct answer already known at this point, whether that came in
# automatically or via the clarify buttons above), show it back to the
# admin with buttons to tweak the question text, any option's text, or
# even flip which option is correct — before moving on.
# ═══════════════════════════════════════════════════════════════
def _review_text(item: dict) -> str:
    if item["type"] == "written":
        return f"📝 <b>{html.escape(item['title'])}</b>\n{html.escape(item['content'])}"
    lines = [f"❓ {html.escape(item['q'])}"]
    for i, opt in enumerate(item["options"]):
        mark = "  ✅" if i == item.get("correct") else ""
        lines.append(html.escape(opt) + mark)
    return "\n".join(lines)

def _review_buttons(item_index: int, item: dict) -> InlineKeyboardMarkup:
    if item["type"] == "written":
        rows = [
            [InlineKeyboardButton("✏️ عدّل العنوان", callback_data=f"revedit:{item_index}:title")],
            [InlineKeyboardButton("✏️ عدّل المحتوى", callback_data=f"revedit:{item_index}:content")],
            [InlineKeyboardButton("✅ تمام، مفيش تعديل", callback_data=f"revedit:{item_index}:done")],
        ]
        return InlineKeyboardMarkup(rows)

    opt_buttons = [
        InlineKeyboardButton(f"✏️ {string.ascii_uppercase[i]}", callback_data=f"revedit:{item_index}:opt:{i}")
        for i in range(len(item["options"]))
    ]
    rows = [opt_buttons[i:i + 6] for i in range(0, len(opt_buttons), 6)]
    rows.append([InlineKeyboardButton("✏️ عدّل نص السؤال", callback_data=f"revedit:{item_index}:q")])
    if item.get("correct") is not None:
        rows.append([InlineKeyboardButton("🔁 غيّر الإجابة الصح", callback_data=f"revedit:{item_index}:correct")])
    rows.append([InlineKeyboardButton("✅ تمام، مفيش تعديل", callback_data=f"revedit:{item_index}:done")])
    return InlineKeyboardMarkup(rows)

def _edit_button_markup(item_index: int) -> None:
    """No longer attached to every single per-question confirmation — that
    was the exact 'a prompt for every question' clutter this replaced.
    Editing now goes through one global entry point instead: the '✏️ Edit
    a Question' button on the PDF Collection Mode progress message, which
    opens a numbered picker (see edit_pick_keyboard) and reuses the same
    revedit:{index}:open flow this used to jump into directly."""
    return None

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
    # forward is paired with it, in either mode.
    pending_img = PENDING_IMAGE.pop(user_id, None)

    # ── PDF mode: save poll (+ any paired image) to buffer ──────
    if user_id in PDF_BUFFER:
        labeled_options = [
            f"{string.ascii_uppercase[i]}) {opt}" for i, opt in enumerate(raw_options)
        ]
        item = {
            "type": "mcq", "q": question,
            "options": labeled_options, "correct": correct_index,  # None = unknown
            "poll_id": poll.id,
        }
        if pending_img:
            item["image"] = pending_img

        PDF_BUFFER[user_id].append(item)
        item_index = len(PDF_BUFFER[user_id]) - 1

        label = ("🖼 " if pending_img else "") + ("~" if correct_index is None else "") \
                + question[:50] + ("…" if len(question) > 50 else "")
        await update_progress(context, user_id, update.effective_chat.id, latest_label=label)

        if correct_index is None:
            # Telegram hid the answer (quiz still open, not ours) — queue it
            # for a quick button tap instead of silently guessing. The
            # review/edit prompt fires once that tap resolves it (see the
            # "clarify:" branch in button_handler).
            POLL_WATCH[poll.id] = (user_id, item_index)
            queue = CLARIFY_QUEUE.setdefault(user_id, [])
            queue.append(item_index)
            if len(queue) == 1:  # nothing else currently being asked
                await _ask_next_clarification(context, user_id, update.effective_chat.id)
        else:
            # Correct answer already known — show confirmation with edit button.
            short = question[:50] + ("…" if len(question) > 50 else "")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"✅ اتسجل: {html.escape(short)}",
                parse_mode=ParseMode.HTML,
                reply_markup=_edit_button_markup(item_index),
            )
        return

    # ── Normal mode: show the poll question + choices with an edit button ──
    lines = [f"❓ <b>{html.escape(question)}</b>"]
    for opt in raw_options:
        lines.append(html.escape(opt))
    full_text = "\n".join(lines)

    # Build a temporary "normal mode" item so the edit flow works the same way
    normal_item = {
        "type": "mcq", "q": question,
        "options": [f"{string.ascii_uppercase[i]}) {o}" if not o.startswith(tuple(string.ascii_uppercase)) else o
                    for i, o in enumerate(raw_options)],
        "correct": None,
    }
    nm_buf = PDF_BUFFER.setdefault(user_id, [])
    nm_buf.append(normal_item)
    nm_index = len(nm_buf) - 1

    if pending_img:
        with open(pending_img, "rb") as f:
            cap = full_text if len(full_text) <= 1024 else None
            sent = await context.bot.send_photo(
                chat_id=user_id, photo=f, caption=cap,
                parse_mode=ParseMode.HTML if cap else None,
                reply_markup=_edit_button_markup(nm_index) if cap else None,
            )
            if not cap:
                await context.bot.send_message(
                    chat_id=user_id, text=full_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=_edit_button_markup(nm_index),
                )
    else:
        await context.bot.send_message(
            chat_id=user_id, text=full_text,
            parse_mode=ParseMode.HTML,
            reply_markup=_edit_button_markup(nm_index),
        )

# ═══════════════════════════════════════════════════════════════
# IMAGE HANDLER (PDF mode only)
# ═══════════════════════════════════════════════════════════════
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id  = update.effective_chat.id
    real_uid = update.effective_user.id if update.effective_user else user_id
    if user_id in SLEEPING:
        return

    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        return

    # ── AWAITING BACKGROUND IMAGE (part of the /pdf_start setup flow) ──
    if AWAITING_BG.get(user_id):
        bg_dir  = os.path.join(IMG_BASE_DIR, str(user_id))
        os.makedirs(bg_dir, exist_ok=True)
        bg_path = os.path.join(bg_dir, "page_background.jpg")
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(bg_path)
        PDF_BG_IMAGE_PATH[user_id] = bg_path
        del AWAITING_BG[user_id]
        await _finish_pdf_setup(context, user_id, update.message)
        return

    in_pdf_mode = user_id in PDF_BUFFER
    caption     = (update.message.caption or "").strip()

    # ── Download the image (works in both PDF and normal mode now) ──
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
        if in_pdf_mode:
            labeled_options = [
                f"{string.ascii_uppercase[i]}) {opt}" for i, opt in enumerate(raw_options)
            ]
            PDF_BUFFER[user_id].append({
                "type": "mcq", "q": question,
                "options": labeled_options, "correct": correct_index,
                "image": img_path,
            })
            await update_progress(
                context, user_id, update.effective_chat.id,
                latest_label=f"🖼 {question[:50]}" + ("…" if len(question) > 50 else ""),
            )
        else:
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

    # ── Case 2 (PDF mode only): non-empty caption that ISN'T a full
    # question — keep the old behaviour of saving it as a standalone
    # image item (e.g. comparison charts / tables with a plain caption).
    if in_pdf_mode and caption:
        PDF_BUFFER[user_id].append({
            "type": "image", "path": img_path, "caption": caption,
        })
        await update_progress(
            context, user_id, update.effective_chat.id,
            latest_label=f"Image — {caption}",
        )
        return

    # ── Case 3: no caption — park the image and ask for the question
    _clear_pending_image(user_id)
    PENDING_IMAGE[user_id] = img_path
    await update.message.reply_text(
        "🖼 <b>استلمت الصورة!</b>\n"
        "دلوقتي ابعت السؤال والاختيارات (بنفس صيغة الأسئلة المعتادة) "
        "وهيتضاف الصورة تلقائي للسؤال ده.",
        parse_mode=ParseMode.HTML,
    )

async def handle_font_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Font file (.ttf/.otf) uploaded during the /pdf_start setup flow.
    Registered on a filter that only matches those two extensions, but
    still guarded by AWAITING_FONT — an unsolicited font upload outside
    the setup flow is just ignored, not treated as a command."""
    if not update.message or not update.message.document:
        return
    user_id = update.effective_chat.id
    if user_id in SLEEPING:
        return
    if not AWAITING_FONT.get(user_id):
        return

    doc      = update.message.document
    font_dir = os.path.join(FONT_BASE_DIR, str(user_id))
    os.makedirs(font_dir, exist_ok=True)
    ext       = ".otf" if (doc.file_name or "").lower().endswith(".otf") else ".ttf"
    font_path = os.path.join(font_dir, f"font{ext}")
    tg_file   = await context.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(font_path)

    PDF_FONT_PATH[user_id] = font_path
    PDF_FONT_BOLD_PATH.pop(user_id, None)   # single upload has no bold companion — clear any stale preset one
    del AWAITING_FONT[user_id]
    AWAITING_BG[user_id] = True
    await update.message.reply_text(
        "✅ الخط اتسجل!\n\n"
        "دلوقتي ابعت صورة تتحط كخلفية لكل صفحة في الـ PDF/DOCX، أو دوس Skip لو مش عايز خلفية.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("⏭ Skip", callback_data="bg_skip"),
        ]]),
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
        in_pdf_mode = user_id in PDF_BUFFER
        parsed = parse_mcq_block(caption)
        if not parsed:
            await update.message.reply_text(
                "⚠️ الكابشن مش صيغة سؤال كاملة (لازم سؤال + اختيارات + إجابة صح متعلّم عليها بـ z)."
            )
            return
        question, raw_options, correct_index, explanation = parsed
        if in_pdf_mode:
            labeled_options = [f"{string.ascii_uppercase[i]}) {opt}" for i, opt in enumerate(raw_options)]
            PDF_BUFFER[user_id].append({"type": "mcq", "q": question, "options": labeled_options, "correct": correct_index})
            await update_progress(
                context, user_id, update.effective_chat.id,
                latest_label=f"📄 {question[:50]}" + ("…" if len(question) > 50 else ""),
            )
        else:
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
async def _index_item(caption: str, message_ids: list):
    password = caption.strip().split(maxsplit=1)[0].lower()
    STORAGE_INDEX.setdefault(password, []).append(sorted(message_ids))
    await save_storage_index()
    return password

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

async def storage_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Utility: run inside the storage group to get its chat ID for STORAGE_GROUP_ID."""
    await update.message.reply_text(
        f"🆔 Chat ID: <code>{update.effective_chat.id}</code>", parse_mode=ParseMode.HTML
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
        QUIZ_INDEX[year][current]["ids"].append(msg.message_id)
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

async def quiz_channel_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Utility: forward any message from the quiz channel here first, then
    run this command in the same DM — it reads the forward's source chat ID."""
    fwd = update.message.forward_from_chat if update.message else None
    if not fwd:
        await update.message.reply_text(
            "⚠️ فورورد أي رسالة من قناة الكويزات هنا الأول، وبعدين ابعت /quiz_channel_id تاني."
        )
        return
    await update.message.reply_text(
        f"🆔 Quiz channel ID: <code>{fwd.id}</code>", parse_mode=ParseMode.HTML
    )

async def quiz_lectures_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User-facing: pick a year, then a module, then a subject, then a lecture."""
    years = configured_years()
    if not years:
        await update.message.reply_text("📭 مفيش سنين متاحة دلوقتي.")
        return
    buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"yr:{y}")] for y in years]
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
    """Admin: pick a year, then a module, to restrict BOTH the Daily
    Quiz's random-questions slice and its mistakes-bank slice to just
    that module (e.g. whatever's currently being taught) instead of the
    whole curriculum. Own callback_data namespace (dqy:/dqm:/dq_scope_off)
    — deliberately separate from the yr:/module: user-facing browsing
    flow, since this is a one-time admin scope pick, not lecture
    navigation."""
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
    """Admin: /quiz_delete <year> <n> — removes a lecture from that year's
    index (does not delete the actual channel messages; only stops it
    showing up in /quiz)."""
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
    key = keys[n - 1]
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
    await update.message.reply_text(
        f"🗑 اتشالت محاضرة من {year_label(year)}: {removed['module']} - {removed['subject']}: {removed['name']}\n"
        "(الرسايل نفسها لسه موجودة في القناة — احذفهم يدوي لو عايز)"
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
        await save_report_threads()
        try:
            await context.bot.send_message(
                chat_id=thread["user_id"],
                text=f"📩 <b>رد من الأدمن على مشكلتك:</b>\n\n{html.escape(reply_text)}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Reply", callback_data=f"report_user_reply:{group_message_id}"),
                ]]),
            )
        except Exception as e:
            print("REPORT REPLY DELIVERY FAILED:", e)
            await update.message.reply_text(
                "⚠️ الرد اتسجل بس معرفتش أبعته للمستخدم (يمكن قافل البوت). هرجع أعدل الرسالة برضو."
            )
        try:
            await context.bot.edit_message_text(
                chat_id=REPORT_ISSUE_GROUP_ID, message_id=group_message_id,
                text=_report_thread_text(thread, group_message_id), parse_mode=ParseMode.HTML,
                reply_markup=_report_reply_keyboard(group_message_id, closed=thread["closed"]),
            )
        except Exception as e:
            print("REPORT THREAD EDIT FAILED:", e)
        await backup_report_threads_to_channel(context)
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
        await save_report_threads()
        await update.message.reply_text("✅ اتبعت للأدمن.")
        try:
            await context.bot.edit_message_text(
                chat_id=REPORT_ISSUE_GROUP_ID, message_id=group_message_id,
                text=_report_thread_text(thread, group_message_id), parse_mode=ParseMode.HTML,
                reply_markup=_report_reply_keyboard(group_message_id, closed=thread["closed"]),
            )
        except Exception as e:
            print("REPORT THREAD EDIT FAILED (user followup):", e)
        await backup_report_threads_to_channel(context)
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
        entry = _get_settings_entry(real_uid)
        entry["nickname"] = nickname
        await save_settings()
        await backup_settings_to_channel(context)
        _get_entry(real_uid)["nickname"] = nickname
        await save_analytics()
        await backup_analytics_to_channel(context)
        if onboarding:
            await update.message.reply_text(
                f"✅ اتسجل! هنناديك <b>{html.escape(nickname)}</b> دلوقتي.",
                parse_mode=ParseMode.HTML,
            )
            await update.message.reply_text(
                "📚 وانت في انهي سنة/فرقة؟",
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
    # Keyed by real_uid, same as nickname above.
    if AWAITING_REPORT_ISSUE.pop(real_uid, None):
        if not REPORT_ISSUE_GROUP_ID:
            await update.message.reply_text("⚠️ الميزة دي مش متاحة دلوقتي.")
            return
        tg_user  = update.effective_user
        name     = " ".join(p for p in (tg_user.first_name, tg_user.last_name) if p).strip() if tg_user else "?"
        username = tg_user.username if tg_user else None
        thread = {
            "user_id":  real_uid,
            "name":     name or "?",
            "username": username,
            "user_text": text,
            "replies":  [],
            "closed":   False,
        }
        try:
            sent = await context.bot.send_message(
                chat_id=REPORT_ISSUE_GROUP_ID,
                text="📩 New issue report — loading…",   # placeholder; fixed up right below once we have the real id
                reply_markup=_report_reply_keyboard(0, closed=False),   # placeholder id, fixed up right below too
            )
        except Exception as e:
            print("REPORT ISSUE SEND FAILED:", e)
            await update.message.reply_text("⚠️ مشكلة في إرسال الرسالة — جرب تاني لو سمحت.")
            return
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
        REPORT_THREADS[sent.message_id] = thread
        await save_report_threads()
        await backup_report_threads_to_channel(context)
        await update.message.reply_text("✅ اتبعتت. هيتم الرد عليك من هنا لما الأدمن يشوفها.")
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
        await save_report_threads()
        try:
            await context.bot.send_message(
                chat_id=thread["user_id"],
                text=f"📩 <b>رد من الأدمن على مشكلتك:</b>\n\n{html.escape(text)}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Reply", callback_data=f"report_user_reply:{group_message_id}"),
                ]]),
            )
        except Exception as e:
            print("REPORT REPLY DELIVERY FAILED:", e)
            await update.message.reply_text(
                "⚠️ الرد اتسجل بس معرفتش أبعته للمستخدم (يمكن قافل البوت). هرجع أعدل الرسالة برضو."
            )
        try:
            await context.bot.edit_message_text(
                chat_id=REPORT_ISSUE_GROUP_ID, message_id=group_message_id,
                text=_report_thread_text(thread, group_message_id), parse_mode=ParseMode.HTML,
                reply_markup=_report_reply_keyboard(group_message_id, closed=thread["closed"]),
            )
        except Exception as e:
            print("REPORT THREAD EDIT FAILED:", e)
        await backup_report_threads_to_channel(context)
        return

    # ── AWAITING A QUESTION EDIT (from the review/edit prompt) ───
    pending_edit = PENDING_EDIT.pop(user_id, None)
    if pending_edit:
        items = PDF_BUFFER.get(user_id)
        idx   = pending_edit["index"]
        if not items or idx >= len(items):
            await update.message.reply_text("⚠️ السؤال ده مش موجود في البافر دلوقتي.")
            return
        item  = items[idx]
        field = pending_edit["field"]

        if field == "option":
            opt_idx = pending_edit["opt_index"]
            if 0 <= opt_idx < len(item["options"]):
                letter = string.ascii_uppercase[opt_idx]
                item["options"][opt_idx] = f"{letter}) {text}"
        elif field in ("q", "title", "content"):
            item[field] = text

        await update.message.reply_text(
            "👀 <b>راجع السؤال:</b>\n\n" + _review_text(item) + "\n\nفيه حاجة تانية عايز تعدلها؟",
            parse_mode=ParseMode.HTML,
            reply_markup=_review_buttons(idx, item),
        )
        return

    # ── AWAITING PDF NAME ────────────────────────────────────────
    if AWAITING_NAME.get(user_id):
        name = text.strip()
        PDF_NAMES[user_id] = name
        del AWAITING_NAME[user_id]
        AWAITING_FONT[user_id] = True
        await update.message.reply_text(
            f"📥 <b>الاسم اتسجل:</b> <i>{name}</i>\n\n"
            "اختار خط جاهز، أو ابعت ملف خط (.ttf أو .otf) بنفسك، أو دوس Skip لو عايز الخط الافتراضي.",
            parse_mode=ParseMode.HTML,
            reply_markup=font_prompt_keyboard(),
        )
        return

    # ── AWAITING FONT FILE (reminder — the real handling is in
    #    handle_font_upload/the font_skip button; this only fires if the
    #    user sends plain text instead) ─────────────────────────────
    if AWAITING_FONT.get(user_id):
        await update.message.reply_text(
            "⚠️ اختار خط من الأزرار فوق، ابعت ملف خط (.ttf أو .otf)، أو دوس Skip.",
        )
        return

    # ── AWAITING BACKGROUND IMAGE (same — reminder only) ────────────
    if AWAITING_BG.get(user_id):
        await update.message.reply_text(
            "⚠️ محتاج تبعت صورة كخلفية، أو دوس Skip فوق.",
        )
        return


    # ── STORAGE PASSWORD LOOKUP ──────────────────────────────────
    if update.effective_chat.type == "private":
        items = STORAGE_INDEX.get(text.lower())
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

    in_pdf_mode = user_id in PDF_BUFFER

    try:
        blocks = re.split(r"\n\s*\n", text)

        if not in_pdf_mode and len(blocks) > MAX_QUESTIONS_PER_MSG:
            await update.message.reply_text(
                f"❌ الحد الأقصى {MAX_QUESTIONS_PER_MSG} سؤال في المرة الواحدة"
            )
            return

        any_saved    = False
        last_label   = ""

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # ── WRITTEN ─────────────────────────────────────────
            if in_pdf_mode:
                written = parse_written_question(block)
            else:
                written = parse_written_strict(block)

            if written:
                title, content = written
                if in_pdf_mode:
                    PDF_BUFFER[user_id].append({
                        "type":    "written",
                        "title":   title,
                        "content": content,
                    })
                    any_saved  = True
                    last_label = title[:50] + ("…" if len(title) > 50 else "")
                    item_index = len(PDF_BUFFER[user_id]) - 1
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"✅ اتسجل: <b>{html.escape(last_label)}</b>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=_edit_button_markup(item_index),
                    )
                else:
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
                if not in_pdf_mode and _looks_like_mcq_attempt(lines):
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
                if not in_pdf_mode:
                    await update.message.reply_text(
                        "⚠️ <b>ما فيش إجابة صح!</b>\n\n"
                        "علّم الإجابة الصحيحة بـ <code>z</code> في نهايتها:\n"
                        "<code>b) الإجابة الصح z</code>",
                        parse_mode=ParseMode.HTML,
                    )
                continue

            # An image sent (with no caption / unparseable caption) just
            # before this message is paired with this question, in either mode.
            pending_img = PENDING_IMAGE.pop(user_id, None)

            # ── PDF MODE ────────────────────────────────────────
            if in_pdf_mode:
                labeled_options = [
                    f"{string.ascii_uppercase[i]}) {opt}" for i, opt in enumerate(raw_options)
                ]
                item = {
                    "type": "mcq", "q": question,
                    "options": labeled_options, "correct": correct_index,
                }
                if pending_img:
                    item["image"] = pending_img
                PDF_BUFFER[user_id].append(item)
                any_saved  = True
                last_label = ("🖼 " if pending_img else "") + question[:50] + ("…" if len(question) > 50 else "")
                item_index = len(PDF_BUFFER[user_id]) - 1
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"✅ اتسجل: {html.escape(last_label)}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_edit_button_markup(item_index),
                )
                continue

            # ── NORMAL QUIZ MODE ─────────────────────────────────
            await deliver_quiz(
                context, user_id, question, raw_options, correct_index,
                explanation=explanation, image_path=pending_img,
            )
            events = await _record_activity(real_uid, questions_delta=1)
            _update_telegram_name(real_uid, update.effective_user)
            await react_random(update, context)
            await _announce_events(context, user_id, events)
            await backup_analytics_to_channel(context)
        if in_pdf_mode and any_saved:
            await update_progress(context, user_id, update.effective_chat.id, last_label)

    except Exception as e:
        print("ERROR:", e)

# ═══════════════════════════════════════════════════════════════
# INLINE BUTTON HANDLER
# ═══════════════════════════════════════════════════════════════
@_serialize_per_user
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user_id = query.from_user.id
    await query.answer()

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
        await restore_fn(context.application)
        ok = RESTORE_OK.get(key, True)
        if ok:
            await context.bot.send_message(chat_id=user_id, text=f"✅ {label} — تم الـ restore من آخر نسخة مثبتة.")
        else:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ {label} — الـ restore فشل. اتبعتلك رسالة تانية بالتفاصيل (نفس رسالة فشل الـ restore وقت التشغيل).",
            )
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
            await query.answer("⚠️ الـ report ده اتقفل، مينفعش ترد عليه تاني.", show_alert=True)
            return
        if thread["user_id"] != user_id:
            # Shouldn't happen (this button only ever goes out to the
            # thread's own reporter), but don't let a forwarded/replayed
            # callback_data let someone follow up on someone else's thread.
            await query.answer("⚠️ مش قادر أعمل كده.", show_alert=True)
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

    # ── QUIZ YEARS: top-level list ──────────────────────────────────
    if query.data == "quiz_years":
        years = configured_years()
        if not years:
            await query.edit_message_text("📭 مفيش سنين متاحة دلوقتي.")
            return
        buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"yr:{y}")] for y in years]
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

    # ── CLARIFY: manual correct-answer button tap ────────────────
    if query.data.startswith("clarify:"):
        _, item_index_str, choice_str = query.data.split(":")
        item_index = int(item_index_str)
        choice     = int(choice_str)

        items = PDF_BUFFER.get(user_id)
        if not items or item_index >= len(items) or items[item_index]["correct"] is not None:
            await query.edit_message_text("⚠️ السؤال ده اتحل أو اتشال بالفعل.")
            return

        item = items[item_index]
        if not (0 <= choice < len(item["options"])):
            return

        item["correct"] = choice
        POLL_WATCH.pop(item.get("poll_id"), None)

        queue = CLARIFY_QUEUE.get(user_id, [])
        if item_index in queue:
            queue.remove(item_index)

        await query.edit_message_text(
            f"✅ Q{item_index + 1}: {html.escape(item['options'][choice])}",
            parse_mode=ParseMode.HTML,
            reply_markup=_edit_button_markup(item_index),
        )

        if queue:
            await _ask_next_clarification(context, user_id, query.message.chat_id)
        else:
            CLARIFY_QUEUE.pop(user_id, None)
        return

    # ── QUESTION REVIEW / EDIT ──────────────────────────────────
    if query.data == "edit_pick":
        items = PDF_BUFFER.get(user_id, [])
        if not items:
            await query.answer("مفيش أسئلة دلوقتي", show_alert=True)
            return
        lines = ["✏️ <b>اختار رقم السؤال اللي عايز تعدله:</b>\n"]
        for i, item in enumerate(items):
            lines.append(f"{i + 1}. {html.escape(_item_preview_label(item))}")
        await query.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            reply_markup=edit_pick_keyboard(items),
        )
        return

    if query.data == "edit_pick_back":
        items = PDF_BUFFER.get(user_id, [])
        await query.edit_message_text(
            build_progress_text(items), parse_mode=ParseMode.HTML,
            reply_markup=export_keyboard(),
        )
        return

    if query.data.startswith("revedit:"):
        parts      = query.data.split(":")
        item_index = int(parts[1])
        action     = parts[2]

        items = PDF_BUFFER.get(user_id)
        if not items or item_index >= len(items):
            await query.edit_message_text("⚠️ السؤال ده مش موجود في البافر دلوقتي.")
            return
        item = items[item_index]

        if action == "open":
            # First press — expand into the full edit menu
            await query.edit_message_text(
                "✏️ <b>إيه اللي عايز تعدله؟</b>\n\n" + _review_text(item),
                parse_mode=ParseMode.HTML,
                reply_markup=_review_buttons(item_index, item),
            )
            return

        if action == "done":
            await query.edit_message_text(
                "✅ <b>خلاص، اتسجل:</b>\n\n" + _review_text(item), parse_mode=ParseMode.HTML
            )
            return

        if action in ("q", "title", "content"):
            PENDING_EDIT[user_id] = {"index": item_index, "field": action}
            prompt = {
                "q":       "✏️ اكتب نص السؤال الجديد:",
                "title":   "✏️ اكتب العنوان الجديد:",
                "content": "✏️ اكتب المحتوى الجديد:",
            }[action]
            await query.edit_message_text(prompt)
            return

        if action == "opt":
            opt_idx = int(parts[3])
            if not (0 <= opt_idx < len(item["options"])):
                return
            PENDING_EDIT[user_id] = {"index": item_index, "field": "option", "opt_index": opt_idx}
            letter = string.ascii_uppercase[opt_idx]
            await query.edit_message_text(f"✏️ اكتب النص الجديد للاختيار {letter} (من غير الحرف):")
            return

        if action == "correct":
            buttons = [
                InlineKeyboardButton(string.ascii_uppercase[i], callback_data=f"revcorrect:{item_index}:{i}")
                for i in range(len(item["options"]))
            ]
            rows = [buttons[i:i + 6] for i in range(0, len(buttons), 6)]
            await query.edit_message_text("🔁 اختار الإجابة الصح:", reply_markup=InlineKeyboardMarkup(rows))
            return
        return

    if query.data.startswith("revcorrect:"):
        _, item_index_str, choice_str = query.data.split(":")
        item_index = int(item_index_str)
        choice     = int(choice_str)

        items = PDF_BUFFER.get(user_id)
        if not items or item_index >= len(items):
            await query.edit_message_text("⚠️ السؤال ده مش موجود في البافر دلوقتي.")
            return
        item = items[item_index]
        if not (0 <= choice < len(item["options"])):
            return

        item["correct"] = choice
        await query.edit_message_text(
            "👀 <b>راجع السؤال:</b>\n\n" + _review_text(item) + "\n\nفيه حاجة تانية عايز تعدلها؟",
            parse_mode=ParseMode.HTML,
            reply_markup=_review_buttons(item_index, item),
        )
        return

    # ── PDF SETUP FLOW: font/background skip buttons ────────────────
    if query.data.startswith("font_preset:"):
        if not AWAITING_FONT.get(user_id):
            return
        idx   = int(query.data.split(":")[1])
        names = list(BUNDLED_FONTS.keys())
        if idx >= len(names):
            return
        name      = names[idx]
        font_path = BUNDLED_FONTS[name]["regular"]
        if not font_path or not os.path.exists(font_path):
            await query.answer(f"⚠️ ملف {name} مش موجود على السيرفر دلوقتي.", show_alert=True)
            return
        bold_path = BUNDLED_FONTS[name]["bold"]
        PDF_FONT_PATH[user_id] = font_path
        # Bold companion is optional — if it's missing, bold text just
        # reuses the regular weight (same as a plain user upload does).
        if bold_path and os.path.exists(bold_path):
            PDF_FONT_BOLD_PATH[user_id] = bold_path
        else:
            PDF_FONT_BOLD_PATH.pop(user_id, None)
        del AWAITING_FONT[user_id]
        AWAITING_BG[user_id] = True
        await query.edit_message_text(
            f"✅ خط <b>{name}</b> اتحدد!\n\n"
            "دلوقتي ابعت صورة تتحط كخلفية لكل صفحة في الـ PDF/DOCX، أو دوس Skip لو مش عايز خلفية.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip", callback_data="bg_skip"),
            ]]),
        )
        return

    if query.data == "font_skip":
        if not AWAITING_FONT.get(user_id):
            return
        PDF_FONT_PATH.pop(user_id, None)
        PDF_FONT_BOLD_PATH.pop(user_id, None)
        del AWAITING_FONT[user_id]
        AWAITING_BG[user_id] = True
        await query.edit_message_text(
            "⏭ اتخطيت اختيار الخط.\n\n"
            "دلوقتي ابعت صورة تتحط كخلفية لكل صفحة في الـ PDF/DOCX، أو دوس Skip لو مش عايز خلفية.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip", callback_data="bg_skip"),
            ]]),
        )
        return

    if query.data == "bg_skip":
        if not AWAITING_BG.get(user_id):
            return
        del AWAITING_BG[user_id]
        await _finish_pdf_setup(context, user_id, query.message, edit=True)
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
        year_class = get_year_class(user_id)
        if not year_class:
            await query.edit_message_text(
                "📚 محتاج تحدد سنتك/فرقتك الأول عشان تشوف الـ Leaderboard بتاعها.\n"
                "اختار من هنا:",
                reply_markup=year_class_keyboard("set_yc"),
            )
            return
        rows = _year_leaderboard(year_class)
        title = f"🏆 <b>Leaderboard — {year_class_label(year_class)}</b>"
        if not rows:
            text = f"{title}\n\nمفيش حد جاوب أسئلة محاضرات في السنة دي لسه."
        else:
            medal = {0: "🥇", 1: "🥈", 2: "🥉"}
            lines = [title, ""]
            for i, r in enumerate(rows):
                rank = medal.get(i, f"{i + 1}.")
                lines.append(f"{rank} {html.escape(r['name'])} (Lv.{r['level']}) — {r['correct']} ✅")
            text = "\n".join(lines)
        await query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Back to Home", callback_data="back_home"),
            ]]),
        )
        return

    if query.data == "menu_settings":
        AWAITING_NICKNAME.pop(user_id, None)
        await _send_settings(context, user_id, query.message, edit=True)
        return

    if query.data == "edit_nickname":
        AWAITING_NICKNAME[user_id] = True
        await query.edit_message_text(
            "✏️ ابعت الاسم المستعار اللي عايزه (حتى 32 حرف).",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 رجوع", callback_data="menu_settings"),
            ]]),
        )
        return

    if query.data == "edit_year_class":
        await query.edit_message_text(
            "📚 وانت في انهي سنة/فرقة؟",
            reply_markup=year_class_keyboard("set_yc"),
        )
        return

    # ── set_yc: / onboard_yc: — year/class picker tap, from Settings or ──
    # from the onboarding flow (right after the first-ever nickname save).
    if query.data.startswith("set_yc:") or query.data.startswith("onboard_yc:"):
        prefix, year_class = query.data.split(":")
        is_onboarding = (prefix == "onboard_yc")
        if year_class not in YEAR_CLASS_NUMBER:
            await query.edit_message_text("⚠️ الاختيار ده مش متاح.")
            return
        entry = _get_settings_entry(user_id)
        entry["year_class"] = year_class
        await save_settings()
        await backup_settings_to_channel(context)
        if is_onboarding:
            nickname = get_nickname(user_id)
            greeting = f"يا {html.escape(nickname)}! " if nickname else ""
            await query.edit_message_text(
                f"✅ تمام، {year_class_label(year_class)}.",
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=(
                    f"{quizzy_block(QUIZZY_WELCOME_ART, random.choice(QUIZZY_WELCOME_LINES))}\n\n"
                    f"{greeting}تحب تعمل أي؟!:"
                ),
                parse_mode=ParseMode.HTML,
                reply_markup=start_menu_keyboard(),
            )
        else:
            await _send_settings(context, user_id, query.message, edit=True)
        return

    if query.data in ("toggle_reactions", "toggle_auto_next", "toggle_randomize", "toggle_achievement_notifs", "toggle_spaced_repetition"):
        key = {
            "toggle_reactions": "reactions",
            "toggle_auto_next": "auto_next",
            "toggle_randomize": "randomize",
            "toggle_achievement_notifs": "achievement_notifs",
            "toggle_spaced_repetition": "spaced_repetition",
        }[query.data]
        entry = _get_settings_entry(user_id)
        entry[key] = not entry.get(key, True)
        await save_settings()
        await backup_settings_to_channel(context)
        await _send_settings(context, user_id, query.message, edit=True)
        return

    if query.data == "toggle_question_timer":
        # 3-way cycle: Off -> 60s -> 30s -> Off
        entry = _get_settings_entry(user_id)
        current = entry.get("question_timer", 0)
        entry["question_timer"] = {0: 60, 60: 30, 30: 0}.get(current, 0)
        await save_settings()
        await backup_settings_to_channel(context)
        await _send_settings(context, user_id, query.message, edit=True)
        return

    if query.data == "toggle_language":
        # 2-way cycle: ar -> en -> ar. Only flips the stored preference —
        # see the I18N section for how (and how little, so far) anything
        # actually reads it yet.
        entry = _get_settings_entry(user_id)
        current = entry.get("language", "ar")
        entry["language"] = "en" if current == "ar" else "ar"
        await save_settings()
        await backup_settings_to_channel(context)
        await _send_settings(context, user_id, query.message, edit=True)
        return

    # ── Settings: Clear Mistake Bank (with confirmation) ──
    # MISTAKES_BANK holds every user's entries in one file, but each entry
    # is tagged with its owner (see MISTAKES BANK schema note), so this
    # action only ever touches the tapping user's own entries — and
    # asks for an explicit tap-to-confirm before wiping them, since it's
    # destructive.
    if query.data == "clear_mistakes_bank_ask":
        count = len(_scoped_mistakes_bank(user_id))
        if not count:
            await query.answer("🎉 بنك الأخطاء بتاعك فاضي أصلاً!", show_alert=True)
            return
        await query.edit_message_text(
            f"⚠️ <b>متأكد إنك عايز تمسح بنك الأخطاء بتاعك؟</b>\n\n"
            f"هيتمسح <b>{count}</b> سؤال، والعملية دي مش هترجع تاني.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 أيوه، امسح", callback_data="clear_mistakes_bank_yes")],
                [InlineKeyboardButton("🔙 لأ، رجّعني", callback_data="menu_settings")],
            ]),
        )
        return

    if query.data == "clear_mistakes_bank_yes":
        # Remove only this user's entries (identity, not just count, so the
        # removal is exact even if the bank changed between the confirm
        # screen and this tap).
        before  = len(MISTAKES_BANK)
        MISTAKES_BANK[:] = [m for m in MISTAKES_BANK if not (_is_valid_mistake_entry(m) and m["user_id"] == user_id)]
        cleared = before - len(MISTAKES_BANK)
        _MISTAKES_BY_USER.pop(user_id, None)   # O(1) — we know exactly whose bucket emptied
        await save_mistakes_bank()
        await backup_mistakes_bank_to_channel(context)
        await query.answer(f"✅ اتمسح {cleared} سؤال من بنك الأخطاء بتاعك.", show_alert=True)
        await _send_settings(context, user_id, query.message, edit=True)
        return

    if query.data == "menu_quizzes":
        # Same as typing /quiz — sends a fresh message (not an edit) so the
        # welcome message with its buttons stays intact above it.
        years = configured_years()
        if not years:
            await query.message.reply_text("📭 مفيش سنين متاحة دلوقتي.")
            return
        buttons = [[InlineKeyboardButton(year_label(y), callback_data=f"yr:{y}")] for y in years]
        await query.message.reply_text(
            "📚 <b>اختار السنة:</b>", parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if query.data == "daily_quiz":
        await start_daily_quiz(context, user_id, message=query.message)
        return

    # ── 🧠 Mistakes Bank menu button ──────────────────────────────
    if query.data == "mistakes_bank_menu":
        scope = get_daily_quiz_scope()
        count = len(_scoped_mistakes_bank(user_id))
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
            f"(الأسئلة العشوائية وأسئلة الأخطاء القديمة هيتسحبوا من الموديول ده بس)",
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

    # ── EXPORT BUTTONS ──────────────────────────────────────────
    if query.data in ("gen_pdf", "gen_docx", "clear_pdf") and not _pdf_access_allowed(update):
        await query.message.reply_text(MSG_PDF_ACCESS_DENIED)
        return

    items = PDF_BUFFER.get(user_id, [])
    name  = PDF_NAMES.get(user_id, "questions")
    safe  = re.sub(r"[^\w\s\-]", "", name).strip().replace(" ", "_") or "questions"

    if query.data == "gen_pdf":
        if not items:
            await query.message.reply_text(MSG_EXPORT_EMPTY)
            return
        await query.message.reply_text(MSG_EXPORT_GENERATING.format(kind="PDF", count=len(items)))
        await _export_pdf_session(context, query.message, user_id, user_id, items, name, fmt="pdf")

    elif query.data == "gen_docx":
        if not items:
            await query.message.reply_text(MSG_EXPORT_EMPTY)
            return
        await query.message.reply_text(MSG_EXPORT_GENERATING.format(kind="DOCX", count=len(items)))
        await _export_pdf_session(context, query.message, user_id, user_id, items, name, fmt="docx")

    elif query.data == "clear_pdf":
        _reset_pdf_session(user_id)
        await query.message.reply_text(MSG_EXPORT_CLEARED_ALL)

# ═══════════════════════════════════════════════════════════════
# PDF/DOCX EXPORT — single source of truth, called from both the
# /pdf_generate and /pdf_clear commands and their button equivalents
# (gen_pdf/gen_docx/clear_pdf), so there's only one place that can
# forget to track XP or diverge in behavior between the two.
# ═══════════════════════════════════════════════════════════════
async def _finish_pdf_setup(context: ContextTypes.DEFAULT_TYPE, user_id: int, reply_target, edit: bool = False) -> None:
    """Last step of the /pdf_start flow (name → font → background) — opens
    the actual question buffer and shows the 'PDF mode activated' message.
    edit=True rewrites reply_target in place (the bg_skip button flow);
    edit=False sends a fresh reply (there's no bot-owned message to edit
    when this follows an uploaded background photo instead)."""
    PDF_BUFFER[user_id] = []
    PROGRESS_MSG_ID.pop(user_id, None)
    _clear_pending_image(user_id)
    _clear_clarify_queue(user_id)
    _clear_pending_edit(user_id)
    name = PDF_NAMES.get(user_id, "questions")
    text = (
        f"📥 <b>PDF mode activated</b> — File name: <i>{name}</i>\n\n"
        "• ابعت أسئلة نصية (MCQ أو مكتوبة)\n"
        "• أو <b>فوروارد</b> كويزات أو صور/جداول مقارنة\n\n"
        "اضغط <b>Export as PDF</b> أو <b>Export as DOCX</b> لما تخلص 👇"
    )
    send = reply_target.edit_text if edit else reply_target.reply_text
    await send(text, parse_mode=ParseMode.HTML)

def _reset_pdf_session(user_id: int) -> None:
    """Clears everything tied to an in-progress PDF-collection session —
    used after a successful export and by explicit clear/cancel alike."""
    _cleanup_images(user_id)
    _clear_pending_image(user_id)
    _clear_clarify_queue(user_id)
    _clear_pending_edit(user_id)
    PDF_BUFFER.pop(user_id, None)
    PDF_NAMES.pop(user_id, None)
    AWAITING_NAME.pop(user_id, None)
    AWAITING_FONT.pop(user_id, None)
    AWAITING_BG.pop(user_id, None)
    PROGRESS_MSG_ID.pop(user_id, None)
    font_path = PDF_FONT_PATH.pop(user_id, None)
    # Only delete it if it's a per-user upload (under FONT_BASE_DIR) — never
    # a bundled preset (under FONTS_DIR), which is a shared asset every
    # future user picks from, not something owned by this one session.
    if font_path and font_path.startswith(FONT_BASE_DIR) and os.path.exists(font_path):
        try:
            os.remove(font_path)
        except Exception:
            pass
    PDF_FONT_BOLD_PATH.pop(user_id, None)   # always a bundled preset path (or absent) — never a per-user file, nothing to delete
    bg_path = PDF_BG_IMAGE_PATH.pop(user_id, None)
    if bg_path and os.path.exists(bg_path):
        try:
            os.remove(bg_path)
        except Exception:
            pass

async def _export_pdf_session(context: ContextTypes.DEFAULT_TYPE, message, session_id: int,
                               analytics_uid: int, items: list, name: str, fmt: str) -> bool:
    """Builds a PDF or DOCX (fmt='pdf'|'docx') from items, sends it via
    message.reply_document, records the export XP/counter against
    analytics_uid, announces any level-up, and resets the session keyed by
    session_id. Returns False (having already replied with the reason) if
    DOCX isn't available or the build blew up."""
    safe = re.sub(r"[^\w\s\-]", "", name).strip().replace(" ", "_") or "questions"
    font_path      = PDF_FONT_PATH.get(session_id)
    font_bold_path = PDF_FONT_BOLD_PATH.get(session_id)
    bg_path        = PDF_BG_IMAGE_PATH.get(session_id)

    if fmt == "docx":
        if not DOCX_AVAILABLE:
            await message.reply_text(MSG_DOCX_UNAVAILABLE)
            return False
        try:
            doc_bytes = await asyncio.to_thread(
                build_docx, items, name, font_path=font_path,
                font_bold_path=font_bold_path, bg_image_path=bg_path,
            )
        except Exception as e:
            print("DOCX ERROR:", e)
            await message.reply_text(
                f"{quizzy_block(QUIZZY_OOPS_ART, random.choice(QUIZZY_ERROR_LINES))}\n\n<code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )
            return False
        await message.reply_document(
            document=doc_bytes, filename=f"{safe}.docx",
            caption=MSG_DOCX_CAPTION.format(count=len(items), name=name, quizzy_line=random.choice(QUIZZY_SUCCESS_LINES)),
            parse_mode=ParseMode.HTML,
        )
    else:
        try:
            pdf_bytes = await asyncio.to_thread(
                build_pdf, items, name, font_path=font_path,
                font_bold_path=font_bold_path, bg_image_path=bg_path,
            )
        except Exception as e:
            print("PDF ERROR:", e)
            await message.reply_text(
                f"{quizzy_block(QUIZZY_OOPS_ART, random.choice(QUIZZY_ERROR_LINES))}\n\n<code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )
            return False
        await message.reply_document(
            document=pdf_bytes, filename=f"{safe}.pdf",
            caption=MSG_PDF_CAPTION.format(count=len(items), name=name, quizzy_line=random.choice(QUIZZY_SUCCESS_LINES)),
            parse_mode=ParseMode.HTML,
        )

    q_count = sum(1 for it in items if it.get("type") in ("mcq", "written"))
    events  = await _record_activity(analytics_uid, questions_delta=q_count, pdfs_delta=1, session_questions=q_count)
    _update_telegram_name(analytics_uid, getattr(message, "from_user", None))
    await _announce_events(context, session_id, events, settings_uid=analytics_uid)
    await backup_analytics_to_channel(context)
    _reset_pdf_session(session_id)
    return True

# ═══════════════════════════════════════════════════════════════
# /report_issue — user sends a message, admin replies from
# REPORT_ISSUE_GROUP_ID, both sides visible on the same message, and the
# reporter can send follow-ups back into the same thread.
#
# Flow:
#   1. /report_issue -> AWAITING_REPORT_ISSUE[user_id] = True, bot asks for the text.
#   2. Next text message from that user (caught in handle()) is the report.
#      Posted to REPORT_ISSUE_GROUP_ID with a "↩️ Reply" button, and
#      recorded in REPORT_THREADS keyed by that group message's id — this
#      id is also shown on the card itself as the "Reply ID".
#   3. Admin replies one of two ways:
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
#      Either way: appended to the thread via _append_report_message, DM'd
#      to the user with their own "↩️ Reply" button, and the group message
#      is edited to show the full thread so far plus fresh Reply/Close
#      buttons.
#   4. Reporter taps their own "↩️ Reply" -> AWAITING_USER_FOLLOWUP[user_id]
#      = {...}; their next DM text is appended to the same thread (shown
#      to the admin under their name) and the group message is refreshed.
#      Blocked once the thread is closed.
#   5. Close just strips the buttons and marks the thread closed — no
#      further replies possible from that message (a re-tapped Reply, from
#      either side, is rejected with a toast/message).
#
# REPORT_THREADS is persisted the same way as MISTAKES_BANK: a local JSON
# file plus a pinned backup in REPORT_ISSUE_GROUP_ID, restored on startup
# (see restore_report_threads_from_channel). A restart mid-thread no
# longer loses the ability to keep replying to an old report — the thread
# reloads from the channel backup before polling starts.
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

async def report_issue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not REPORT_ISSUE_GROUP_ID:
        await update.message.reply_text("⚠️ الميزة دي مش متاحة دلوقتي.")
        return
    real_uid = update.effective_user.id if update.effective_user else update.effective_chat.id
    AWAITING_REPORT_ISSUE[real_uid] = True
    await update.message.reply_text(
        "✏️ اكتب مشكلتك أو ملاحظتك في رسالة واحدة، وهتوصل للأدمن على طول.",
    )

# ═══════════════════════════════════════════════════════════════
# PDF COMMANDS
# ═══════════════════════════════════════════════════════════════
async def pdf_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _pdf_access_allowed(update):
        await update.message.reply_text(MSG_PDF_ACCESS_DENIED)
        return
    user_id = update.effective_chat.id
    _reset_pdf_session(user_id)
    AWAITING_NAME[user_id] = True
    await update.message.reply_text(MSG_PDF_ASK_NAME, parse_mode=ParseMode.HTML)

async def pdf_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _pdf_access_allowed(update):
        await update.message.reply_text(MSG_PDF_ACCESS_DENIED)
        return
    user_id  = update.effective_chat.id
    real_uid = update.effective_user.id if update.effective_user else user_id
    items    = PDF_BUFFER.get(user_id, [])
    if not items:
        await update.message.reply_text(MSG_PDF_EMPTY)
        return
    name = PDF_NAMES.get(user_id, "questions")
    await update.message.reply_text(MSG_PDF_GENERATING.format(count=len(items)))
    await _export_pdf_session(context, update.message, user_id, real_uid, items, name, fmt="pdf")

async def pdf_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _pdf_access_allowed(update):
        await update.message.reply_text(MSG_PDF_ACCESS_DENIED)
        return
    user_id = update.effective_chat.id
    _reset_pdf_session(user_id)
    await update.message.reply_text(MSG_PDF_CLEARED)

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bails out of whatever's in progress: PDF collection session (or its
    font/background setup step) or a pending image waiting for its question."""
    user_id = update.effective_chat.id
    was_doing_something = bool(
        PDF_BUFFER.get(user_id) or AWAITING_NAME.get(user_id)
        or AWAITING_FONT.get(user_id) or AWAITING_BG.get(user_id)
        or PENDING_IMAGE.get(user_id)
    )
    _reset_pdf_session(user_id)
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
            "👋 Hello! What's your name? (Set a Nickname - it can be changed later)",
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
    lines.append("/mystats — your stats (questions created, day streak, lecture quiz results)")
    lines.append("⚙️ Settings — from the /start menu: set your nickname")
    lines.append("/pdf_start — starts a session collecting images for a PDF")
    lines.append("/pdf_generate — builds a PDF from the images you've collected")
    lines.append("/pdf_clear — clears the current PDF session")
    lines.append("/cancel — cancels whatever's currently in progress (PDF, pending image, etc.)")
    lines.append("/report_issue — send a message straight to the admin")
    lines.append("/quiz — browse lectures (year → module → subject → lecture) and pull their questions")
    lines.append("/time — current time, and when the next 💥Daily Quiz💥 push is")
    lines.append("/storage_id — gets this chat's ID (for setting STORAGE_GROUP_ID or LECTURE_RESULTS_GROUP_ID)")
    lines.append("/quiz_channel_id — gets a quiz channel's chat ID (forward a message from it first)")
    lines.append("/c — this list")

    if is_admin(update):
        lines.append("\n🔐 <b>Admin only</b>")
        lines.append("/admincheck — confirms you're an admin")
        lines.append("/health — bot status dashboard (users, lectures, backups, sessions, errors, uptime)")
        lines.append("/restore — manually re-pull one system's data from its currently-pinned channel backup")
        lines.append("/broadcast &lt;message&gt; — sends a message to every user")
        lines.append("/backup_now — instantly refreshes every pinned backup (storage + each year's quiz index)")
        lines.append("/quiz_list &lt;year&gt; — numbered list of every lecture (open and closed) in that year")
        lines.append("/quiz_delete &lt;year&gt; &lt;number&gt; — removes a lecture from that year's index")
        lines.append("/daily_module — restrict the Daily Quiz's subject pool to one module (or clear the restriction)")
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
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text(MSG_ADMIN_ONLY)
        return

    # Message text comes after /broadcast, or from a replied-to message
    if context.args:
        text = " ".join(context.args)
    elif update.message.reply_to_message and update.message.reply_to_message.text:
        text = update.message.reply_to_message.text
    else:
        await update.message.reply_text(
            "⚠️ استخدام:\n"
            "<code>/broadcast رسالتك هنا</code>\n\n"
            "أو رد بـ /broadcast على رسالة موجودة.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not text.strip():
        await update.message.reply_text("❌ الرسالة فارغة")
        return

    users_list = list(USERS)
    total      = len(users_list)

    status_msg = await update.message.reply_text(
        f"📡 <b>جاري الإرسال لـ {total} مستخدم...</b>",
        parse_mode=ParseMode.HTML,
    )

    success = 0
    failed  = 0
    blocked = []

    for uid in users_list:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=ParseMode.HTML,
            )
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

    # Remove users who blocked the bot
    if blocked:
        for uid in blocked:
            USERS.discard(uid)
        await save_users()
        await backup_storage_to_channel(context)

    summary = (
        f"✅ <b>Broadcast اتبعت!</b>\n\n"
        f"👥 المستخدمين: <b>{total}</b>\n"
        f"✔️ نجح: <b>{success}</b>\n"
        f"❌ فشل / بلوك: <b>{failed}</b>"
    )
    if blocked:
        summary += f"\n🗑 تم حذف {len(blocked)} يوزر بلوك البوت من القائمة"

    await status_msg.edit_text(summary, parse_mode=ParseMode.HTML)

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
ACHIEVEMENT_CATEGORY_LABEL = {
    "questions":         "❓ صانع الأسئلة",
    "streak":            "🔥 ملتزم",
    "pdfs":              "📚 صانع PDF",
    "speed":             "⚡ سريع",
    "lecture_questions": "🎓 طالب مجتهد",
    "lecture_streak":    "🎯 دقة",
}

async def _send_achievements(context: ContextTypes.DEFAULT_TYPE, user_id: int, reply_target, edit: bool = False) -> None:
    """Full achievements breakdown — every category, all 5 tiers each,
    marked unlocked/locked with its threshold. Shared by the /mystats
    'Achievements' button (only entry point for now). reply_target is a
    Message — edit=True rewrites it in place (button flow); edit=False
    sends a fresh reply (there's nothing bot-owned to edit yet, e.g. a
    freshly typed command)."""
    entry = _get_entry(user_id)
    ach   = entry.get("achievements", {})

    lines = ["🏆 <b>كل الإنجازات</b>\n"]
    for key, tiers in ACHIEVEMENTS.items():
        current = ach.get(key, 0)
        label   = ACHIEVEMENT_CATEGORY_LABEL.get(key, key)
        lines.append(f"{label} ({current}/5)")
        for i, (threshold, name, xp_bonus, emoji) in enumerate(tiers):
            tier = i + 1
            mark = "✅" if tier <= current else "🔒"
            lines.append(f"  {mark} {name} — {threshold}+")
        lines.append("")

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
        await send("📊 لسه معندكش إحصائيات. ابعت أسئلة وهتظهر هنا!")
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

    # achievements summary
    ach        = entry.get("achievements", {})
    ach_lines  = []
    icons      = {
        "questions": "❓", "streak": "🔥", "pdfs": "📚", "speed": "⚡",
        "lecture_questions": "🎓", "lecture_streak": "🎯",
    }
    for key, emoji in icons.items():
        tier = ach.get(key, 0)
        if tier:
            name = ACHIEVEMENTS[key][tier - 1][1]
            ach_lines.append(f"  {emoji} {name} {'⭐' * tier}")

    ach_text = "\n".join(ach_lines) if ach_lines else "  لسه مفيش إنجازات"

    send = reply_target.edit_text if edit else reply_target.reply_text
    await send(
        f"╔══════════════════╗\n"
        f"     🌐     YOUR PROFILE      🌐\n"
        f"╚══════════════════╝\n"
        f"Nickname: {html.escape(nickname)}\n"
        f"Role: Student (Default)\n"
        f"Year: {year_line}\n\n"
        f"🏅 Level: {level} — <i>{title}</i>\n"
        f"✨ XP: {xp:,}  [{bar}]  → {xp_end:,}\n\n"
        f"🔥 Current streak: {streak} days\n"
        f"🏅 Best streak: {streak_best} days\n\n"
        f"📚 Questions\n"
        f"   Answered: {lec_answered:,}\n"
        f"   Correct: {lec_correct:,}\n"
        f"   Accuracy: {accuracy:.1f}%\n\n"
        f"🧠 Mistakes Bank\n"
        f"  current: {mistake_count} questions\n\n"
        f"🏆 <b>Achievements</b>\n{ach_text}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏆 Achievements", callback_data="view_achievements"),
            InlineKeyboardButton("📚 More Quizzes", callback_data="quiz_years"),
            InlineKeyboardButton("🏠 Back to Home",  callback_data="back_home"),
        ]]),
    )

async def _send_settings(context: ContextTypes.DEFAULT_TYPE, user_id: int, reply_target, edit: bool = False) -> None:
    """Builds and sends the Settings screen to reply_target (an
    update.message or a callback_query.message). Mirrors _send_mystats:
    edit=True rewrites reply_target in place (button flow), edit=False
    sends a fresh reply."""
    nickname = get_nickname(user_id)
    nick_line = f"<b>{html.escape(nickname)}</b>" if nickname else "<i>مش متسجل — دوس تحت تحطه</i>"

    send = reply_target.edit_text if edit else reply_target.reply_text
    await send(
        f"⚙️ <b>الإعدادات</b>\n\n"
        f"👤 الاسم المستعار: {nick_line}",
        parse_mode=ParseMode.HTML,
        reply_markup=settings_menu_keyboard(user_id),
    )

async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Analytics are keyed by the person's real Telegram user id — not the
    # chat id — so this shows the same numbers whether /mystats is run in
    # a DM or inside a group/channel the bot is in.
    user_id = update.effective_user.id if update.effective_user else update.effective_chat.id
    await _send_mystats(context, user_id, update.message)


async def reset_analytics_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: wipe all analytics data locally and delete the pinned
    backup in the analytics group. Use when you need a clean slate."""
    global _analytics_backup_msg_id, _analytics_dirty
    if update.effective_chat.id != ADMIN_ID:
        return
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
    await update.message.reply_text("🗑 Analytics wiped — local file cleared and backup deleted.")

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

    if app.job_queue is None:
        print(
            "⚠️ No JobQueue available — periodic backup reconciliation, the "
            "analytics flush, and the Daily Quiz push are disabled. Local "
            "analytics from poll answers will only hit disk on the next "
            "immediate-save call site (restore/reset/import) or on a clean "
            "shutdown, not every 60s. Install with: "
            "pip install \"python-telegram-bot[job-queue]\""
        )
    else:
        app.job_queue.run_repeating(
            _reconcile_backups_job, interval=BACKUP_RECONCILE_INTERVAL, first=BACKUP_RECONCILE_INTERVAL,
        )
        app.job_queue.run_repeating(
            _flush_analytics_job, interval=ANALYTICS_FLUSH_INTERVAL, first=ANALYTICS_FLUSH_INTERVAL,
        )
        app.job_queue.run_daily(
            _daily_quiz_push_job, time=dt_time(hour=DAILY_QUIZ_HOUR, minute=DAILY_QUIZ_MIN, tzinfo=DAILY_QUIZ_TZ),
        )

async def _flush_analytics_job(context: ContextTypes.DEFAULT_TYPE):
    """Periodic tick for the hot-path debounce described above
    _analytics_dirty: writes analytics.json only if a poll answer marked it
    dirty since the last tick. No-ops (no deepcopy, no I/O) on a quiet tick."""
    await _flush_analytics_if_dirty()

async def _post_shutdown(app):
    """Runs once on a clean shutdown (PTB's own stop-signal handling calls
    this before the process exits) — flushes any analytics still sitting in
    memory from the last (< ANALYTICS_FLUSH_INTERVAL)-second window, so a
    normal restart/redeploy never loses data. Only a hard crash (killed
    process, power loss) can still lose that window; a clean stop cannot."""
    await _flush_analytics_if_dirty()

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
    SETTINGS_GROUP_ID, LECTURE_RESULTS_GROUP_ID,
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
    reads like 'Kareem tapped button toggle_language' instead of making
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
app.add_handler(CommandHandler("pdf_start",      pdf_start))
app.add_handler(CommandHandler("pdf_generate",   pdf_generate))
app.add_handler(CommandHandler("pdf_clear",      pdf_clear))
# Storage group setup helper
app.add_handler(CommandHandler("mystats",           mystats_cmd))
app.add_handler(CommandHandler("restore_analytics", restore_analytics_cmd))
app.add_handler(CommandHandler("import_analytics",  import_analytics_cmd))
app.add_handler(CommandHandler("reset_analytics",   reset_analytics_cmd))
app.add_handler(CommandHandler("storage_id",     storage_id_cmd))
app.add_handler(CommandHandler("backup_now",     backup_now_cmd))
# Quiz channel
app.add_handler(CommandHandler("quiz_channel_id", quiz_channel_id_cmd))
app.add_handler(CommandHandler("quiz",            quiz_lectures_cmd))
app.add_handler(CommandHandler("daily_module",     daily_module_cmd))
app.add_handler(CommandHandler("time",             time_cmd))
app.add_handler(CommandHandler("quiz_list",       quiz_list_cmd))
app.add_handler(CommandHandler("quiz_delete",     quiz_delete_cmd))

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

# Image handler (photos in PDF mode) — excludes the storage group
app.add_handler(MessageHandler(
    filters.PHOTO & ~filters.Chat(STORAGE_GROUP_ID), handle_image
))

# Font-file handler (.ttf/.otf uploads during /pdf_start setup) — must be
# registered before the general PDF/document handlers below since it's a
# different mime/extension entirely; excludes the storage group and quiz channel.
app.add_handler(MessageHandler(
    (filters.Document.FileExtension("ttf") | filters.Document.FileExtension("otf"))
    & ~filters.Chat(STORAGE_GROUP_ID) & ~filters.Chat(QUIZ_CHANNEL_IDS),
    handle_font_upload,
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
        f"[ {'OK' if DOCX_AVAILABLE else 'SKIP'} ] DOCX export module"
        + ("" if DOCX_AVAILABLE else " — python-docx not installed"),
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
