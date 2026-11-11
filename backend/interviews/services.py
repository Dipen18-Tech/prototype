"""Domain services: every action that changes data lives here.

Rules:
  1. Each public action is one DB transaction (all-or-nothing).
  2. Every journey state change goes through _transition() → validated by machine.next_state and audit-logged.
  3. Permission checks happen here too, so no view can forget them.
  4. User-facing rule violations raise DomainError; permission problems raise Forbidden.
"""
import logging
import secrets
from collections import defaultdict
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from . import machine as M
from .utils import DomainError, Forbidden, BADGE_DEADLINE, CONFIG, RUBRIC_KEYS, SKILLS, DAY, HOUR, MIN, day_start, fmt_date, fmt_time, ist_weekday, now as clock_now, to_ms
from .messages import badge_dict
from .models import (AvailabilitySlot, DemoClock, IntegrityFlag, Interview, InterviewOutcome, Interviewer, Journey,
                     JourneyEvent, Notification, SkillClearance, Student)

log = logging.getLogger("interviews")
SLOT = timedelta(minutes=CONFIG["SLOT_MIN"])


# ---------------------------------------------------------------- roles
def student_of(user):
    try:
        return user.student
    except (AttributeError, Student.DoesNotExist):
        return None


def interviewer_of(user):
    try:
        return user.interviewer
    except (AttributeError, Interviewer.DoesNotExist):
        return None


def role_of(user):
    if not user or not user.is_authenticated:
        return None
    if user.is_staff:
        return "ops"
    if interviewer_of(user):
        return "interviewer"
    if student_of(user):
        return "student"
    return None


def _require_ops(user):
    if role_of(user) != "ops":
        raise Forbidden()


# ---------------------------------------------------------------- shared helpers
def deadline(now):
    # Fallback keeps the demo usable if it's opened after the real deadline.
    return BADGE_DEADLINE if now < BADGE_DEADLINE else day_start(now) + 50 * DAY


def skill_progress(student, badge):
    have = {c.skill: c.level for c in student.skills.all() if c.status == "cleared"}
    return [{"key": k, "name": SKILLS.get(k, k), "need": lvl, "have": have.get(k, 0), "ok": have.get(k, 0) >= lvl}
            for k, lvl in badge.required_skills]


def _lead(journey):
    return (CONFIG["PRIORITY_LEAD_H"] if journey.priority else CONFIG["BOOK_MIN_LEAD_H"]) * HOUR


def _booked_counts(day_starts):
    """{(interviewer_id, day_start): booked slot count} for the given IST days."""
    if not day_starts:
        return {}
    lo, hi = min(day_starts), max(day_starts) + DAY
    out = defaultdict(int)
    for iid, st in AvailabilitySlot.objects.filter(status=AvailabilitySlot.BOOKED, start_at__gte=lo, start_at__lt=hi).values_list("interviewer_id", "start_at"):
        out[(iid, day_start(st))] += 1
    return out


def _previous_interviewer_ids(journey, ignore_interview_id=None):
    qs = Interview.objects.filter(journey=journey, outcome__isnull=False)
    if ignore_interview_id:
        qs = qs.exclude(pk=ignore_interview_id)
    return set(qs.values_list("interviewer_id", flat=True))


def candidates(journey, start, ignore_interview_id=None, counts=None):
    """Open slots at `start` that can take this journey, best (least-loaded, fresh interviewer) first."""
    track = journey.badge.track
    slots = [s for s in AvailabilitySlot.objects.filter(start_at=start, status=AvailabilitySlot.OPEN).select_related("interviewer")
             if s.interviewer.active and track in s.interviewer.tracks]
    counts = counts if counts is not None else _booked_counts([day_start(start)])
    d = day_start(start)
    slots = [s for s in slots if counts.get((s.interviewer_id, d), 0) < s.interviewer.max_per_day]
    prev = _previous_interviewer_ids(journey, ignore_interview_id)
    fresh = [s for s in slots if s.interviewer_id not in prev]
    if fresh:  # retakes go to a fresh pair of eyes when possible
        slots = fresh
    return sorted(slots, key=lambda s: counts.get((s.interviewer_id, d), 0))


