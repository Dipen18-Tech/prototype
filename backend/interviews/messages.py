"""Every student-facing message, written exactly as it appears.

Notifications are stored as (kind, payload) and rendered here at read time, so copy fixes apply
to old messages too and the same text can later go out by email / WhatsApp.
"""
from .utils import BADGE_DEADLINE, CONFIG, fmt_date, fmt_time, from_ms


def _badge_name(b):
    return f"{b['track']} · {b['level']}"


def _deadline():
    return fmt_date(BADGE_DEADLINE).split(", ", 1)[1]


def _d(ms):
    return fmt_date(from_ms(ms))


def _t(ms):
    return fmt_time(from_ms(ms))


# ★ marks the three key messages called out in the write-up.
TEMPLATES = {
    # ★1 The unlock — turns "eligible" into "booked".
    "ELIGIBLE": lambda c: dict(
        title=f"You've unlocked your {c['badge']['level']} interview",
        body=f"You've cleared all {len(c['badge']['req'])} skills for {_badge_name(c['badge'])}. One step left: a 30-minute Level Clearance Interview with the GRIT team. Pick a slot that suits you — interviews must be done by {_deadline()} to count.",
        cta="Book my interview", tone="info", key=1),
    # ★2 The confirmation — makes sure the student shows up, prepared.
    "BOOKED": lambda c: dict(
        title=f"Interview booked: {_d(c['start'])} at {_t(c['start'])}",
        body=f"{_badge_name(c['badge'])} · 30 min with {c['interviewer']}. Join from your GRIT dashboard — the Join button goes live 10 minutes before. Keep your college ID and a working camera ready. Need to change it? You can reschedule twice, up to 4 hours before.",
        cta="View interview details", tone="primary", key=2),
    # ★3 The "not yet" — keeps the student motivated and says exactly what's next.
    "RETAKE": lambda c: dict(
        title=f"Not yet — you can retake from {_d(c['until'])}",
        body=f"You were close on {_badge_name(c['badge'])}. Your interviewer's note: “{c['feedback']}” Your next attempt unlocks on {_d(c['until'])}. Attempts used: {c['used']} of {c['max']}.",
        cta="See full feedback", tone="warn", key=3),

    "PASSED": lambda c: dict(
        title=f"Badge earned: {_badge_name(c['badge'])}",
        body=f"Congratulations, {c['first']}! +{c['badge']['miles']} Miles added. You're now a Qualiflyer at #{c['rank']} on the leaderboard."
             + (f" Next up: {c['next_badge']['level']} — {c['next_have']} of {len(c['next_badge']['req'])} skills already cleared." if c.get("next_badge") else ""),
        cta="See my badge", tone="success"),
    "FAILED": lambda c: dict(
        title="Not cleared this time",
        body=f"Your {_badge_name(c['badge'])} interview didn't meet the bar yet. Your interviewer's note: “{c['feedback']}” Your next attempt unlocks on {_d(c['until'])}. Attempts used: {c['used']} of {c['max']}.",
        cta="See full feedback", tone="danger"),
    "CLOSED": lambda c: dict(
        title=f"No attempts left for {c['badge']['level']}",
        body=f"You've used all {c['max']} attempts for {_badge_name(c['badge'])} this season. Your feedback is saved on your dashboard. If something affected your interviews, talk to your campus GRIT team.",
        cta="See feedback", tone="muted"),
    "NO_SHOW": lambda c: dict(
        title="We missed you at your interview",
        body=f"You didn't join your {_badge_name(c['badge'])} interview on {_d(c['start'])} at {_t(c['start'])}. You can book a new slot from {_d(c['until'])}. "
             + ("This missed interview used one of your attempts." if c.get("counted") else "A second missed interview will use one of your attempts."),
        cta="Got it", tone="danger"),
    "COOLDOWN_ENDED": lambda c: dict(
        title="Your next attempt is unlocked",
        body=f"You can now book your {_badge_name(c['badge'])} interview again. Attempts left: {c['left']}. Slots before {_deadline()} fill up fast.",
        cta="Book my interview", tone="info"),
    "RESCHEDULED": lambda c: dict(
        title=f"Interview moved to {_d(c['start'])} at {_t(c['start'])}",
        body=f"{_badge_name(c['badge'])} · 30 min with {c['interviewer']}. Reschedules left for this booking: {c['left']}.",
        cta="View interview details", tone="primary"),
    "CANCELLED": lambda c: dict(
        title="Interview cancelled",
        body=f"Your {_d(c['start'])} {_t(c['start'])} interview is cancelled. No attempt was used — book a new slot whenever you're ready.",
        cta="Book a new slot", tone="muted"),
    "CANCELLED_BY_TEAM": lambda c: dict(
        title="Your interview needs a new time",
        body=f"Sorry — we had to cancel your {_d(c['start'])} {_t(c['start'])} interview ({c['reason'].lower()}). This doesn't use an attempt, and you've got priority: you can book slots as soon as 2 hours from now.",
        cta="Book a new slot", tone="warn"),
    "REASSIGNED": lambda c: dict(
        title="Same time, new interviewer",
        body=f"Your {_d(c['start'])} {_t(c['start'])} interview is still on — it will now be taken by {c['interviewer']}. Nothing else changes.",
        cta="View interview details", tone="primary"),
    "TECH_ISSUE": lambda c: dict(
        title="Let's try that interview again",
        body="Your interview couldn't be completed because of a technical issue. It doesn't count as an attempt, and you've got priority booking — slots from 2 hours from now are open to you.",
        cta="Book a new slot", tone="warn"),
    "ON_HOLD": lambda c: dict(
        title=f"Your {c['badge']['level']} journey is on hold",
        body=f"Your {_badge_name(c['badge'])} progress is under an integrity review. Any booked interview has been released. We'll update you within 5 working days — for questions, contact your campus GRIT team.",
        cta=None, tone="danger"),
    "FLAG_CLEARED": lambda c: dict(
        title="Review closed — you're all clear",
        body=f"The integrity review on {_badge_name(c['badge'])} is closed with no action. Everything is back as it was"
             + (" — please book a new interview slot, since your earlier one was released." if c.get("was_booked") else "."),
        cta="Book my interview" if c.get("was_booked") else None, tone="success"),
    "REVOKED": lambda c: dict(
        title=f"Your {c['badge']['level']} badge has been withdrawn",
        body=f"Following an integrity review, your {_badge_name(c['badge'])} "
             + (f"badge and its {c['badge']['miles']} Miles have" if c.get("had_badge") else "progress has")
             + " been withdrawn for this season. If you'd like to appeal, contact your campus GRIT team within 7 days.",
        cta=None, tone="danger"),
    "NUDGE": lambda c: dict(
        title="Your interview slot is waiting",
        body=f"You unlocked your {_badge_name(c['badge'])} interview {c['days']} days ago. {c['open']} slots are open in the next 7 days — book now before they fill up.",
        cta="Book my interview", tone="info"),
    "EXTRA_ATTEMPT": lambda c: dict(
        title="You've been given another attempt",
        body=f"The GRIT team has granted you one more {_badge_name(c['badge'])} interview attempt. Book a slot whenever you're ready.",
        cta="Book my interview", tone="info"),
}

# Where each message's button takes the student.
CTA_TARGET = {
    "ELIGIBLE": "book", "COOLDOWN_ENDED": "book", "CANCELLED": "book", "CANCELLED_BY_TEAM": "book", "TECH_ISSUE": "book",
    "NUDGE": "book", "EXTRA_ATTEMPT": "book", "FLAG_CLEARED": "book",
    "BOOKED": "interview", "RESCHEDULED": "interview", "REASSIGNED": "interview", "RETAKE": "interview", "FAILED": "interview", "CLOSED": "interview",
}


def badge_dict(badge):
    """Plain-dict view of a Badge model (what templates and payloads use)."""
    return {"code": badge.code, "track": badge.track, "level": badge.level, "miles": badge.miles, "req": badge.required_skills}


def render(kind, payload):
    """Render a stored notification. Never raises — a broken payload shows a generic message."""
    try:
        ctx = {"max": CONFIG["MAX_ATTEMPTS"], **payload}
        return TEMPLATES[kind](ctx)
    except Exception:  # noqa: BLE001 — a malformed historic payload must not break the inbox
        return dict(title="Update on your interview", body="Open your dashboard for details.", cta=None, tone="muted")
