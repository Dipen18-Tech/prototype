"""Demo data: every journey state has at least one student, so every screen can be demoed.

The simulated clock starts at 11:50 AM IST today, so the interviewer's 12:00 interview is joinable right away.
"""
import os
import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction

from . import machine as M
from .messages import badge_dict
from .models import (AvailabilitySlot, Badge, DemoClock, IntegrityFlag, Interview, InterviewOutcome, Interviewer, Journey,
                     JourneyEvent, Notification, SkillClearance, Student)
from .utils import BADGES, CONFIG, DAY, HOUR, MIN, day_start, fmt_date, fmt_time, real_now, to_ms
from .services import run_system, slots_for_day

DEMO_PASSWORD = os.environ.get("GRIT_DEMO_PASSWORD", "grit-demo-2026")
SLOT = timedelta(minutes=CONFIG["SLOT_MIN"])

INTERVIEWERS = [
    ("i1", "priya", "Priya Raman", "Senior Mentor · AI Products", ["AI Product Mastery", "AI Systems Mastery"], 6,
     {"1": [[9, 30, 13, 0], [14, 30, 17, 30]], "2": [[9, 30, 13, 0]], "3": [[9, 30, 13, 0], [14, 30, 17, 30]], "4": [[14, 30, 17, 30]], "5": [[9, 30, 13, 0]]}),
    ("i2", "vikram", "Vikram Sethi", "Mentor · Full-stack", ["AI Product Mastery"], 5,
     {"1": [[15, 0, 18, 0]], "3": [[15, 0, 18, 0]], "5": [[15, 0, 18, 0]], "6": [[10, 0, 12, 0]]}),
    ("i3", "ananya", "Ananya Iyer", "Mentor · Backend & Data", ["AI Systems Mastery", "AI Product Mastery"], 4,
     {"2": [[10, 0, 12, 0], [16, 0, 18, 0]], "4": [[10, 0, 12, 0]], "6": [[11, 0, 13, 0]]}),
    ("i4", "rohit", "Rohit Menon", "Mentor · Frontend", ["AI Product Mastery"], 4,
     {"1": [[18, 0, 20, 0]], "2": [[18, 0, 20, 0]], "3": [[18, 0, 20, 0]], "4": [[18, 0, 20, 0]]}),
]
FULL_APM = {"CT": 1, "CSF": 1, "GENAI": 1, "UI": 2}
FULL_ASM = {"CT": 1, "CSF": 1, "SSE": 1, "SQL": 1}


def demo_usernames():
    return [u for _, u, *_ in INTERVIEWERS] + ["ops"] + [s.user.username for s in Student.objects.select_related("user") if s.user]


@transaction.atomic
def reset_demo():
    """Wipe demo data and recreate it. Never touches users outside the demo set."""
    User = get_user_model()
    for model in (JourneyEvent, Notification, IntegrityFlag, InterviewOutcome, Interview, AvailabilitySlot, Journey, SkillClearance):
        model.objects.all().delete()
    demo_users = list(User.objects.filter(student__isnull=False)) + list(User.objects.filter(interviewer__isnull=False)) + list(User.objects.filter(username="ops"))
    Student.objects.all().delete()
    Interviewer.objects.all().delete()
    for u in demo_users:
        u.delete()
    Badge.objects.all().delete()
    seed()