def available_times(journey, now, ignore_interview_id=None):
    """Bookable start times grouped by IST day: [(day_start, [{"start", "seats"}])]."""
    lo = now + _lead(journey)
    hi = min(now + CONFIG["BOOK_WINDOW_DAYS"] * DAY, deadline(now))
    open_slots = list(AvailabilitySlot.objects.filter(status=AvailabilitySlot.OPEN, start_at__gte=lo, start_at__lte=hi - SLOT)
                      .select_related("interviewer").order_by("start_at"))
    track = journey.badge.track
    others = list(Interview.objects.filter(journey__student=journey.student, status=Interview.SCHEDULED)
                  .exclude(pk=ignore_interview_id).values_list("start_at", flat=True))
    counts = _booked_counts({day_start(s.start_at) for s in open_slots})
    prev = _previous_interviewer_ids(journey, ignore_interview_id)
    by_start = defaultdict(list)
    for s in open_slots:
        iv = s.interviewer
        if iv.active and track in iv.tracks and counts.get((iv.pk, day_start(s.start_at)), 0) < iv.max_per_day:
            by_start[s.start_at].append(s)
    days = defaultdict(list)
    for start, slots in sorted(by_start.items()):
        if any(abs(o - start) < SLOT for o in others):
            continue
        fresh = [s for s in slots if s.interviewer_id not in prev]
        days[day_start(start)].append({"start": start, "seats": len(fresh or slots)})
    return sorted(days.items())


def open_seats(now, days=7, track=None):
    """Real bookable capacity: open slots, capped by each interviewer's remaining daily limit."""
    lo, hi = now + CONFIG["BOOK_MIN_LEAD_H"] * HOUR, now + days * DAY
    per = defaultdict(int)
    ivs = {}
    for s in AvailabilitySlot.objects.filter(status=AvailabilitySlot.OPEN, start_at__gte=lo, start_at__lt=hi).select_related("interviewer"):
        if track and track not in s.interviewer.tracks:
            continue
        per[(s.interviewer_id, day_start(s.start_at))] += 1
        ivs[s.interviewer_id] = s.interviewer
    counts = _booked_counts({d for _, d in per})
    return sum(max(0, min(n, ivs[iid].max_per_day - counts.get((iid, d), 0))) for (iid, d), n in per.items())


def _audit(journey, event, from_state, to_state, actor, detail, now):
    JourneyEvent.objects.create(journey=journey, student_id=journey.student_id if journey else None, event=event,
                                from_state=from_state, to_state=to_state, actor=actor, detail=detail or "", created_at=now)


def _transition(journey, event, actor, detail, now, prev=None):
    frm = journey.state
    journey.state = M.next_state(frm, event, prev)
    journey.updated_at = now
    journey.version += 1
    journey.save()
    _audit(journey, event, frm, journey.state, actor, detail, now)
    return journey.state


def _notify(student, kind, payload, now, journey=None):
    Notification.objects.create(student=student, journey=journey, kind=kind, created_at=now,
                                payload={"first": student.name.split()[0], **payload})


def _release_slot(iv, now):
    slot = iv.slot
    if slot:
        # Only re-open future slots; a past slot has no value to anyone.
        slot.status = AvailabilitySlot.OPEN if slot.start_at > now else AvailabilitySlot.EXPIRED
        slot.save(update_fields=["status"])


def _active_interview(journey):
    return journey.interviews.filter(status=Interview.SCHEDULED).select_related("slot", "interviewer").first()


def slots_for_day(interviewer, day, windows=None):
    """Unsaved open slots for one IST day from an interviewer's weekly pattern."""
    wins = windows if windows is not None else (interviewer.weekly_pattern or {}).get(str(ist_weekday(day + 12 * HOUR)), [])
    out = []
    for sh, sm, eh, em in wins:
        t, end = day + timedelta(hours=sh, minutes=sm), day + timedelta(hours=eh, minutes=em)
        while t + SLOT <= end:
            out.append(AvailabilitySlot(interviewer=interviewer, start_at=t, end_at=t + SLOT))
            t += SLOT
    return out


