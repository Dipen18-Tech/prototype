"""Data model — mirrors the write-up.

badge_journeys.state is the state machine; journey_events is its append-only history.
Double booking is impossible at the database level (partial unique constraints on Interview).
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from . import machine


class Badge(models.Model):
    code = models.CharField(max_length=20, primary_key=True)
    track = models.CharField(max_length=80)
    level = models.CharField(max_length=30)
    miles = models.PositiveIntegerField()
    required_skills = models.JSONField(help_text='[["CT", 1], ["UI", 2]] — skill key and minimum level')
    next_badge = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f"{self.track} · {self.level}"


class Student(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="student")
    code = models.CharField(max_length=20, unique=True)  # public id used by the API
    name = models.CharField(max_length=120)
    roll_no = models.CharField(max_length=30, unique=True)
    campus = models.CharField(max_length=60)
    batch = models.CharField(max_length=4)
    bonus_miles = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.name} ({self.roll_no})"


class SkillClearance(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="skills")
    skill = models.CharField(max_length=20)
    level = models.PositiveSmallIntegerField()
    cleared_at = models.DateTimeField()
    status = models.CharField(max_length=10, default="cleared", choices=[("cleared", "Cleared"), ("revoked", "Revoked")])

    class Meta:
        constraints = [models.UniqueConstraint(fields=["student", "skill"], name="one_clearance_per_skill")]


class Interviewer(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="interviewer")
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    title = models.CharField(max_length=120, blank=True)
    tracks = models.JSONField(default=list)
    max_per_day = models.PositiveSmallIntegerField(default=5)
    weekly_pattern = models.JSONField(default=dict, blank=True, help_text='{"1": [[9,30,13,0]]} — JS weekday → IST windows')
    materialized_until = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class AvailabilitySlot(models.Model):
    OPEN, BOOKED, EXPIRED = "open", "booked", "expired"
    interviewer = models.ForeignKey(Interviewer, on_delete=models.CASCADE, related_name="slots")
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()
    status = models.CharField(max_length=10, default=OPEN, choices=[(OPEN, "Open"), (BOOKED, "Booked"), (EXPIRED, "Expired")])

    class Meta:
        constraints = [models.UniqueConstraint(fields=["interviewer", "start_at"], name="one_slot_per_interviewer_time")]
        indexes = [models.Index(fields=["status", "start_at"])]


class Journey(models.Model):
    """One per student × badge. `state` is the state machine."""
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="journeys")
    badge = models.ForeignKey(Badge, on_delete=models.PROTECT)
    state = models.CharField(max_length=20, choices=[(s, s) for s in machine.STATES], db_index=True)
    prev_state = models.CharField(max_length=20, blank=True, default="")
    eligible_at = models.DateTimeField(null=True, blank=True)
    attempts_used = models.PositiveSmallIntegerField(default=0)
    extra_attempts = models.PositiveSmallIntegerField(default=0)
    no_shows = models.PositiveSmallIntegerField(default=0)
    cooldown_until = models.DateTimeField(null=True, blank=True)
    priority = models.BooleanField(default=False)
    awarded_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_nudged_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=0)  # bumped on every transition (optimistic-locking hook)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["student", "badge"], name="one_journey_per_badge"),
            models.CheckConstraint(condition=Q(state__in=machine.STATES), name="journey_state_valid"),
        ]

    @property
    def max_attempts(self):
        from .utils import CONFIG

        return CONFIG["MAX_ATTEMPTS"] + self.extra_attempts

    def __str__(self):
        return f"{self.student.name} — {self.badge_id} [{self.state}]"


class Interview(models.Model):
    SCHEDULED, COMPLETED, NO_SHOW, TECH_ISSUE, CANCELLED = "scheduled", "completed", "no_show", "tech_issue", "cancelled"
    journey = models.ForeignKey(Journey, on_delete=models.CASCADE, related_name="interviews")
    slot = models.ForeignKey(AvailabilitySlot, null=True, blank=True, on_delete=models.SET_NULL, related_name="interviews")
    interviewer = models.ForeignKey(Interviewer, on_delete=models.PROTECT, related_name="interviews")
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()
    attempt_no = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=12, default=SCHEDULED, choices=[(s, s) for s in (SCHEDULED, COMPLETED, NO_SHOW, TECH_ISSUE, CANCELLED)])
    reschedule_count = models.PositiveSmallIntegerField(default=0)
    cancel_reason = models.CharField(max_length=200, blank=True)
    meet_link = models.URLField(blank=True)
    booked_at = models.DateTimeField()

    class Meta:
        constraints = [
            # The database itself refuses a second live booking for the same slot or the same badge.
            models.UniqueConstraint(fields=["slot"], condition=Q(status="scheduled"), name="no_double_booking"),
            models.UniqueConstraint(fields=["journey"], condition=Q(status="scheduled"), name="one_live_booking_per_journey"),
        ]


class InterviewOutcome(models.Model):
    interview = models.OneToOneField(Interview, primary_key=True, on_delete=models.CASCADE, related_name="outcome")
    result = models.CharField(max_length=12, choices=[(r, r) for r in ("pass", "retake", "fail", "no_show", "tech_issue")])
    scores = models.JSONField(null=True, blank=True)
    feedback_to_student = models.TextField(blank=True)
    internal_notes = models.TextField(blank=True)  # never sent to students
    recorded_by = models.ForeignKey(Interviewer, null=True, on_delete=models.SET_NULL)
    recorded_by_name = models.CharField(max_length=120)
    recorded_at = models.DateTimeField()


class IntegrityFlag(models.Model):
    journey = models.ForeignKey(Journey, on_delete=models.CASCADE, related_name="flags")
    reason = models.TextField()  # internal; not shown to the student
    status = models.CharField(max_length=10, default="open", choices=[(s, s) for s in ("open", "confirmed", "dismissed")])
    had_booking = models.BooleanField(default=False)
    raised_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    raised_at = models.DateTimeField()
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)


class Notification(models.Model):
    """Stored as template key + payload; the copy lives in one place (messages.js)."""
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="notifications")
    journey = models.ForeignKey(Journey, null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=30)
    payload = models.JSONField(default=dict)
    channel = models.CharField(max_length=10, default="in_app")
    created_at = models.DateTimeField(db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)


class JourneyEvent(models.Model):
    """Append-only audit log of every state change and notable action."""
    journey = models.ForeignKey(Journey, null=True, blank=True, on_delete=models.SET_NULL, related_name="events")
    student = models.ForeignKey(Student, null=True, blank=True, on_delete=models.SET_NULL)
    event = models.CharField(max_length=30)
    from_state = models.CharField(max_length=20)
    to_state = models.CharField(max_length=20)
    actor = models.CharField(max_length=120)
    detail = models.TextField(blank=True)
    created_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class DemoClock(models.Model):
    """Singleton offset applied to 'now' in demo mode (lets reviewers fast-forward time)."""
    offset_seconds = models.FloatField(default=0)
