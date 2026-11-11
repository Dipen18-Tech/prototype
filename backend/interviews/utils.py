"""Shared helpers: errors, policy numbers, time helpers, UI metadata, middleware and context processor.

Kept dependency-free at import time (no imports of models/services at module level)
so every other module can import from here without circular imports.
"""
import logging
import time as _time
from datetime import datetime, timedelta, timezone

from django.utils import timezone as djtz

log = logging.getLogger("interviews")


# =============================================================== errors
class DomainError(Exception):
    """A rule violation the user should see (shown as a friendly message)."""


class Forbidden(Exception):
    """The signed-in user may not perform this action (HTTP 403)."""


# =============================================================== policy
# Server-side source of truth; changing a number here changes the UI copy too.
CONFIG = {
    "SLOT_MIN": 30,              # interview length
    "BOOK_MIN_LEAD_H": 12,       # earliest bookable slot (interviewer prep time)
    "PRIORITY_LEAD_H": 2,        # lead time after a cancellation on our side
    "BOOK_WINDOW_DAYS": 14,      # how far ahead students can book
    "CHANGE_CUTOFF_H": 4,        # reschedule / cancel allowed until 4h before
    "RESCHEDULE_LIMIT": 2,       # per booking
    "JOIN_OPENS_MIN": 10,        # join button goes live 10 min before
    "NO_SHOW_GRACE_MIN": 10,     # interviewer can mark no-show 10 min after start
    "OUTCOME_SLA_H": 24,         # outcome must be recorded within 24h
    "MAX_ATTEMPTS": 3,           # per badge per season
    "COOLDOWN_DAYS": {"RETAKE": 7, "FAILED": 21, "NO_SHOW": 3},
    "MIN_FEEDBACK_CHARS": 40,    # student-facing feedback for non-pass outcomes
    "NUDGE_AFTER_DAYS": 3,
}

BADGE_DEADLINE = datetime(2026, 12, 31, 18, 29, tzinfo=timezone.utc)  # 31 Dec 2026, 23:59 IST

SKILLS = {
    "CT": "Computational Thinking",
    "CSF": "CS Fundamentals",
    "GENAI": "Applied Gen AI Development",
    "UI": "UI Engineering",
    "SSE": "Server Side Engineering",
    "SQL": "SQL",
    "SD": "System Design",
    "DSML": "DS & ML",
}

# Seed data for the Badge table. AI Product Mastery · Novice mirrors the published GRIT example.
BADGES = [
    {"code": "APM-NOV", "track": "AI Product Mastery", "level": "Novice", "miles": 500,
     "req": [["CT", 1], ["CSF", 1], ["GENAI", 1], ["UI", 2]], "next": "APM-SPE"},
    {"code": "APM-SPE", "track": "AI Product Mastery", "level": "Specialist", "miles": 1000,
     "req": [["CT", 2], ["GENAI", 2], ["UI", 3], ["SSE", 2], ["SD", 1]], "next": None},
    {"code": "ASM-NOV", "track": "AI Systems Mastery", "level": "Novice", "miles": 500,
     "req": [["CT", 1], ["CSF", 1], ["SSE", 1], ["SQL", 1]], "next": None},
]

MILES_PER_LEVEL = {1: 100, 2: 200, 3: 350, 4: 500}
RUBRIC_KEYS = ["problem", "concepts", "build", "comm"]
BADGE_LEVEL_ORDER = {"Novice": 1, "Specialist": 2, "Expert": 3, "Master": 4, "Grand Master": 5}


# =============================================================== time
# Stored in UTC; business days are IST (UTC+5:30, no DST). The clock is overridable
# (DemoClock) so the demo can fast-forward through cooldowns and SLAs.
IST = timezone(timedelta(hours=5, minutes=30))
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def real_now():
    return djtz.now()


def now():
    """Current (possibly simulated) time."""
    from .models import DemoClock  # local import: models imports this module

    clock = DemoClock.objects.filter(pk=1).only("offset_seconds").first()
    return real_now() + timedelta(seconds=clock.offset_seconds) if clock else real_now()