def seed():
    User = get_user_model()
    rnd = random.Random(42)
    today = day_start(real_now())
    T = today + 11 * HOUR + 50 * MIN
    DemoClock.objects.update_or_create(pk=1, defaults={"offset_seconds": (T - real_now()).total_seconds()})
    at = lambda d, h, m=0: today + d * DAY + timedelta(hours=h, minutes=m)  # noqa: E731

    # badges
    for b in BADGES:
        Badge.objects.create(code=b["code"], track=b["track"], level=b["level"], miles=b["miles"], required_skills=b["req"])
    for b in BADGES:
        if b["next"]:
            Badge.objects.filter(code=b["code"]).update(next_badge_id=b["next"])
    badges = {b.code: b for b in Badge.objects.all()}

    def mkuser(username, staff=False):
        u = User.objects.create_user(username=username, password=DEMO_PASSWORD, is_staff=staff, is_superuser=staff)
        return u

    ivs = {}
    for code, uname, name, title, tracks, cap, pattern in INTERVIEWERS:
        ivs[code] = Interviewer.objects.create(user=mkuser(uname), code=code, name=name, title=title, tracks=tracks, max_per_day=cap, weekly_pattern=pattern)
    mkuser("ops", staff=True)

    def log(t, actor, j, event, frm, to, detail=""):
        JourneyEvent.objects.create(journey=j, student_id=j.student_id, event=event, from_state=frm, to_state=to, actor=actor, detail=detail, created_at=t)

    def note(student, kind, payload, t, read=True, j=None):
        Notification.objects.create(student=student, journey=j, kind=kind, created_at=t, read_at=t if read else None,
                                    payload={"first": student.name.split()[0], **payload})

    def add_student(code, name, campus, batch, skills, bonus=0):
        s = Student.objects.create(user=mkuser(name.split()[0].lower()), code=code, name=name, campus=campus, batch=batch, bonus_miles=bonus,
                                   roll_no=f"NIAT{batch[2:]}{campus[:3].upper()}{100 + Student.objects.count() * 7:04d}")
        for k, lvl in skills.items():
            SkillClearance.objects.create(student=s, skill=k, level=lvl, cleared_at=T - 5 * DAY - rnd.randint(0, 40) * DAY)
        return s

    def add_journey(s, badge, state, eligible_at=None, **kw):
        j = Journey.objects.create(student=s, badge=badges[badge], state=state, eligible_at=eligible_at, created_at=T - 30 * DAY,
                                   updated_at=kw.pop("updated_at", T - DAY), **kw)
        if eligible_at:
            log(eligible_at, "System", j, "SKILLS_CLEARED", M.LOCKED, M.ELIGIBLE, "All required skills cleared")
            note(s, "ELIGIBLE", {"badge": badge_dict(j.badge)}, eligible_at, j=j)
        return j

    def add_interview(j, iid, start, status=Interview.SCHEDULED, outcome=None, booked_at=None, attempt_no=None):
        iv_ = ivs[iid]
        slot = AvailabilitySlot.objects.create(interviewer=iv_, start_at=start, end_at=start + SLOT, status=AvailabilitySlot.BOOKED)
        booked_at = booked_at or start - 3 * DAY
        iv = Interview.objects.create(journey=j, slot=slot, interviewer=iv_, start_at=start, end_at=start + SLOT, status=status,
                                      attempt_no=attempt_no or j.attempts_used + 1, booked_at=booked_at,
                                      meet_link=f"https://meet.grit.example/{rnd.randint(10**7, 10**8)}")
        log(booked_at, "Student", j, "BOOK", M.ELIGIBLE, M.SCHEDULED, f"{fmt_date(start)} {fmt_time(start)} with {iv_.name}")
        note(j.student, "BOOKED", {"badge": badge_dict(j.badge), "start": to_ms(start), "interviewer": iv_.name}, booked_at, j=j)
        if outcome:
            res, scores, fb, notes_, when = outcome
            InterviewOutcome.objects.create(interview=iv, result=res, scores=scores, feedback_to_student=fb, internal_notes=notes_,
                                            recorded_by=iv_, recorded_by_name=iv_.name, recorded_at=when)
        return iv

    sc = lambda a, b, c, d: {"problem": a, "concepts": b, "build": c, "comm": d}  # noqa: E731

    # 1. Eligible, not booked → booking demo
    s = add_student("s1", "Aarav Sharma", "Hyderabad", "2024", {**FULL_APM, "SQL": 1}, 150)
    add_journey(s, "APM-NOV", M.ELIGIBLE, T - 4 * DAY)

    # 2. Booked for today 12:00 → join window open at seed time
    s = add_student("s2", "Diya Patel", "Bengaluru", "2024", {**FULL_APM, "CT": 2})
    j = add_journey(s, "APM-NOV", M.SCHEDULED, T - 6 * DAY)
    add_interview(j, "i1", at(0, 12), booked_at=T - 3 * DAY)

    # 3. Locked: one skill short → "clear skill" demo
    s = add_student("s3", "Rahul Verma", "Pune", "2025", {"CT": 1, "CSF": 1, "GENAI": 1, "UI": 1})
    add_journey(s, "APM-NOV", M.LOCKED)

    # 4. Passed → Specialist opened
    s = add_student("s4", "Sneha Reddy", "Hyderabad", "2024", {"CT": 2, "CSF": 1, "GENAI": 2, "UI": 2}, 300)
    j = add_journey(s, "APM-NOV", M.PASSED, T - 14 * DAY, attempts_used=1, awarded_at=at(-5, 16, 25), updated_at=at(-5, 16, 25))
    add_interview(j, "i2", at(-5, 16), Interview.COMPLETED, ("pass", sc(4, 3, 4, 4), "Strong product sense — your RAG demo was well scoped and you explained the trade-offs clearly.", "Clear pass.", at(-5, 16, 25)), attempt_no=1)
    log(at(-5, 16, 25), "Interviewer · Vikram Sethi", j, "RECORD_PASS", M.SCHEDULED, M.PASSED, "+500 Miles")
    Journey.objects.create(student=s, badge=badges["APM-SPE"], state=M.LOCKED, created_at=at(-5, 16, 25), updated_at=at(-5, 16, 24))

    # 5. Retake later, cooldown running
    s = add_student("s5", "Karthik Nair", "Chennai", "2024", FULL_APM, 100)
    fb = "Good UI fundamentals, but you struggled to explain how your prompt pipeline handles bad model output. Practise adding validation and fallbacks to your Gen AI project."
    until = day_start(at(-3, 0)) + (CONFIG["COOLDOWN_DAYS"]["RETAKE"] + 1) * DAY
    j = add_journey(s, "APM-NOV", M.RETAKE, T - 9 * DAY, attempts_used=1, cooldown_until=until, updated_at=at(-3, 10, 40))
    add_interview(j, "i3", at(-3, 10), Interview.COMPLETED, ("retake", sc(3, 2, 3, 3), fb, "Close — re-check Gen AI error handling.", at(-3, 10, 40)), attempt_no=1)
    log(at(-3, 10, 40), "Interviewer · Ananya Iyer", j, "RECORD_RETAKE", M.SCHEDULED, M.RETAKE, "Attempt 1/3")
    note(s, "RETAKE", {"badge": badge_dict(j.badge), "feedback": fb, "until": to_ms(until), "used": 1, "max": 3}, at(-3, 10, 40), read=False, j=j)

    # 6 & 7. Interviewed this morning → awaiting outcome (the system job moves them)
    s = add_student("s6", "Meera Iyer", "Bengaluru", "2024", {**FULL_APM, "SSE": 1}, 50)
    j = add_journey(s, "APM-NOV", M.SCHEDULED, T - 8 * DAY)
    add_interview(j, "i1", at(0, 9, 30), booked_at=T - 5 * DAY)
    s = add_student("s7", "Arjun Rao", "Hyderabad", "2025", FULL_APM)
    j = add_journey(s, "APM-NOV", M.SCHEDULED, T - 7 * DAY)
    add_interview(j, "i1", at(0, 10, 30), booked_at=T - 2 * DAY)

    # 8. Revoked: passed, then malpractice confirmed
    s = add_student("s8", "Imran Khan", "Pune", "2024", FULL_APM, 200)
    j = add_journey(s, "APM-NOV", M.REVOKED, T - 30 * DAY, attempts_used=1, awarded_at=at(-20, 15, 30), revoked_at=at(-1, 17), prev_state=M.PASSED, updated_at=at(-1, 17))
    add_interview(j, "i2", at(-20, 15), Interview.COMPLETED, ("pass", sc(4, 3, 3, 3), "Solid build walkthrough.", "", at(-20, 15, 30)), attempt_no=1)
    reason = "UI Engineering L2 contest: submission matched a leaked solution"
    log(at(-20, 15, 30), "Interviewer · Vikram Sethi", j, "RECORD_PASS", M.SCHEDULED, M.PASSED, "+500 Miles")
    log(at(-4, 11), "Ops", j, "FLAG_RAISED", M.PASSED, M.ON_HOLD, reason)
    log(at(-1, 17), "Ops", j, "FLAG_CONFIRMED", M.ON_HOLD, M.REVOKED, "Badge nullified (−500 Miles)")
    IntegrityFlag.objects.create(journey=j, reason=reason, status="confirmed", raised_at=at(-4, 11), resolved_at=at(-1, 17))
    note(s, "PASSED", {"badge": badge_dict(j.badge), "rank": 9}, at(-20, 15, 30), j=j)
    note(s, "ON_HOLD", {"badge": badge_dict(j.badge)}, at(-4, 11), j=j)
    note(s, "REVOKED", {"badge": badge_dict(j.badge), "had_badge": True}, at(-1, 17), read=False, j=j)

    # 9. Booked in two days (reschedule / cancel demo)
    s = add_student("s9", "Fatima Sheikh", "Chennai", "2025", FULL_APM)
    j = add_journey(s, "APM-NOV", M.SCHEDULED, T - 5 * DAY)
    add_interview(j, "i2", at(2, 16), booked_at=T - DAY)

    # 10. Failed, long cooldown
    s = add_student("s10", "Kavya Menon", "Hyderabad", "2024", FULL_APM)
    fb = "You could not yet explain the basics of how your UI talks to an API, and the build task was left incomplete. Revisit UI Engineering L2 projects before your next attempt."
    until = day_start(at(-9, 0)) + (CONFIG["COOLDOWN_DAYS"]["FAILED"] + 1) * DAY
    j = add_journey(s, "APM-NOV", M.FAILED, T - 20 * DAY, attempts_used=1, cooldown_until=until, updated_at=at(-9, 19))
    add_interview(j, "i4", at(-9, 18, 30), Interview.COMPLETED, ("fail", sc(2, 1, 1, 2), fb, "", at(-9, 19)), attempt_no=1)
    log(at(-9, 19), "Interviewer · Rohit Menon", j, "RECORD_FAIL", M.SCHEDULED, M.FAILED, "Attempt 1/3")
    note(s, "FAILED", {"badge": badge_dict(j.badge), "feedback": fb, "until": to_ms(until), "used": 1, "max": 3}, at(-9, 19), j=j)

    # 11. No-show yesterday
    s = add_student("s11", "Rohan Gupta", "Bengaluru", "2025", FULL_APM)
    until = day_start(at(-1, 0)) + (CONFIG["COOLDOWN_DAYS"]["NO_SHOW"] + 1) * DAY
    j = add_journey(s, "APM-NOV", M.NO_SHOW, T - 6 * DAY, no_shows=1, cooldown_until=until, updated_at=at(-1, 18, 15))
    add_interview(j, "i4", at(-1, 18), Interview.NO_SHOW, ("no_show", None, "", "Waited 10 min, no response on call or WhatsApp.", at(-1, 18, 12)))
    log(at(-1, 18, 12), "Interviewer · Rohit Menon", j, "RECORD_NO_SHOW", M.SCHEDULED, M.NO_SHOW, "1st no-show — no attempt used")
    note(s, "NO_SHOW", {"badge": badge_dict(j.badge), "start": to_ms(at(-1, 18)), "until": to_ms(until), "counted": False}, at(-1, 18, 12), read=False, j=j)

    # 12. On hold — open integrity flag
    s = add_student("s12", "Ishita Das", "Pune", "2024", FULL_APM)
    j = add_journey(s, "APM-NOV", M.ON_HOLD, T - 10 * DAY, prev_state=M.ELIGIBLE, updated_at=at(-2, 14))
    reason = "Applied Gen AI L1: 94% similarity with another submission"
    log(at(-2, 14), "Ops", j, "FLAG_RAISED", M.ELIGIBLE, M.ON_HOLD, reason)
    IntegrityFlag.objects.create(journey=j, reason=reason, raised_at=at(-2, 14))
    note(s, "ON_HOLD", {"badge": badge_dict(j.badge)}, at(-2, 14), read=False, j=j)

    # Supporting cast
    s = add_student("s13", "Nikhil Joshi", "Chennai", "2024", FULL_ASM)
    add_journey(s, "ASM-NOV", M.ELIGIBLE, T - 9 * DAY)
    s = add_student("s14", "Pooja Kulkarni", "Pune", "2025", FULL_APM)
    add_journey(s, "APM-NOV", M.ELIGIBLE, T - DAY)
    s = add_student("s15", "Siddharth Bose", "Bengaluru", "2024", {**FULL_APM, "SQL": 2})
    add_journey(s, "APM-NOV", M.ELIGIBLE, T - 12 * DAY, last_nudged_at=T - 5 * DAY)
    s = add_student("s16", "Tanvi Shah", "Hyderabad", "2024", FULL_APM)
    j = add_journey(s, "APM-NOV", M.SCHEDULED, T - 4 * DAY)
    add_interview(j, "i1", at(0, 14, 30), booked_at=T - 2 * DAY)
    s = add_student("s17", "Varun Pillai", "Chennai", "2025", FULL_APM)
    j = add_journey(s, "APM-NOV", M.SCHEDULED, T - 3 * DAY)
    add_interview(j, "i1", at(0, 17), booked_at=T - DAY)
    s = add_student("s18", "Lakshmi Narayan", "Bengaluru", "2024", {**FULL_APM, "CT": 2, "UI": 3}, 400)
    j = add_journey(s, "APM-NOV", M.PASSED, T - 20 * DAY, attempts_used=1, awarded_at=at(-12, 10, 30), updated_at=at(-12, 10, 30))
    add_interview(j, "i3", at(-12, 10), Interview.COMPLETED, ("pass", sc(4, 4, 4, 3), "Excellent.", "", at(-12, 10, 30)), attempt_no=1)
    log(at(-12, 10, 30), "Interviewer · Ananya Iyer", j, "RECORD_PASS", M.SCHEDULED, M.PASSED, "+500 Miles")
    Journey.objects.create(student=s, badge=badges["APM-SPE"], state=M.LOCKED, created_at=at(-12, 10, 30), updated_at=at(-12, 10, 29))
    s = add_student("s19", "Aditya Kumar", "Hyderabad", "2024", FULL_ASM)
    j = add_journey(s, "ASM-NOV", M.SCHEDULED, T - 3 * DAY)
    add_interview(j, "i3", at(1, 11), booked_at=T - DAY)
    s = add_student("s20", "Neha Singh", "Pune", "2025", {"CT": 1, "CSF": 1})
    add_journey(s, "APM-NOV", M.LOCKED)
    s = add_student("s21", "Mohammed Faiz", "Chennai", "2024", FULL_APM)
    j = add_journey(s, "APM-NOV", M.ELIGIBLE, T - 6 * DAY, priority=True)
    add_interview(j, "i2", at(-2, 15, 30), Interview.TECH_ISSUE, ("tech_issue", None, "", "Student audio kept dropping; could not complete.", at(-2, 15, 50)))
    log(at(-2, 15, 50), "Interviewer · Vikram Sethi", j, "RECORD_TECH_ISSUE", M.SCHEDULED, M.ELIGIBLE, "No attempt used, priority rebooking")
    note(s, "TECH_ISSUE", {}, at(-2, 15, 50), read=False, j=j)
    s = add_student("s22", "Riya Chatterjee", "Hyderabad", "2024", FULL_APM)
    j = add_journey(s, "APM-NOV", M.CLOSED, T - 45 * DAY, attempts_used=3, updated_at=at(-2, 11))
    add_interview(j, "i1", at(-2, 10, 30), Interview.COMPLETED, ("fail", sc(2, 2, 1, 2), "Build task still incomplete; API integration concepts need more work.", "Third attempt.", at(-2, 11)), attempt_no=3)
    log(at(-2, 11), "System", j, "ATTEMPTS_EXHAUSTED", M.FAILED, M.CLOSED, "3 attempts used")
    note(s, "CLOSED", {"badge": badge_dict(j.badge), "max": 3}, at(-2, 11), j=j)
    s = add_student("s23", "Harsh Vardhan", "Bengaluru", "2024", {**FULL_ASM, "SSE": 2}, 250)
    j = add_journey(s, "ASM-NOV", M.PASSED, T - 10 * DAY, attempts_used=1, awarded_at=at(-3, 16, 30), updated_at=at(-3, 16, 30))
    add_interview(j, "i3", at(-3, 16), Interview.COMPLETED, ("pass", sc(3, 4, 3, 3), "Great SQL depth.", "", at(-3, 16, 30)), attempt_no=1)
    log(at(-3, 16, 30), "Interviewer · Ananya Iyer", j, "RECORD_PASS", M.SCHEDULED, M.PASSED, "+500 Miles")
    s = add_student("s24", "Sana Mirza", "Chennai", "2025", {"CT": 1, "CSF": 1, "SSE": 1})
    add_journey(s, "ASM-NOV", M.LOCKED)

    # Availability: weekly pattern for the booking window (today always a working day for Priya).
    for iv in ivs.values():
        existing = set(AvailabilitySlot.objects.filter(interviewer=iv).values_list("start_at", flat=True))
        new = []
        for d in range(CONFIG["BOOK_WINDOW_DAYS"] + 1):
            day = today + d * DAY
            override = iv.weekly_pattern["1"] if d == 0 and iv.code == "i1" else None
            new += [sl for sl in slots_for_day(iv, day, override) if sl.start_at not in existing]
        AvailabilitySlot.objects.bulk_create(new, ignore_conflicts=True)
        iv.materialized_until = today + (CONFIG["BOOK_WINDOW_DAYS"] + 1) * DAY
        iv.save(update_fields=["materialized_until"])

    run_system()