# ---------------------------------------------------------------- scheduled jobs
def _system_work_pending(now):
    """Cheap read-only check so the job only takes the write lock when there's something to do."""
    horizon = day_start(now) + (CONFIG["BOOK_WINDOW_DAYS"] + 1) * DAY
    return (
        Interview.objects.filter(status=Interview.SCHEDULED, end_at__lte=now, journey__state=M.SCHEDULED).exists()
        or Journey.objects.filter(state__in=M.COOLDOWN_STATES, cooldown_until__lte=now).exists()
        or AvailabilitySlot.objects.filter(status=AvailabilitySlot.OPEN, start_at__lt=now).exists()
        or Interviewer.objects.filter(active=True).exclude(weekly_pattern={}).filter(Q(materialized_until__isnull=True) | Q(materialized_until__lt=horizon)).exists()
        or AvailabilitySlot.objects.filter(status=AvailabilitySlot.EXPIRED, start_at__lt=now - 7 * DAY, interviews__isnull=True).exists()
    )


def run_system(now=None):
    """Time-driven transitions + housekeeping. Idempotent; safe to call on every request or from cron."""
    now = now or clock_now()
    if not _system_work_pending(now):
        return
    with transaction.atomic():
        for iv in Interview.objects.filter(status=Interview.SCHEDULED, end_at__lte=now, journey__state=M.SCHEDULED).select_related("journey"):
            _transition(iv.journey, "SLOT_ENDED", "System", f"Interview at {fmt_time(iv.start_at)} ended; outcome pending", now)

        for j in Journey.objects.filter(state__in=M.COOLDOWN_STATES, cooldown_until__lte=now).select_related("student", "badge"):
            _transition(j, "COOLDOWN_ENDED", "System", "Cooldown complete", now)
            j.cooldown_until, j.eligible_at = None, now
            j.save(update_fields=["cooldown_until", "eligible_at"])
            _notify(j.student, "COOLDOWN_ENDED", {"badge": badge_dict(j.badge), "left": j.max_attempts - j.attempts_used}, now, j)

        AvailabilitySlot.objects.filter(status=AvailabilitySlot.OPEN, start_at__lt=now).update(status=AvailabilitySlot.EXPIRED)

        # Roll each interviewer's weekly pattern forward so the booking window is always populated.
        # Only never-materialised days are generated, so one-off removals and blocked days stick.
        horizon = day_start(now) + (CONFIG["BOOK_WINDOW_DAYS"] + 1) * DAY
        for iv in Interviewer.objects.filter(active=True):
            if not iv.weekly_pattern:
                continue
            cursor = max(iv.materialized_until or day_start(now), day_start(now))
            if cursor >= horizon:
                continue
            existing = set(AvailabilitySlot.objects.filter(interviewer=iv, start_at__gte=cursor, start_at__lt=horizon).values_list("start_at", flat=True))
            new = []
            while cursor < horizon:
                new += [s for s in slots_for_day(iv, cursor) if s.start_at not in existing]
                cursor += DAY
            AvailabilitySlot.objects.bulk_create(new, ignore_conflicts=True)
            iv.materialized_until = cursor
            iv.save(update_fields=["materialized_until"])

        # Keep storage small: drop old, never-booked expired slots.
        AvailabilitySlot.objects.filter(status=AvailabilitySlot.EXPIRED, start_at__lt=now - 7 * DAY, interviews__isnull=True).delete()


# ---------------------------------------------------------------- student actions
def _own_journey(user, journey_id, lock=True):
    student = student_of(user)
    if not student:
        raise Forbidden()
    qs = Journey.objects.select_related("badge", "student")
    if lock:
        qs = qs.select_for_update()
    j = qs.filter(pk=journey_id, student=student).first()
    if not j:
        raise Forbidden()
    return j


