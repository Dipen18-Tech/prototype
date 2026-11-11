"""Read-side queries and derived data for the screens (no writes here)."""
from . import machine as M
from .models import Interview, Journey, Student
from .utils import BADGE_LEVEL_ORDER, CONFIG, MILES_PER_LEVEL, DAY, HOUR, MIN, fmt_date as _d, fmt_time as _t
from .services import available_times, deadline, open_seats, skill_progress

STATE_PRIORITY = [M.SCHEDULED, M.AWAITING_RESULT, M.ON_HOLD, M.ELIGIBLE, M.RETAKE, M.FAILED, M.NO_SHOW, M.REVOKED, M.CLOSED, M.PASSED, M.LOCKED]


def skill_miles(student):
    return sum(MILES_PER_LEVEL[lvl] for c in student.skills.all() if c.status == "cleared" for lvl in range(1, c.level + 1))


def miles(student, journeys=None):
    journeys = journeys if journeys is not None else student.journeys.select_related("badge").all()
    return skill_miles(student) + sum(j.badge.miles for j in journeys if j.state == M.PASSED) + student.bonus_miles


def leaderboard():
    """GRIT ranking: Qualiflyers first, then Miles → highest badge → skill depth → earliest milestone."""
    rows = []
    for s in Student.objects.prefetch_related("skills", "journeys__badge"):
        js = list(s.journeys.all())
        passed = [j for j in js if j.state == M.PASSED]
        rows.append({
            "student": s, "miles": miles(s, js), "qualiflyer": bool(passed),
            "top": max([BADGE_LEVEL_ORDER[j.badge.level] for j in passed], default=0),
            "depth": sum(c.level for c in s.skills.all() if c.status == "cleared"),
            "at": min([j.awarded_at.timestamp() for j in passed if j.awarded_at], default=float("inf")),
        })
    rows.sort(key=lambda r: (not r["qualiflyer"], -r["miles"], -r["top"], -r["depth"], r["at"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def rank_of(student):
    return next(r for r in leaderboard() if r["student"].pk == student.pk)


def primary_journey(journeys):
    return sorted(journeys, key=lambda j: (STATE_PRIORITY.index(j.state), -j.updated_at.timestamp()))[0]


def weak_areas(scores):
    labels = {"problem": "Problem solving", "concepts": "Concept depth", "build": "Hands-on build / code", "comm": "Communication"}
    return [labels[k] for k, v in (scores or {}).items() if v <= 2]


def stepper(journey, progress, iv):
    """Four-step progress strip for the student home."""
    s = journey.state
    was_passed = s == M.REVOKED and journey.prev_state == M.PASSED
    done = {
        "skills": s != M.LOCKED,
        "booked": s in (M.SCHEDULED, M.AWAITING_RESULT, M.PASSED, M.RETAKE, M.FAILED, M.NO_SHOW, M.CLOSED) or was_passed,
        "interviewed": s in (M.AWAITING_RESULT, M.PASSED, M.RETAKE, M.FAILED, M.CLOSED) or was_passed,
        "badge": s == M.PASSED,
    }
    last = ("Badge withdrawn" if s == M.REVOKED else "Badge earned", "")
    if s in M.COOLDOWN_STATES and journey.cooldown_until:
        last = ("Next attempt", journey.cooldown_until)
    elif s == M.CLOSED:
        last = ("No attempts left", "")
    steps = [
        ("skills", "Skills cleared", f"{sum(p['ok'] for p in progress)}/{len(progress)}"),
        ("booked", "Interview booked", iv.start_at if iv else ""),
        ("interviewed", "Interview done", ""),
        ("badge", last[0], last[1]),
    ]
    current = next((i for i, (k, _, _) in enumerate(steps) if not done[k]), None)
    return [{"label": lbl, "sub": sub, "done": done[k], "current": i == current, "bad": k == "badge" and s == M.REVOKED, "n": i + 1}
            for i, (k, lbl, sub) in enumerate(steps)]


def badge_shelf(journeys, track):
    levels = ["Novice", "Specialist", "Expert", "Master", "Grand Master"]
    by_level = {j.badge.level: j for j in journeys if j.badge.track == track}
    out = []
    for lvl in levels:
        j = by_level.get(lvl)
        state = j.state if j else None
        cls = "earned" if state == M.PASSED else "revoked" if state == M.REVOKED else "next" if j else ""
        out.append({"level": lvl, "cls": cls, "state": state})
    return out


def student_context(student, now, journey_id=None):
    """Everything the student screens need."""
    journeys = list(student.journeys.select_related("badge", "badge__next_badge").all())
    j = next((x for x in journeys if str(x.pk) == str(journey_id)), None) or primary_journey(journeys)
    progress = skill_progress(student, j.badge)
    history = list(j.interviews.select_related("interviewer", "outcome").order_by("-start_at"))
    iv = next((h for h in history if h.status == Interview.SCHEDULED), None)
    last = next((h for h in history if hasattr(h, "outcome")), None)
    lb = leaderboard()
    me = next(r for r in lb if r["student"].pk == student.pk)
    quals = [r for r in lb if r["qualiflyer"]]
    top = quals[:4] + ([me] if me["qualiflyer"] and me not in quals[:4] else [])
    return {
        "student": student, "journey": j, "badge": j.badge, "journeys": journeys, "others": [x for x in journeys if x.pk != j.pk],
        "progress": progress, "skills_have": sum(p["ok"] for p in progress), "skills_total": len(progress), "history": history, "iv": iv, "last": last,
        "last_outcome": getattr(last, "outcome", None) if last else None,
        "miles": me["miles"], "rank": me["rank"], "qualiflyer": me["qualiflyer"], "leaderboard": top, "qualiflyer_count": len(quals),
        "seats_week": open_seats(now, 7, j.badge.track), "deadline": deadline(now),
        "days_left": max(0, -(-(deadline(now) - now) // DAY)),
        "attempts_left": j.max_attempts - j.attempts_used,
        "stepper": stepper(j, progress, iv), "shelf": badge_shelf(journeys, j.badge.track),
        "weak": weak_areas(getattr(getattr(last, "outcome", None), "scores", None)) if last else [],
        "join_open": bool(iv and iv.start_at - CONFIG["JOIN_OPENS_MIN"] * MIN <= now <= iv.end_at),
        "can_change": bool(iv and now <= iv.start_at - CONFIG["CHANGE_CUTOFF_H"] * HOUR),
        "now": now,
        "first_name": student.name.split()[0],
        "greeting": _greeting(now),
        "first_missing": next((p for p in progress if not p["ok"]), None),
    }


def _greeting(now):
    from .utils import IST

    h = now.astimezone(IST).hour
    return "Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening"


def tracker_rows(now):
    """One row per journey for the ops tracker (and CSV export)."""
    rows = []
    live = {iv.journey_id: iv for iv in Interview.objects.filter(status=Interview.SCHEDULED).select_related("interviewer")}
    for j in Journey.objects.select_related("student", "badge").prefetch_related("student__skills"):
        iv, detail, risk, sort = live.get(j.pk), "", "", 0
        if j.state == M.ELIGIBLE:
            days = (now - (j.eligible_at or now)).days
            detail = f"Eligible {days}d, not booked" + (" · priority" if j.priority else "")
            if j.last_nudged_at:
                detail += f" · nudged {(now - j.last_nudged_at).days}d ago"
            sort = -days
            risk = "nudge" if days >= CONFIG["NUDGE_AFTER_DAYS"] else ""
        elif j.state == M.SCHEDULED and iv:
            detail, sort = f"{_d(iv.start_at)} {_t(iv.start_at)} · {iv.interviewer.name}", iv.start_at.timestamp()
        elif j.state == M.AWAITING_RESULT and iv:
            detail = f"Interviewed {_d(iv.start_at)} {_t(iv.start_at)} · {iv.interviewer.name}"
            risk = "overdue" if now > iv.end_at + CONFIG["OUTCOME_SLA_H"] * HOUR else ""
        elif j.state in M.COOLDOWN_STATES and j.cooldown_until:
            detail = f"Can rebook {_d(j.cooldown_until)}"
            risk = "deadline" if j.cooldown_until > deadline(now) else ""
        elif j.state == M.PASSED and j.awarded_at:
            detail = f"Awarded {_d(j.awarded_at)}"
        elif j.state == M.LOCKED:
            p = skill_progress(j.student, j.badge)
            detail = f"{sum(x['ok'] for x in p)}/{len(p)} skills"
        elif j.state == M.ON_HOLD:
            detail = "Integrity review open"
        elif j.state == M.REVOKED and j.revoked_at:
            detail = f"Nullified {_d(j.revoked_at)}"
        elif j.state == M.CLOSED:
            detail = f"{j.attempts_used} attempts used"
        rows.append({"j": j, "s": j.student, "b": j.badge, "iv": iv, "detail": detail, "risk": risk, "sort": sort})
    rows.sort(key=lambda r: (r["sort"], r["s"].name))
    return rows


__all__ = ["available_times", "leaderboard", "rank_of", "student_context", "tracker_rows", "miles", "primary_journey"]
