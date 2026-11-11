"""The student interview journey state machine.

Every state change in services.py goes through next_state(); anything not listed here is rejected.
The frontend's "How it works" tab renders this exact table (sent in the snapshot).
"""
from .utils import DomainError

LOCKED = "LOCKED"
ELIGIBLE = "ELIGIBLE"
SCHEDULED = "SCHEDULED"
AWAITING_RESULT = "AWAITING_RESULT"
PASSED = "PASSED"
RETAKE = "RETAKE"
FAILED = "FAILED"
NO_SHOW = "NO_SHOW"
ON_HOLD = "ON_HOLD"
REVOKED = "REVOKED"
CLOSED = "CLOSED"

STATES = [LOCKED, ELIGIBLE, SCHEDULED, AWAITING_RESULT, PASSED, RETAKE, FAILED, NO_SHOW, ON_HOLD, REVOKED, CLOSED]
COOLDOWN_STATES = [RETAKE, FAILED, NO_SHOW]
PREVIOUS = "↩ previous"
_FLAGGABLE = [LOCKED, ELIGIBLE, SCHEDULED, AWAITING_RESULT, PASSED, RETAKE, FAILED, NO_SHOW]
_RECORDABLE = [SCHEDULED, AWAITING_RESULT]

TRANSITIONS = [
    {"event": "SKILLS_CLEARED", "from": [LOCKED], "to": ELIGIBLE, "actor": "System", "note": "Last required skill cleared → student notified to book."},
    {"event": "SKILL_REVOKED", "from": [ELIGIBLE, SCHEDULED], "to": LOCKED, "actor": "System", "note": "A skill clearance is withdrawn; any booking is released."},
    {"event": "BOOK", "from": [ELIGIBLE], "to": SCHEDULED, "actor": "Student", "note": "Picks an open slot ≥12h ahead; system assigns the least-loaded interviewer."},
    {"event": "RESCHEDULE", "from": [SCHEDULED], "to": SCHEDULED, "actor": "Student", "note": "Max 2 per booking, until 4h before start."},
    {"event": "CANCEL", "from": [SCHEDULED], "to": ELIGIBLE, "actor": "Student", "note": "Until 4h before. No attempt used."},
    {"event": "CANCEL_BY_TEAM", "from": [SCHEDULED], "to": ELIGIBLE, "actor": "Interviewer / Ops", "note": "Only if no other interviewer can take the slot. Student gets priority rebooking."},
    {"event": "SLOT_ENDED", "from": [SCHEDULED], "to": AWAITING_RESULT, "actor": "System", "note": "Interview end time passes without an outcome."},
    {"event": "RECORD_PASS", "from": _RECORDABLE, "to": PASSED, "actor": "Interviewer", "note": "Badge + Miles awarded instantly."},
    {"event": "RECORD_RETAKE", "from": _RECORDABLE, "to": RETAKE, "actor": "Interviewer", "note": "Feedback mandatory. Uses an attempt. 7-day cooldown."},
    {"event": "RECORD_FAIL", "from": _RECORDABLE, "to": FAILED, "actor": "Interviewer", "note": "Feedback mandatory. Uses an attempt. 21-day cooldown."},
    {"event": "RECORD_NO_SHOW", "from": _RECORDABLE, "to": NO_SHOW, "actor": "Interviewer", "note": "Allowed 10 min after start. 3-day block."},
    {"event": "RECORD_TECH_ISSUE", "from": _RECORDABLE, "to": ELIGIBLE, "actor": "Interviewer", "note": "Call failed / incomplete. No attempt used, priority rebooking."},
    {"event": "ATTEMPTS_EXHAUSTED", "from": COOLDOWN_STATES, "to": CLOSED, "actor": "System", "note": "3rd non-pass attempt."},
    {"event": "COOLDOWN_ENDED", "from": COOLDOWN_STATES, "to": ELIGIBLE, "actor": "System", "note": "Cooldown date reached → student notified."},
    {"event": "EXTRA_ATTEMPT", "from": [CLOSED], "to": ELIGIBLE, "actor": "Ops", "note": "Exception granted, reason logged."},
    {"event": "FLAG_RAISED", "from": _FLAGGABLE, "to": ON_HOLD, "actor": "Ops", "note": "Malpractice reported. Booking released, badge frozen."},
    {"event": "FLAG_DISMISSED", "from": [ON_HOLD], "to": PREVIOUS, "actor": "Ops", "note": "Restores prior state (a released booking returns to Eligible)."},
    {"event": "FLAG_CONFIRMED", "from": [ON_HOLD], "to": REVOKED, "actor": "Ops", "note": "Badge nullified, Miles reversed, removed from Qualiflyer board."},
]

_LABELS = {
    LOCKED: "Skills in progress", ELIGIBLE: "Eligible · not booked", SCHEDULED: "Booked", AWAITING_RESULT: "Awaiting outcome",
    PASSED: "Passed · badge earned", RETAKE: "Retake later", FAILED: "Not cleared", NO_SHOW: "Student no-show",
    ON_HOLD: "On hold · integrity review", REVOKED: "Badge nullified", CLOSED: "Attempts exhausted",
}


def can(state, event):
    return any(t["event"] == event and state in t["from"] for t in TRANSITIONS)


def next_state(state, event, prev=None):
    """Return the target state, or raise DomainError if the move is not allowed."""
    for t in TRANSITIONS:
        if t["event"] == event and state in t["from"]:
            if t["to"] == PREVIOUS:
                # A released booking can't come back — the slot may be gone.
                return ELIGIBLE if prev in (None, SCHEDULED, AWAITING_RESULT) else prev
            return t["to"]
    raise DomainError(f"Can't {event.lower().replace('_', ' ')} while {_LABELS.get(state, state)}.")