@transaction.atomic
def book(user, journey_id, start):
    now = clock_now()
    j = _own_journey(user, journey_id)
    if j.state != M.ELIGIBLE:
        raise DomainError("You can't book right now — check your dashboard for your current status.")
    if start < now + _lead(j):
        raise DomainError(f"Slots must be at least {int(_lead(j) / HOUR)} hours away. Please pick a later time.")
    if start > now + CONFIG["BOOK_WINDOW_DAYS"] * DAY:
        raise DomainError(f"You can book up to {CONFIG['BOOK_WINDOW_DAYS']} days ahead.")
    if start + SLOT > deadline(now):
        raise DomainError("That slot is after the badge deadline.")
    if Interview.objects.filter(journey__student=j.student, status=Interview.SCHEDULED, start_at__gt=start - SLOT, start_at__lt=start + SLOT).exists():
        raise DomainError("You already have another interview at that time.")

    for slot in candidates(j, start):
        # Lock the slot row and re-check it's still open (another student may have just taken it).
        locked = AvailabilitySlot.objects.select_for_update().filter(pk=slot.pk, status=AvailabilitySlot.OPEN).first()
        if not locked:
            continue
        try:
            with transaction.atomic():
                locked.status = AvailabilitySlot.BOOKED
                locked.save(update_fields=["status"])
                iv = Interview.objects.create(
                    journey=j, slot=locked, interviewer=slot.interviewer, start_at=locked.start_at, end_at=locked.end_at,
                    attempt_no=j.attempts_used + 1, booked_at=now, meet_link=f"https://meet.grit.example/{secrets.token_urlsafe(6)}")
        except IntegrityError:  # unique constraint caught a race — try the next interviewer
            continue
        j.priority = False
        _transition(j, "BOOK", "Student", f"{fmt_date(start)} {fmt_time(start)} with {slot.interviewer.name}", now)
        _notify(j.student, "BOOKED", {"badge": badge_dict(j.badge), "start": to_ms(start), "interviewer": slot.interviewer.name}, now, j)
        return iv
    raise DomainError("That time was just taken by another student. The list has been refreshed — please pick another slot.")


def _changeable(user, interview_id, now):
    student = student_of(user)
    iv = Interview.objects.select_for_update().select_related("journey__badge", "journey__student", "slot").filter(pk=interview_id).first()
    if not student or not iv or iv.journey.student_id != student.pk:
        raise Forbidden()
    if iv.status != Interview.SCHEDULED:
        raise DomainError("This interview can no longer be changed.")
    if now > iv.start_at - CONFIG["CHANGE_CUTOFF_H"] * HOUR:
        raise DomainError(f"Changes close {CONFIG['CHANGE_CUTOFF_H']} hours before the interview. If it's an emergency, contact your campus GRIT team.")
    return iv


@transaction.atomic
def reschedule(user, interview_id, new_start):
    now = clock_now()
    iv = _changeable(user, interview_id, now)
    j = iv.journey
    if iv.reschedule_count >= CONFIG["RESCHEDULE_LIMIT"]:
        raise DomainError("You've used both reschedules for this booking. You can still cancel and book again.")
    if new_start == iv.start_at:
        raise DomainError("That is your current slot.")
    if new_start < now + _lead(j):
        raise DomainError(f"New slot must be at least {CONFIG['BOOK_MIN_LEAD_H']} hours away.")
    if new_start > now + CONFIG["BOOK_WINDOW_DAYS"] * DAY or new_start + SLOT > deadline(now):
        raise DomainError("That slot is outside the booking window.")
    for slot in candidates(j, new_start, ignore_interview_id=iv.pk):
        locked = AvailabilitySlot.objects.select_for_update().filter(pk=slot.pk, status=AvailabilitySlot.OPEN).first()
        if not locked:
            continue
        old = iv.start_at
        _release_slot(iv, now)
        locked.status = AvailabilitySlot.BOOKED
        locked.save(update_fields=["status"])
        iv.slot, iv.interviewer = locked, slot.interviewer
        iv.start_at, iv.end_at = locked.start_at, locked.end_at
        iv.reschedule_count += 1
        iv.save()
        _transition(j, "RESCHEDULE", "Student", f"{fmt_date(old)} {fmt_time(old)} → {fmt_date(new_start)} {fmt_time(new_start)}", now)
        _notify(j.student, "RESCHEDULED", {"badge": badge_dict(j.badge), "start": to_ms(new_start), "interviewer": slot.interviewer.name,
                                           "left": CONFIG["RESCHEDULE_LIMIT"] - iv.reschedule_count}, now, j)
        return iv
    raise DomainError("That time was just taken. Please pick another slot.")