def day_start(dt):
    """Midnight IST of the day containing dt, returned in UTC."""
    local = dt.astimezone(IST)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def ist_weekday(dt):
    """JS-style weekday (0=Sunday … 6=Saturday) in IST, matching the weekly pattern format."""
    return (dt.astimezone(IST).weekday() + 1) % 7


def to_ms(dt):
    return int(dt.timestamp() * 1000) if dt else None


def from_ms(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        raise DomainError("Invalid time.")


def fmt_date(dt):
    return dt.astimezone(IST).strftime("%a, %d %b").replace(" 0", " ")


def fmt_time(dt):
    return dt.astimezone(IST).strftime("%I:%M %p").lstrip("0")


# =============================================================== UI metadata
# label = what the interview team sees; student = what the student sees; tone drives colour.
# (Plain string keys so this module never imports machine.py.)
STATE_META = {
    "LOCKED": {"label": "Skills in progress", "student": "Clear your skills first", "tone": "muted", "desc": "Not all required skills for the badge are cleared yet."},
    "ELIGIBLE": {"label": "Eligible · not booked", "student": "Ready to book", "tone": "info", "desc": "All skills cleared. Student can book a slot."},
    "SCHEDULED": {"label": "Booked", "student": "Interview booked", "tone": "primary", "desc": "Slot confirmed with an interviewer. Can reschedule/cancel until cutoff."},
    "AWAITING_RESULT": {"label": "Awaiting outcome", "student": "Result on the way", "tone": "warn", "desc": "Interview time has passed; outcome not yet recorded (24h SLA)."},
    "PASSED": {"label": "Passed · badge earned", "student": "Badge earned", "tone": "success", "desc": "Badge awarded, Miles credited, student becomes a Qualiflyer."},
    "RETAKE": {"label": "Retake later", "student": "Retake unlocks soon", "tone": "warn", "desc": "Close to the bar. 7-day cooldown, then eligible to rebook."},
    "FAILED": {"label": "Not cleared", "student": "Not cleared this time", "tone": "danger", "desc": "Below the bar. 21-day cooldown, then eligible to rebook."},
    "NO_SHOW": {"label": "Student no-show", "student": "Missed interview", "tone": "danger", "desc": "Student did not join. 3-day booking block; 2nd no-show uses an attempt."},
    "ON_HOLD": {"label": "On hold · integrity review", "student": "On hold", "tone": "danger", "desc": "Integrity flag open. Booking released, badge frozen until reviewed."},
    "REVOKED": {"label": "Badge nullified", "student": "Badge nullified", "tone": "danger", "desc": "Malpractice confirmed. Badge and its Miles removed for the season."},
    "CLOSED": {"label": "Attempts exhausted", "student": "No attempts left", "tone": "muted", "desc": "All attempts used without a pass. Ops can grant an extra attempt."},
}

OUTCOME_PILL = {
    "pass": ("success", "Passed"), "retake": ("warn", "Retake later"), "fail": ("danger", "Not cleared"),
    "no_show": ("danger", "Missed"), "tech_issue": ("muted", "Technical issue"),
}

RUBRIC = [("problem", "Problem solving"), ("concepts", "Concept depth"), ("build", "Hands-on build / code"), ("comm", "Communication")]

# Minimal stroke icon set (Lucide-style paths)
ICONS = {
    "calendar": '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18M6 6l12 12"/>',
    "plane": '<path d="M17.8 19.2 16 11l3.5-3.5C21 6 21.5 4 21 3c-1-.5-3 0-4.5 1.5L13 8 4.8 6.2c-.5-.1-.9.1-1.1.5l-.3.5c-.2.5-.1 1 .3 1.3L9 12l-2 3H4l-1 1 3 2 2 3 1-1v-3l3-2 3.5 5.3c.3.4.8.5 1.3.3l.5-.2c.4-.3.6-.7.5-1.2z"/>',
    "award": '<circle cx="12" cy="8" r="6"/><path d="M15.48 12.89 17 22l-5-3-5 3 1.52-9.11"/>',
    "bell": '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/>',
    "users": '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
    "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4M12 17h.01"/>',
    "left": '<path d="m15 18-6-6 6-6"/>',
    "right": '<path d="m9 18 6-6-6-6"/>',
    "video": '<path d="m22 8-6 4 6 4V8z"/><rect x="2" y="6" width="14" height="12" rx="2"/>',
    "refresh": '<path d="M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5"/>',
    "flag": '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1zM4 22v-7"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "list": '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    "home": '<path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M9 22V12h6v10"/>',
    "grid": '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>',
    "send": '<path d="m22 2-7 20-4-9-9-4z"/><path d="M22 2 11 13"/>',
    "star": '<path d="m12 2 3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/>',
    "sparkle": '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1"/>',
    "file": '<path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>',
    "compare": '<rect x="3" y="3" width="8" height="18" rx="1"/><rect x="13" y="3" width="8" height="18" rx="1"/>',
    "trend": '<path d="m22 7-8.5 8.5-5-5L2 17"/><path d="M16 7h6v6"/>',
    "target": '<circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
}


# =============================================================== middleware
class SystemJobsMiddleware:
    """Runs the time-based jobs lazily (throttled) so the app is correct even without a cron.
    In production also schedule `python manage.py run_jobs` every minute."""

    INTERVAL_S = 15
    _last_run = 0.0

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(("/static/", "/admin/")) and _time.monotonic() - SystemJobsMiddleware._last_run > self.INTERVAL_S:
            SystemJobsMiddleware._last_run = _time.monotonic()
            try:
                from .services import run_system

                run_system()
            except Exception:  # noqa: BLE001 — jobs must never take the site down; they retry next request
                log.exception("run_system failed")
        return self.get_response(request)


# =============================================================== context processor
def template_globals(request):
    """Available on every page: who's signed in, nav badges, demo controls."""
    from django.conf import settings

    from .models import IntegrityFlag, Interview, Interviewer, Journey, Notification, Student
    from .selectors import STATE_PRIORITY
    from .services import interviewer_of, role_of, student_of

    user = getattr(request, "user", None)
    role = role_of(user) if user else None
    current = now()
    ctx = {"role": role, "now": current, "demo_mode": settings.DEMO_MODE, "me_name": "", "nav_counts": {}}
    if role == "student":
        s = student_of(user)
        ctx["me_name"], ctx["me_sub"] = s.name, f"{s.campus} · {s.batch} batch"
        ctx["nav_counts"]["inbox"] = Notification.objects.filter(student=s, read_at__isnull=True).count()
    elif role == "interviewer":
        iv = interviewer_of(user)
        ctx["me_name"], ctx["me_sub"] = iv.name, iv.title
        ctx["nav_counts"]["schedule"] = Interview.objects.filter(interviewer=iv, status=Interview.SCHEDULED, start_at__lte=current).count()
    elif role == "ops":
        ctx["me_name"], ctx["me_sub"] = "Interview Ops", "All campuses"
        ctx["nav_counts"]["outcomes"] = Journey.objects.filter(state="AWAITING_RESULT").count()
        ctx["nav_counts"]["integrity"] = IntegrityFlag.objects.filter(status="open").count()

    if settings.DEMO_MODE and user and user.is_authenticated:
        # Quick switcher: every demo account, labelled with its current state.
        def primary_label(student):
            js = sorted(student.journeys.all(), key=lambda j: STATE_PRIORITY.index(j.state))
            return STATE_META[js[0].state]["label"] if js else "—"

        students = [(s.user.username, f"{s.name} — {primary_label(s)}") for s in Student.objects.select_related("user").prefetch_related("journeys") if s.user]
        ivs = [(i.user.username, i.name) for i in Interviewer.objects.select_related("user") if i.user]
        ctx["switch_groups"] = [("Students", students), ("Interviewers", ivs), ("Team", [("ops", "Interview Ops")])]
        ctx["me_username"] = user.username
    return ctx