@transaction.atomic
def cancel(user, interview_id):
    now = clock_now()
    iv = _changeable(user, interview_id, now)
    j = iv.journey
    _release_slot(iv, now)
    iv.status, iv.cancel_reason = Interview.CANCELLED, "Cancelled by student"
    iv.save(update_fields=["status", "cancel_reason"])
    _transition(j, "CANCEL", "Student", "Cancelled by student; slot released", now)
    _notify(j.student, "CANCELLED", {"start": to_ms(iv.start_at)}, now, j)


@transaction.atomic
def clear_skill(student, key, level):
    """A skill contest is cleared (normally pushed by the contest system; a demo button here)."""
    if key not in SKILLS or not 1 <= int(level) <= 4:
        raise DomainError("Unknown skill or level.")
    now = clock_now()
    SkillClearance.objects.update_or_create(student=student, skill=key, defaults={"level": int(level), "cleared_at": now, "status": "cleared"})
    for j in Journey.objects.select_for_update().filter(student=student, state=M.LOCKED).select_related("badge"):
        if all(p["ok"] for p in skill_progress(student, j.badge)):
            _transition(j, "SKILLS_CLEARED", "System", f"{SKILLS[key]} L{level} cleared", now)
            j.eligible_at = now
            j.save(update_fields=["eligible_at"])
            _notify(student, "ELIGIBLE", {"badge": badge_dict(j.badge)}, now, j)


def mark_read(student):
    Notification.objects.filter(student=student, read_at__isnull=True).update(read_at=clock_now())


# ---------------------------------------------------------------- interviewer actions
@transaction.atomic
def record_outcome(user, interview_id, attendance, result=None, scores=None, feedback="", notes=""):
    now = clock_now()
    me = interviewer_of(user)
    iv = Interview.objects.select_for_update().select_related("journey__badge", "journey__student", "interviewer").filter(pk=interview_id).first()
    if not iv or not me or iv.interviewer_id != me.pk:
        raise Forbidden()
    if iv.status != Interview.SCHEDULED or InterviewOutcome.objects.filter(interview=iv).exists():
        raise DomainError("An outcome is already recorded for this interview.")
    if now < iv.start_at:
        raise DomainError(f"You can record the outcome once the interview starts ({fmt_time(iv.start_at)}).")
    j = Journey.objects.select_for_update().select_related("badge", "student").get(pk=iv.journey_id)
    b, student, actor = badge_dict(j.badge), j.student, f"Interviewer · {me.name}"
    feedback, notes = (feedback or "").strip(), (notes or "").strip()
    cooldown_end = lambda kind: day_start(now) + (CONFIG["COOLDOWN_DAYS"][kind] + 1) * DAY  # unlocks at midnight IST  # noqa: E731

    def outcome(res, **kw):
        InterviewOutcome.objects.create(interview=iv, result=res, recorded_by=me, recorded_by_name=me.name, recorded_at=now, internal_notes=notes, **kw)

    if attendance == "no_show":
        if now < iv.start_at + CONFIG["NO_SHOW_GRACE_MIN"] * MIN:
            raise DomainError(f"Wait until {CONFIG['NO_SHOW_GRACE_MIN']} minutes after start before marking a no-show.")
        iv.status = Interview.NO_SHOW
        iv.save(update_fields=["status"])
        outcome("no_show")
        j.no_shows += 1
        counted = j.no_shows >= 2
        if counted:
            j.attempts_used += 1
        j.cooldown_until = cooldown_end("NO_SHOW")
        j.save()
        _transition(j, "RECORD_NO_SHOW", actor, "2nd no-show — attempt used" if counted else "1st no-show — no attempt used", now)
        _notify(student, "NO_SHOW", {"badge": b, "start": to_ms(iv.start_at), "until": to_ms(j.cooldown_until), "counted": counted}, now, j)
        _exhaust_check(j, now)
        return

    if attendance == "tech_issue":
        iv.status = Interview.TECH_ISSUE
        iv.save(update_fields=["status"])
        outcome("tech_issue")
        j.priority, j.eligible_at = True, now
        j.save()
        _transition(j, "RECORD_TECH_ISSUE", actor, "Technical issue — no attempt used, priority rebooking", now)
        _notify(student, "TECH_ISSUE", {}, now, j)
        return

    if attendance != "attended":
        raise DomainError("Tell us whether the interview happened.")
    if result not in ("pass", "retake", "fail"):
        raise DomainError("Choose an outcome: Pass, Retake later or Fail.")
    scores = scores or {}
    missing = [k for k in RUBRIC_KEYS if not (isinstance(scores.get(k), int) and 1 <= scores[k] <= 4)]
    if missing:
        raise DomainError("Score every rubric area from 1 to 4.")
    if result != "pass" and len(feedback) < CONFIG["MIN_FEEDBACK_CHARS"]:
        raise DomainError(f"Write at least {CONFIG['MIN_FEEDBACK_CHARS']} characters of feedback for the student — they need to know what to work on.")

    iv.status = Interview.COMPLETED
    iv.save(update_fields=["status"])
    outcome(result, scores=scores, feedback_to_student=feedback)
    j.attempts_used += 1
    j.save()

    if result == "pass":
        j.awarded_at = now
        j.save(update_fields=["awarded_at"])
        _transition(j, "RECORD_PASS", actor, f"Badge awarded (+{b['miles']} Miles)", now)
        payload = {"badge": b}
        nb = j.badge.next_badge
        if nb and not Journey.objects.filter(student=student, badge=nb).exists():
            prog = skill_progress(student, nb)
            nj = Journey.objects.create(student=student, badge=nb, state=M.LOCKED, created_at=now, updated_at=now - timedelta(seconds=1))
            _audit(nj, "JOURNEY_OPENED", "—", M.LOCKED, "System", f"{nb.level} unlocked as next badge", now)
            payload.update(next_badge=badge_dict(nb), next_have=sum(p["ok"] for p in prog))
        from .selectors import rank_of  # local import: selectors imports services
        payload["rank"] = rank_of(student)["rank"]
        _notify(student, "PASSED", payload, now, j)
        return

    kind = "RETAKE" if result == "retake" else "FAILED"
    j.cooldown_until = cooldown_end(kind)
    j.save(update_fields=["cooldown_until"])
    _transition(j, "RECORD_RETAKE" if kind == "RETAKE" else "RECORD_FAIL", actor,
                f"Attempt {j.attempts_used}/{j.max_attempts}; rebook from {fmt_date(j.cooldown_until)}", now)
    if j.cooldown_until > deadline(now):
        _audit(j, "DEADLINE_RISK", j.state, j.state, "System", "Cooldown ends after the badge deadline — needs an ops decision", now)
    if j.attempts_used < j.max_attempts:
        _notify(student, kind, {"badge": b, "feedback": feedback, "until": to_ms(j.cooldown_until), "used": j.attempts_used, "max": j.max_attempts}, now, j)
    _exhaust_check(j, now)


def _exhaust_check(j, now):
    if j.state != M.PASSED and j.attempts_used >= j.max_attempts:
        _transition(j, "ATTEMPTS_EXHAUSTED", "System", f"{j.attempts_used} attempts used", now)
        j.cooldown_until = None
        j.save(update_fields=["cooldown_until"])
        _notify(j.student, "CLOSED", {"badge": badge_dict(j.badge), "max": j.max_attempts}, now, j)


def _my_interviewer(user):
    me = interviewer_of(user)
    if not me:
        raise Forbidden()
    return me


@transaction.atomic
def toggle_slot(user, start):
    me = _my_interviewer(user)
    if start <= clock_now():
        raise DomainError("You can't change availability in the past.")
    slot = AvailabilitySlot.objects.select_for_update().filter(interviewer=me, start_at=start).first()
    if slot and slot.status == AvailabilitySlot.BOOKED:
        raise DomainError("A student is booked in this slot. Use “Can't make it” on the booking — we'll try to reassign it first.")
    if slot:
        slot.delete()
        return False
    AvailabilitySlot.objects.create(interviewer=me, start_at=start, end_at=start + SLOT)
    return True


@transaction.atomic
def add_window(user, day, from_min, to_min):
    me = _my_interviewer(user)
    if to_min <= from_min:
        raise DomainError("End time must be after start time.")
    now = clock_now()
    new = [s for s in slots_for_day(me, day, [[from_min // 60, from_min % 60, to_min // 60, to_min % 60]]) if s.start_at > now]
    existing = set(AvailabilitySlot.objects.filter(interviewer=me, start_at__in=[s.start_at for s in new]).values_list("start_at", flat=True))
    new = [s for s in new if s.start_at not in existing]
    AvailabilitySlot.objects.bulk_create(new)
    return len(new)


def _cancel_by_team(iv, reason, actor, now):
    """Hand the booking to a free colleague at the same time; otherwise cancel with priority rebooking."""
    j = Journey.objects.select_for_update().select_related("badge", "student").get(pk=iv.journey_id)
    from_name = iv.interviewer.name
    alt = next((s for s in candidates(j, iv.start_at) if s.interviewer_id != iv.interviewer_id), None)
    old_slot = iv.slot
    if alt:
        alt.status = AvailabilitySlot.BOOKED
        alt.save(update_fields=["status"])
        iv.slot, iv.interviewer = alt, alt.interviewer
        iv.save(update_fields=["slot", "interviewer"])
        if old_slot:
            old_slot.delete()  # the interviewer withdrew this slot
        _audit(j, "REASSIGNED", j.state, j.state, actor, f"{from_name} → {alt.interviewer.name} ({reason})", now)
        _notify(j.student, "REASSIGNED", {"start": to_ms(iv.start_at), "interviewer": alt.interviewer.name}, now, j)
        return "reassigned"
    iv.status, iv.cancel_reason, iv.slot = Interview.CANCELLED, reason, None
    iv.save(update_fields=["status", "cancel_reason", "slot"])
    if old_slot:
        old_slot.delete()
    j.priority, j.eligible_at = True, now
    j.save(update_fields=["priority", "eligible_at"])
    _transition(j, "CANCEL_BY_TEAM", actor, f"{reason}; no colleague free — priority rebooking", now)
    _notify(j.student, "CANCELLED_BY_TEAM", {"start": to_ms(iv.start_at), "reason": reason}, now, j)
    return "cancelled"


REASONS = ["Interviewer unavailable", "Interviewer unwell", "Scheduling conflict"]


@transaction.atomic
def release_interview(user, interview_id, reason):
    now = clock_now()
    role = role_of(user)
    iv = Interview.objects.select_for_update().select_related("interviewer", "slot").filter(pk=interview_id).first()
    if not iv or not (role == "ops" or (role == "interviewer" and interviewer_of(user).pk == iv.interviewer_id)):
        raise Forbidden()
    if iv.status != Interview.SCHEDULED or iv.start_at <= now:
        raise DomainError("This booking can no longer be released.")
    if reason not in REASONS:
        raise DomainError("Pick a reason.")
    actor = "Ops" if role == "ops" else f"Interviewer · {iv.interviewer.name}"
    return _cancel_by_team(iv, reason, actor, now)


@transaction.atomic
def block_day(user, day):
    me = _my_interviewer(user)
    now = clock_now()
    res = {"removed": 0, "reassigned": 0, "cancelled": 0}
    res["removed"], _ = AvailabilitySlot.objects.filter(interviewer=me, status=AvailabilitySlot.OPEN, start_at__gte=max(day, now), start_at__lt=day + DAY).delete()
    for iv in Interview.objects.select_for_update().select_related("interviewer", "slot").filter(
            interviewer=me, status=Interview.SCHEDULED, start_at__gte=max(day, now), start_at__lt=day + DAY):
        res[_cancel_by_team(iv, "Interviewer unavailable", f"Interviewer · {me.name}", now)] += 1
    return res


# ---------------------------------------------------------------- ops actions
@transaction.atomic
def raise_flag(user, journey_id, reason):
    _require_ops(user)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise DomainError("Describe the reason (at least 10 characters) — it is kept in the audit log.")
    now = clock_now()
    j = Journey.objects.select_for_update().select_related("badge", "student").filter(pk=journey_id).first()
    if not j:
        raise DomainError("Journey not found.")
    prev = j.state
    iv = _active_interview(j)
    _transition(j, "FLAG_RAISED", "Ops", reason, now)
    j.prev_state = prev
    j.save(update_fields=["prev_state"])
    if iv:
        _release_slot(iv, now)
        iv.status, iv.cancel_reason = Interview.CANCELLED, "Integrity review"
        iv.save(update_fields=["status", "cancel_reason"])
    IntegrityFlag.objects.create(journey=j, reason=reason, had_booking=bool(iv), raised_by=user, raised_at=now)
    _notify(j.student, "ON_HOLD", {"badge": badge_dict(j.badge)}, now, j)


@transaction.atomic
def resolve_flag(user, flag_id, decision):
    _require_ops(user)
    if decision not in ("confirmed", "dismissed"):
        raise DomainError("Unknown decision.")
    now = clock_now()
    f = IntegrityFlag.objects.select_for_update().filter(pk=flag_id).first()
    if not f or f.status != "open":
        raise DomainError("This flag is already resolved.")
    j = Journey.objects.select_for_update().select_related("badge", "student").get(pk=f.journey_id)
    b = badge_dict(j.badge)
    f.status, f.resolved_at, f.resolved_by = decision, now, user
    f.save()
    if decision == "confirmed":
        had_badge = j.prev_state == M.PASSED
        _transition(j, "FLAG_CONFIRMED", "Ops", f"Badge nullified (−{b['miles']} Miles)" if had_badge else "Disqualified for this badge", now)
        j.revoked_at = now
        j.save(update_fields=["revoked_at"])
        _notify(j.student, "REVOKED", {"badge": b, "had_badge": had_badge}, now, j)
    else:
        _transition(j, "FLAG_DISMISSED", "Ops", "No malpractice found", now, prev=j.prev_state)
        if j.state == M.ELIGIBLE:
            j.eligible_at = now
            j.save(update_fields=["eligible_at"])
        _notify(j.student, "FLAG_CLEARED", {"badge": b, "was_booked": f.had_booking}, now, j)


@transaction.atomic
def grant_extra_attempt(user, journey_id):
    _require_ops(user)
    now = clock_now()
    j = Journey.objects.select_for_update().select_related("badge", "student").get(pk=journey_id)
    _transition(j, "EXTRA_ATTEMPT", "Ops", "Extra attempt granted", now)
    j.extra_attempts += 1
    j.eligible_at = now
    j.save(update_fields=["extra_attempts", "eligible_at"])
    _notify(j.student, "EXTRA_ATTEMPT", {"badge": badge_dict(j.badge)}, now, j)


@transaction.atomic
def nudge(user, journey_ids):
    _require_ops(user)
    now = clock_now()
    seats = open_seats(now, 7)
    n = 0
    for j in Journey.objects.select_for_update().select_related("badge", "student").filter(pk__in=journey_ids, state=M.ELIGIBLE):
        days = max(1, (now - (j.eligible_at or now)).days)
        _notify(j.student, "NUDGE", {"badge": badge_dict(j.badge), "days": days, "open": seats}, now, j)
        j.last_nudged_at = now
        j.save(update_fields=["last_nudged_at"])
        _audit(j, "NUDGE_SENT", j.state, j.state, "Ops", "Booking reminder sent", now)
        n += 1
    return n


# ---------------------------------------------------------------- demo controls
def advance_clock(seconds):
    clock, _ = DemoClock.objects.get_or_create(pk=1)
    clock.offset_seconds += seconds
    clock.save()
    run_system()


def interviews_stats():
    return dict(Interview.objects.values_list("status").annotate(n=Count("id")))
