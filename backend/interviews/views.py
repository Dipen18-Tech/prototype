"""Server-rendered, class-based views. Every write goes through services.py; views only parse input, call, and redirect.

Pattern for writes (Post/Redirect/Get):
    DomainError → friendly error message, back to the form
    Forbidden   → HTTP 403

Building blocks (mixins):
    RoleRequiredMixin  – must be signed in, and have one of `roles` (403 otherwise)
    DemoOnlyMixin      – 404 unless GRIT_DEMO_MODE is on
    ServiceCallMixin   – self.attempt(fn, ok_msg) turns DomainError into a message
    NavMixin           – passes `nav` (active tab) to the template
"""
import csv
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views import View
from django.views.generic import FormView, ListView, RedirectView, TemplateView

from . import machine as M
from . import messages as copy
from . import services as svc
from .forms import AddWindowForm, BookConfirmForm, FlagForm, OutcomeForm, ReleaseForm, RescheduleForm
from .models import AvailabilitySlot, Badge, IntegrityFlag, Interview, Interviewer, Journey, JourneyEvent, Notification, Student
from .selectors import badge_shelf, leaderboard, stepper, student_context, tracker_rows, weak_areas
from .utils import (CONFIG, DAY, HOUR, IST, MIN, RUBRIC, SKILLS, STATE_META, DomainError, Forbidden, day_start, fmt_date,
                    from_ms, to_ms)
from .utils import now as clock_now

DEMO_PASSWORD_HINT = "grit-demo-2026"


# =============================================================== mixins
class RoleRequiredMixin(LoginRequiredMixin):
    """Signed-in users only; additionally restricted to `roles` when set."""
    roles = ()

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()  # → login page
        if self.roles and svc.role_of(request.user) not in self.roles:
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class DemoOnlyMixin:
    """Demo helpers (persona login, clock, reset) disappear when GRIT_DEMO_MODE=0."""

    def dispatch(self, request, *args, **kwargs):
        if not settings.DEMO_MODE:
            raise Http404
        return super().dispatch(request, *args, **kwargs)


class ServiceCallMixin:
    def attempt(self, fn, ok_msg=None):
        """Run a service call; returns (ok, result). Domain errors become a message, not a crash."""
        try:
            result = fn()
        except DomainError as e:
            messages.error(self.request, str(e))
            return False, None
        except Forbidden:
            raise PermissionDenied
        if ok_msg:
            messages.success(self.request, ok_msg(result) if callable(ok_msg) else ok_msg)
        return True, result

    def safe_next(self, default):
        nxt = self.request.POST.get("next") or self.request.GET.get("next")
        return nxt if nxt and url_has_allowed_host_and_scheme(nxt, {self.request.get_host()}) else default


class NavMixin:
    nav = None

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.setdefault("nav", self.nav)
        return ctx


class StudentMixin(RoleRequiredMixin, ServiceCallMixin, NavMixin):
    roles = ("student",)

    @property
    def student(self):
        s = svc.student_of(self.request.user)
        if not s:
            raise PermissionDenied
        return s


class InterviewerMixin(RoleRequiredMixin, ServiceCallMixin, NavMixin):
    roles = ("interviewer",)

    @property
    def me(self):
        iv = svc.interviewer_of(self.request.user)
        if not iv:
            raise PermissionDenied
        return iv


class OpsMixin(RoleRequiredMixin, ServiceCallMixin, NavMixin):
    roles = ("ops",)


# =============================================================== auth & demo
HOME_FOR = {"student": "student_home", "interviewer": "iv_today", "ops": "ops_overview"}


class HomeView(View):
    """Send each role to its own landing page."""

    def get(self, request):
        if not request.user.is_authenticated:
            return redirect("login")
        role = svc.role_of(request.user)
        if role:
            return redirect(HOME_FOR[role])
        return TemplateView.as_view(template_name="interviews/no_role.html")(request)


class SignInView(LoginView):
    template_name = "interviews/login.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["demo_accounts"] = []
        if settings.DEMO_MODE:
            ctx["demo_accounts"] = [
                ("Students", [("aarav", "Aarav Sharma", "Eligible — book an interview"), ("diya", "Diya Patel", "Interview live now"),
                              ("karthik", "Karthik Nair", "Retake later"), ("sneha", "Sneha Reddy", "Badge earned"),
                              ("rahul", "Rahul Verma", "One skill short"), ("imran", "Imran Khan", "Badge nullified")]),
                ("Interviewers", [("priya", "Priya Raman", "5 interviews today"), ("vikram", "Vikram Sethi", "")]),
                ("Interview team", [("ops", "Interview Ops", "Tracker, outcomes, integrity")]),
            ]
        return ctx


# CSRF-exempt on purpose: low-risk action (worst case a forged request signs the user out).
# Everything that changes real data (booking, outcomes, flags, availability…) keeps CSRF protection.
@method_decorator(csrf_exempt, name="dispatch")
class SignOutView(View):
    """POST-only sign-out. Not Django's LogoutView: that one forces csrf_protect internally,
    which would override the csrf_exempt above."""
    http_method_names = ["post"]

    def post(self, request):
        logout(request)
        return redirect("login")


class CsrfFailureView(TemplateView):
    """Shown instead of Django's bare 403 when a form's security token is stale or missing.

    Most common cause: the page was open before the user signed in / switched account, which
    rotates the token. The page renders a fresh token, so the button it offers works.
    """
    template_name = "interviews/csrf_failure.html"

    def dispatch(self, request, *args, reason="", **kwargs):
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):  # failures happen on POST; render the same page
        return self.get(request, *args, **kwargs)

    def render_to_response(self, context, **kwargs):
        return super().render_to_response(context, status=403, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        back = self.request.META.get("HTTP_REFERER", "")
        ctx["is_logout"] = self.request.path == reverse("logout")
        ctx["back_url"] = back if back and url_has_allowed_host_and_scheme(back, {self.request.get_host()}, require_https=self.request.is_secure()) else reverse("home")
        return ctx


csrf_failure = CsrfFailureView.as_view()  # settings.CSRF_FAILURE_VIEW points here


class DemoLoginView(DemoOnlyMixin, ServiceCallMixin, View):
    """Demo only: one-click sign-in as any demo persona."""

    def get(self, request, username):
        from .seed import demo_usernames

        user = get_user_model().objects.filter(username=username).first()
        if not user or username not in set(demo_usernames()):
            raise Http404
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        return redirect(self.safe_next("/"))


class DemoSwitchView(LoginRequiredMixin, DemoOnlyMixin, RedirectView):
    def get_redirect_url(self, *args, **kwargs):
        return reverse("demo_login", args=[self.request.GET.get("username", "")])


# CSRF-exempt on purpose: low-risk action (worst case a forged request moves the demo clock; demo mode only).
# Everything that changes real data (booking, outcomes, flags, availability…) keeps CSRF protection.
@method_decorator(csrf_exempt, name="dispatch")
class DemoClockView(LoginRequiredMixin, DemoOnlyMixin, ServiceCallMixin, View):
    http_method_names = ["post"]
    ALLOWED = {"3600": "1 hour", "86400": "1 day", "604800": "7 days"}

    def post(self, request):
        secs = request.POST.get("seconds")
        if secs in self.ALLOWED:
            svc.advance_clock(int(secs))
            messages.info(request, f"Clock moved forward {self.ALLOWED[secs]}. Scheduled jobs (cooldowns, SLAs) have run.")
        return redirect(self.safe_next("/"))


# CSRF-exempt on purpose: low-risk action (worst case a forged request resets demo data; demo mode only).
# Everything that changes real data (booking, outcomes, flags, availability…) keeps CSRF protection.
@method_decorator(csrf_exempt, name="dispatch")
class DemoResetView(LoginRequiredMixin, DemoOnlyMixin, View):
    http_method_names = ["post"]

    def post(self, request):
        from .seed import reset_demo

        username = request.user.username
        reset_demo()
        user = get_user_model().objects.filter(username=username).first()
        if user:  # demo users are recreated, so sign the same person back in
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        messages.success(request, "Demo data reset.")
        return redirect("home")


# =============================================================== student
class StudentHomeView(StudentMixin, TemplateView):
    template_name = "interviews/student/home.html"
    nav = "home"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["c"] = student_context(self.student, clock_now(), self.request.GET.get("journey"))
        return ctx


class StudentBookView(StudentMixin, TemplateView):
    template_name = "interviews/student/book.html"
    nav = "home"

    def get(self, request, *args, **kwargs):
        s, now = self.student, clock_now()
        c = student_context(s, now, request.GET.get("journey"))
        j = c["journey"]
        resched = None
        if request.GET.get("reschedule"):
            resched = Interview.objects.filter(pk=request.GET["reschedule"], journey__student=s, status=Interview.SCHEDULED).first()
        if not resched and j.state != M.ELIGIBLE:
            self.template_name = "interviews/student/book_closed.html"
            return self.render_to_response(self.get_context_data(c=c))
        if resched:
            j = resched.journey

        times = dict(svc.available_times(j, now, ignore_interview_id=resched.pk if resched else None))
        days = []
        for d in range(CONFIG["BOOK_WINDOW_DAYS"] + 1):
            day = day_start(now) + d * DAY
            n = len(times.get(day, []))
            days.append({"day": day, "ms": to_ms(day), "n": n, "few": 0 < n <= 3})
        try:
            selected = from_ms(request.GET["day"]) if request.GET.get("day") else None
        except DomainError:
            selected = None
        if selected not in times:
            selected = next((d["day"] for d in days if d["n"]), None)
        groups = []
        for label, lo, hi in (("Morning", 0, 12), ("Afternoon", 12, 17), ("Evening", 17, 24)):
            items = [t for t in times.get(selected, []) if lo <= t["start"].astimezone(IST).hour < hi]
            if items:
                groups.append((label, items))
        return self.render_to_response(self.get_context_data(
            c=c, days=days, selected=selected, groups=groups, resched=resched, any_slots=any(d["n"] for d in days),
            resched_left_after=(CONFIG["RESCHEDULE_LIMIT"] - resched.reschedule_count - 1) if resched else None))


class StudentConfirmView(StudentMixin, TemplateView):
    """GET: confirmation page for a chosen slot. POST: book (or reschedule)."""
    template_name = "interviews/student/confirm.html"
    nav = "home"

    def _resched(self):
        rid = self.request.POST.get("reschedule") or self.request.GET.get("reschedule")
        return get_object_or_404(Interview, pk=rid, journey__student=self.student) if rid else None

    def get(self, request, *args, **kwargs):
        try:
            start = from_ms(request.GET.get("start"))
        except DomainError:
            return redirect("student_book")
        return self.render_to_response(self.get_context_data(
            c=student_context(self.student, clock_now()), start=start, start_ms=to_ms(start),
            end=start + timedelta(minutes=CONFIG["SLOT_MIN"]), resched=self._resched()))

    def post(self, request, *args, **kwargs):
        resched = self._resched()
        extra = {"reschedule": resched.pk} if resched else {}
        form = (RescheduleForm if resched else BookConfirmForm)(request.POST)
        if not form.is_valid():
            for errs in form.errors.values():
                for e in errs:
                    messages.error(request, e)
            return redirect(f"{reverse('student_confirm')}?{urlencode({'start': request.POST.get('start', ''), **extra})}")
        start = form.cleaned_data["start"]
        if resched:
            ok, _ = self.attempt(lambda: svc.reschedule(request.user, resched.pk, start), "Interview moved. Updated details are in your inbox.")
        else:
            journey = student_context(self.student, clock_now())["journey"]
            ok, _ = self.attempt(lambda: svc.book(request.user, journey.pk, start), "You're booked! Confirmation sent to your inbox.")
        return redirect("student_interview" if ok else f"{reverse('student_book')}?{urlencode(extra)}")


class StudentInterviewView(StudentMixin, TemplateView):
    template_name = "interviews/student/interview.html"
    nav = "interview"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        c = student_context(self.student, clock_now())
        ctx.update(c=c, rubric=RUBRIC, past=[h for h in c["history"] if hasattr(h, "outcome") or h.status == Interview.CANCELLED])
        return ctx


class StudentCancelView(StudentMixin, TemplateView):
    template_name = "interviews/student/cancel.html"
    nav = "interview"

    def get_interview(self):
        return get_object_or_404(Interview, pk=self.kwargs["pk"], journey__student=self.student)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["iv"] = self.get_interview()
        return ctx

    def post(self, request, *args, **kwargs):
        iv = self.get_interview()
        ok, _ = self.attempt(lambda: svc.cancel(request.user, iv.pk), "Interview cancelled. No attempt used.")
        return redirect("student_home" if ok else "student_interview")


class StudentJoinView(StudentMixin, View):
    def get(self, request, pk):
        get_object_or_404(Interview, pk=pk, journey__student=self.student)
        messages.info(request, "In production this opens the interview room (Meet link or in-app call).")
        return redirect("student_interview")


class StudentInboxView(StudentMixin, TemplateView):
    template_name = "interviews/student/inbox.html"
    nav = "inbox"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        s = self.student
        items = []
        for n in Notification.objects.filter(student=s).order_by("-created_at", "-id"):
            m = copy.render(n.kind, n.payload)
            items.append({**m, "at": n.created_at, "unread": n.read_at is None, "target": copy.CTA_TARGET.get(n.kind)})
        svc.mark_read(s)  # shown as unread this time, read next time
        ctx["items"] = items
        return ctx


# CSRF-exempt on purpose: low-risk action (worst case a forged request clears a demo skill; demo mode only).
# Everything that changes real data (booking, outcomes, flags, availability…) keeps CSRF protection.
@method_decorator(csrf_exempt, name="dispatch")
class DemoClearSkillView(DemoOnlyMixin, StudentMixin, View):
    http_method_names = ["post"]

    def post(self, request):
        try:
            level = int(request.POST.get("level", 0) or 0)
        except ValueError:
            level = 0
        self.attempt(lambda: svc.clear_skill(self.student, request.POST.get("key"), level), "Skill cleared — check the unlock message in your Inbox.")
        return redirect("student_home")


# CSRF-exempt on purpose: low-risk action (worst case a forged request skips a demo cooldown; demo mode only).
# Everything that changes real data (booking, outcomes, flags, availability…) keeps CSRF protection.
@method_decorator(csrf_exempt, name="dispatch")
class DemoSkipCooldownView(DemoOnlyMixin, StudentMixin, View):
    http_method_names = ["post"]

    def post(self, request):
        j = student_context(self.student, clock_now())["journey"]
        if j.cooldown_until:
            svc.advance_clock(max(0, (j.cooldown_until - clock_now()).total_seconds()) + 60)
            messages.info(request, "Clock moved to the end of the cooldown.")
        return redirect("student_home")


# =============================================================== interviewer
class InterviewerTodayView(InterviewerMixin, TemplateView):
    template_name = "interviews/interviewer/today.html"
    nav = "schedule"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        me, now = self.me, clock_now()
        try:
            day = day_start(from_ms(self.request.GET["day"])) if self.request.GET.get("day") else day_start(now)
        except DomainError:
            day = day_start(now)
        ivs = list(Interview.objects.filter(interviewer=me, start_at__gte=day, start_at__lt=day + DAY).exclude(status=Interview.CANCELLED)
                   .select_related("journey__student", "journey__badge", "outcome").order_by("start_at"))
        timeline = []
        for iv in ivs:
            prev = list(Interview.objects.filter(journey=iv.journey, outcome__isnull=False).exclude(pk=iv.pk)
                        .exclude(outcome__result="tech_issue").select_related("outcome").order_by("start_at"))
            timeline.append({"t": iv.start_at, "iv": iv, "prev": prev,
                             "live": iv.start_at - CONFIG["JOIN_OPENS_MIN"] * MIN <= now <= iv.end_at,
                             "started": now >= iv.start_at,
                             "overdue": iv.status == Interview.SCHEDULED and now > iv.end_at + CONFIG["OUTCOME_SLA_H"] * HOUR})
        opens = AvailabilitySlot.objects.filter(interviewer=me, status=AvailabilitySlot.OPEN, start_at__gte=day, start_at__lt=day + DAY)
        timeline += [{"t": s.start_at, "slot": s} for s in opens]
        timeline.sort(key=lambda x: x["t"])
        pending = list(Interview.objects.filter(interviewer=me, status=Interview.SCHEDULED, start_at__lte=now).select_related("journey__student").order_by("start_at"))
        ctx.update(me=me, day=day, prev_day=to_ms(day - DAY), next_day=to_ms(day + DAY), timeline=timeline, booked=len(ivs),
                   open_count=opens.count(), pending=pending, pending_other=[p for p in pending if day_start(p.start_at) != day],
                   auto_refresh=True)
        return ctx


class InterviewerOutcomeView(InterviewerMixin, FormView):
    template_name = "interviews/interviewer/outcome.html"
    form_class = OutcomeForm
    nav = "schedule"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and svc.role_of(request.user) == "interviewer":
            self.interview = get_object_or_404(Interview.objects.select_related("journey__student", "journey__badge"),
                                               pk=kwargs["pk"], interviewer=self.me)
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        d, iv = form.cleaned_data, self.interview
        labels = {"no_show": "marked as no-show", "tech_issue": "marked as technical issue — priority rebooking given",
                  "pass": "passed — badge awarded", "retake": "set to retake later", "fail": "recorded as not cleared"}
        label = labels.get(d["attendance"]) if d["attendance"] != "attended" else labels.get(d.get("result"), "saved")
        ok, _ = self.attempt(lambda: svc.record_outcome(self.request.user, iv.pk, d["attendance"], d.get("result"), form.scores(), d["feedback"], d["notes"]),
                             f"{iv.journey.student.name}: {label}. Student notified.")
        if ok:
            return redirect(f"{reverse('iv_today')}?day={to_ms(day_start(iv.start_at))}")
        return self.form_invalid(form)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        iv, now = self.interview, clock_now()
        j = iv.journey
        b = copy.badge_dict(j.badge)
        until = lambda kind: to_ms(day_start(now) + (CONFIG["COOLDOWN_DAYS"][kind] + 1) * DAY)  # noqa: E731
        ctx.update(iv=iv, j=j, badge=j.badge, cd=CONFIG["COOLDOWN_DAYS"], min_fb=CONFIG["MIN_FEEDBACK_CHARS"],
                   last_attempt=j.max_attempts - j.attempts_used <= 1,
                   previews={  # exactly what the student will receive, per choice (shown with CSS only)
                       "pass": copy.render("PASSED", {"badge": b, "first": j.student.name.split()[0], "rank": "—"}),
                       "retake": copy.render("RETAKE", {"badge": b, "feedback": "<your feedback>", "until": until("RETAKE"), "used": j.attempts_used + 1, "max": j.max_attempts}),
                       "fail": copy.render("FAILED", {"badge": b, "feedback": "<your feedback>", "until": until("FAILED"), "used": j.attempts_used + 1, "max": j.max_attempts}),
                       "no_show": copy.render("NO_SHOW", {"badge": b, "start": to_ms(iv.start_at), "until": until("NO_SHOW"), "counted": j.no_shows >= 1}),
                       "tech_issue": copy.render("TECH_ISSUE", {}),
                   })
        return ctx


class ReleaseInterviewView(RoleRequiredMixin, ServiceCallMixin, NavMixin, FormView):
    """Interviewer (own booking) or ops: 'can't make it' → reassign to a colleague, else cancel with priority."""
    roles = ("interviewer", "ops")
    template_name = "interviews/interviewer/release.html"
    form_class = ReleaseForm
    nav = "schedule"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            self.interview = get_object_or_404(Interview.objects.select_related("journey__student"), pk=kwargs["pk"])
            if svc.role_of(request.user) == "interviewer" and self.interview.interviewer_id != svc.interviewer_of(request.user).pk:
                raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        self.attempt(lambda: svc.release_interview(self.request.user, self.interview.pk, form.cleaned_data["reason"]),
                     lambda r: "Handed to a colleague at the same time. Student notified." if r == "reassigned"
                     else "No colleague free — booking cancelled, student given priority rebooking.")
        return redirect("iv_today" if svc.role_of(self.request.user) == "interviewer" else "ops_tracker")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["iv"] = self.interview
        return ctx


class AvailabilityView(InterviewerMixin, TemplateView):
    template_name = "interviews/interviewer/availability.html"
    nav = "availability"

    def week(self):
        try:
            return max(0, min(1, int(self.request.GET.get("week", 0) or 0)))
        except ValueError:
            return 0

    def post(self, request, *args, **kwargs):
        if "toggle" in request.POST:
            try:
                t = from_ms(request.POST["toggle"])
            except DomainError as e:
                messages.error(request, str(e))
            else:
                self.attempt(lambda: svc.toggle_slot(request.user, t))
        elif "add_window" in request.POST:
            f = AddWindowForm(request.POST)
            if f.is_valid():
                d = f.cleaned_data
                self.attempt(lambda: svc.add_window(request.user, day_start(d["day"]), d["start_min"], d["end_min"]),
                             lambda n: f"{n} slot{'s' if n != 1 else ''} opened." if n else "Those slots are already open (or in the past).")
            else:
                messages.error(request, "Pick a day and a valid time range.")
        return redirect(f"{reverse('iv_availability')}?week={self.week()}")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        me, now, week = self.me, clock_now(), self.week()
        base = day_start(now) + week * 7 * DAY
        days = [base + i * DAY for i in range(7)]
        slots = {s.start_at: s for s in AvailabilitySlot.objects.filter(interviewer=me, start_at__gte=base, start_at__lt=base + 7 * DAY)
                 .exclude(status=AvailabilitySlot.EXPIRED)}
        booked_ivs = {iv.slot_id: iv for iv in Interview.objects.filter(interviewer=me, slot__in=list(slots.values()))
                      .exclude(status=Interview.CANCELLED).select_related("journey__student")}
        rows = []
        for m in range(8 * 60, 21 * 60, CONFIG["SLOT_MIN"]):
            cells = []
            for d in days:
                t = d + timedelta(minutes=m)
                s = slots.get(t)
                state = "booked" if s and s.status == AvailabilitySlot.BOOKED else "open" if s else ""
                cells.append({"t": t, "ms": to_ms(t), "state": state, "past": t <= now, "iv": booked_ivs.get(s.pk) if s else None})
            rows.append({"label": (day_start(now) + timedelta(minutes=m)) if m % 60 == 0 else None, "cells": cells})
        week_slots = list(slots.values())
        ctx.update(me=me, week=week, rows=rows,
                   days=[{"day": d, "ms": to_ms(d), "booked": sum(1 for s in week_slots if day_start(s.start_at) == d and s.status == AvailabilitySlot.BOOKED)} for d in days],
                   times=[(m, day_start(now) + timedelta(minutes=m)) for m in range(8 * 60, 21 * 60 + 1, CONFIG["SLOT_MIN"])],
                   booked=sum(1 for s in week_slots if s.status == AvailabilitySlot.BOOKED),
                   open=sum(1 for s in week_slots if s.status == AvailabilitySlot.OPEN and s.start_at > now))
        return ctx


class BlockDayView(InterviewerMixin, TemplateView):
    template_name = "interviews/interviewer/block.html"
    nav = "availability"

    def get_day(self):
        try:
            return day_start(from_ms(self.request.POST.get("day") or self.request.GET.get("day")))
        except DomainError:
            return None

    def get(self, request, *args, **kwargs):
        day = self.get_day()
        if not day:
            return redirect("iv_availability")
        bookings = Interview.objects.filter(interviewer=self.me, status=Interview.SCHEDULED, start_at__gte=max(day, clock_now()), start_at__lt=day + DAY).count()
        return self.render_to_response(self.get_context_data(day=day, day_ms=to_ms(day), bookings=bookings))

    def post(self, request, *args, **kwargs):
        day = self.get_day()
        if day:
            self.attempt(lambda: svc.block_day(request.user, day),
                         lambda r: f"Day blocked: {r['removed']} open slots removed, {r['reassigned']} reassigned, {r['cancelled']} cancelled with priority.")
        return redirect("iv_availability")


# =============================================================== interview ops
GROUPS = [
    ("all", "All", None), ("unbooked", "Eligible · not booked", [M.ELIGIBLE]), ("booked", "Booked", [M.SCHEDULED]),
    ("awaiting", "Awaiting outcome", [M.AWAITING_RESULT]), ("passed", "Passed", [M.PASSED]),
    ("cooldown", "Retake / Fail / No-show", [M.RETAKE, M.FAILED, M.NO_SHOW, M.CLOSED]),
    ("hold", "On hold / Nullified", [M.ON_HOLD, M.REVOKED]), ("locked", "Skills in progress", [M.LOCKED]),
]


class OpsOverviewView(OpsMixin, TemplateView):
    template_name = "interviews/ops/overview.html"
    nav = "overview"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = clock_now()
        rows = tracker_rows(now)
        count = lambda states: sum(1 for r in rows if r["j"].state in states)  # noqa: E731
        unbooked = [r for r in rows if r["j"].state == M.ELIGIBLE]
        stale = [r for r in unbooked if r["risk"] == "nudge"]
        awaiting = [r for r in rows if r["j"].state == M.AWAITING_RESULT]
        overdue = [r for r in awaiting if r["risk"] == "overdue"]
        decided = Interview.objects.filter(outcome__result__in=["pass", "retake", "fail"])
        pass_rate = round(decided.filter(outcome__result="pass").count() / decided.count() * 100) if decided.exists() else 0
        seats7 = svc.open_seats(now, 7)
        waiting = len(unbooked) + sum(1 for r in rows if r["j"].state in M.COOLDOWN_STATES and r["j"].cooldown_until and r["j"].cooldown_until < now + 7 * DAY)
        funnel = [
            ("Skills in progress", count([M.LOCKED]), "muted"),
            ("Eligible (all)", sum(1 for r in rows if r["j"].state != M.LOCKED), "info"),
            ("Booked or beyond", sum(1 for r in rows if r["j"].state not in (M.LOCKED, M.ELIGIBLE, M.ON_HOLD)), "primary"),
            ("Interviewed", sum(1 for r in rows if r["j"].state in (M.AWAITING_RESULT, M.PASSED, M.RETAKE, M.FAILED, M.CLOSED)
                                or (r["j"].state == M.REVOKED and r["j"].prev_state == M.PASSED)), "warn"),
            ("Badge earned", count([M.PASSED]), "success"),
        ]
        max_f = max([f[1] for f in funnel] + [1])
        load = [{"iv": iv,
                 "upcoming": Interview.objects.filter(interviewer=iv, status=Interview.SCHEDULED, start_at__gt=now, start_at__lt=now + 7 * DAY).count(),
                 "open": AvailabilitySlot.objects.filter(interviewer=iv, status=AvailabilitySlot.OPEN, start_at__gt=now, start_at__lt=now + 7 * DAY).count(),
                 "pending": Interview.objects.filter(interviewer=iv, status=Interview.SCHEDULED, start_at__lte=now).count()}
                for iv in Interviewer.objects.all()]
        dl = svc.deadline(now)
        ctx.update(rows=rows, unbooked=len(unbooked), stale=len(stale), booked=count([M.SCHEDULED]), awaiting=len(awaiting),
                   overdue=len(overdue), passed=count([M.PASSED]), pass_rate=pass_rate, seats7=seats7, waiting=waiting,
                   meter=min(100, round(waiting / max(seats7, 1) * 100)),
                   health="ok" if seats7 >= waiting * 1.5 else "warn" if seats7 >= waiting else "bad",
                   funnel=[(lbl, n, tone, round(n / max_f * 100)) for lbl, n, tone in funnel], load=load,
                   flags=IntegrityFlag.objects.filter(status="open").count(), deadline_risk=sum(1 for r in rows if r["risk"] == "deadline"),
                   deadline=dl, days_left=-(-(dl - now) // DAY), students=Student.objects.count(), interviewers=len(load),
                   nudge_after=CONFIG["NUDGE_AFTER_DAYS"], sla_h=CONFIG["OUTCOME_SLA_H"])
        return ctx


class OpsTrackerView(OpsMixin, TemplateView):
    template_name = "interviews/ops/tracker.html"
    nav = "tracker"

    def post(self, request, *args, **kwargs):
        if request.POST.get("action") == "nudge_stale":
            ids = [r["j"].pk for r in tracker_rows(clock_now()) if r["risk"] == "nudge"]
        else:
            ids = [int(x) for x in request.POST.getlist("journey") if x.isdigit()]
        if not ids:
            messages.error(request, "Select at least one student to nudge.")
        else:
            self.attempt(lambda: svc.nudge(request.user, ids), lambda n: f"Reminder sent to {n} student{'s' if n != 1 else ''}.")
        return redirect(self.safe_next(reverse("ops_tracker")))

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        g = self.request.GET
        flt, q, campus = g.get("filter", "unbooked"), g.get("q", "").strip().lower(), g.get("campus", "")
        all_rows = tracker_rows(clock_now())
        states = {k: s for k, _, s in GROUPS}.get(flt)
        rows = [r for r in all_rows if (not states or r["j"].state in states) and (not campus or r["s"].campus == campus)
                and (not q or q in r["s"].name.lower() or q in r["s"].roll_no.lower())]
        ctx.update(rows=rows, filter=flt, q=g.get("q", ""), campus=campus, now_path=self.request.get_full_path(),
                   groups=[(k, label, sum(1 for r in all_rows if not s or r["j"].state in s)) for k, label, s in GROUPS],
                   campuses=sorted(set(Student.objects.values_list("campus", flat=True))),
                   selectable=any(r["j"].state == M.ELIGIBLE for r in rows),
                   flaggable={s for s in M.STATES if M.can(s, "FLAG_RAISED")})
        return ctx


class OpsOutcomesView(OpsMixin, TemplateView):
    template_name = "interviews/ops/outcomes.html"
    nav = "outcomes"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = clock_now()
        pending = Interview.objects.filter(status=Interview.SCHEDULED, start_at__lte=now).select_related("journey__student", "interviewer").order_by("start_at")
        ctx.update(sla_h=CONFIG["OUTCOME_SLA_H"],
                   pending=[{"iv": iv, "overdue": now > iv.end_at + CONFIG["OUTCOME_SLA_H"] * HOUR, "due": iv.end_at + CONFIG["OUTCOME_SLA_H"] * HOUR} for iv in pending],
                   recent=Interview.objects.filter(outcome__isnull=False).select_related("journey__student", "interviewer", "outcome").order_by("-outcome__recorded_at")[:15])
        return ctx


class OpsIntegrityView(OpsMixin, TemplateView):
    template_name = "interviews/ops/integrity.html"
    nav = "integrity"

    def post(self, request, *args, **kwargs):
        decision = request.POST.get("decision")
        self.attempt(lambda: svc.resolve_flag(request.user, request.POST.get("flag"), decision),
                     "Malpractice confirmed. Badge nullified and Miles reversed." if decision == "confirmed" else "Flag dismissed. Previous state restored.")
        return redirect("ops_integrity")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        flags = list(IntegrityFlag.objects.select_related("journey__student", "journey__badge").order_by("-raised_at"))
        ctx.update(open=[f for f in flags if f.status == "open"], done=[f for f in flags if f.status != "open"])
        return ctx


class OpsFlagCreateView(OpsMixin, FormView):
    template_name = "interviews/ops/flag_new.html"
    form_class = FlagForm
    nav = "integrity"

    def get_initial(self):
        return {"journey": self.request.GET.get("journey")}

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["journeys"] = [(str(j.pk), f"{j.student.name} — {j.badge} ({STATE_META[j.state]['label']})")
                              for j in Journey.objects.select_related("student", "badge").order_by("student__name") if M.can(j.state, "FLAG_RAISED")]
        return kwargs

    def form_valid(self, form):
        ok, _ = self.attempt(lambda: svc.raise_flag(self.request.user, int(form.cleaned_data["journey"]), form.cleaned_data["reason"]),
                             "Flag raised. Journey on hold, student notified.")
        return redirect("ops_integrity") if ok else self.form_invalid(form)


class OpsExtraAttemptView(OpsMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        self.attempt(lambda: svc.grant_extra_attempt(request.user, pk), "Extra attempt granted. Student notified.")
        return redirect(self.safe_next(reverse("ops_tracker")))


class OpsAuditView(OpsMixin, ListView):
    template_name = "interviews/ops/audit.html"
    context_object_name = "events"
    nav = "audit"

    def get_queryset(self):
        q = self.request.GET.get("q", "").strip()
        qs = JourneyEvent.objects.select_related("student")
        if q:
            qs = qs.filter(Q(student__name__icontains=q) | Q(event__icontains=q) | Q(actor__icontains=q))
        return qs[:200]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = self.request.GET.get("q", "").strip()
        return ctx


class OpsExportView(OpsMixin, View):
    def get(self, request):
        now = clock_now()
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="grit-interview-tracker-{now:%Y-%m-%d}.csv"'
        w = csv.writer(resp)
        w.writerow(["Student", "Roll", "Campus", "Batch", "Badge", "Status", "Detail", "Attempts used"])
        safe = lambda v: f"'{v}" if str(v)[:1] in "=+-@" else v  # noqa: E731 — block spreadsheet formula injection
        for r in tracker_rows(now):
            w.writerow([safe(x) for x in (r["s"].name, r["s"].roll_no, r["s"].campus, r["s"].batch, str(r["b"]),
                                          STATE_META[r["j"].state]["label"], r["detail"], r["j"].attempts_used)])
        return resp


# =============================================================== shared pages
SCENARIOS = {
    "pass": ("Pass", M.AWAITING_RESULT, M.PASSED), "retake": ("Retake later", M.AWAITING_RESULT, M.RETAKE),
    "fail": ("Fail", M.AWAITING_RESULT, M.FAILED), "revoked": ("Badge nullified (malpractice)", M.PASSED, M.REVOKED),
}
FEEDBACK = {
    "pass": "Clear explanation of your RAG pipeline and a clean UI build. Keep going for Specialist.",
    "retake": "Good UI fundamentals, but you struggled to explain how your prompt pipeline handles bad model output. Practise adding validation and fallbacks.",
    "fail": "The build task was left incomplete and API basics need more work. Revisit UI Engineering L2 projects before your next attempt.",
}
SCORES = {"pass": {"problem": 4, "concepts": 3, "build": 4, "comm": 4}, "retake": {"problem": 3, "concepts": 2, "build": 3, "comm": 3},
          "fail": {"problem": 2, "concepts": 1, "build": 1, "comm": 2}}


class CompareView(RoleRequiredMixin, NavMixin, TemplateView):
    """Before/after the result: the real templates rendered for an in-memory student (nothing is saved)."""
    template_name = "interviews/compare.html"
    nav = "compare"

    def synthetic(self, key, state, now):
        badge = Badge.objects.select_related("next_badge").get(code="APM-NOV")
        has = state == M.PASSED
        miles = 850 + (badge.miles if has else 0)  # 850 = Aarav's skill Miles in the seed
        rank = sum(1 for r in leaderboard() if r["qualiflyer"] and r["miles"] > miles) + 1 if has else None
        rk = "pass" if key == "revoked" else key
        outcome = SimpleNamespace(result=rk, scores=SCORES.get(rk), feedback_to_student=FEEDBACK.get(rk, ""), recorded_by_name="Priya Raman") \
            if state in (M.PASSED, M.RETAKE, M.FAILED, M.REVOKED) else None
        cd = {M.RETAKE: CONFIG["COOLDOWN_DAYS"]["RETAKE"], M.FAILED: CONFIG["COOLDOWN_DAYS"]["FAILED"]}.get(state)
        j = SimpleNamespace(state=state, attempts_used=1 if outcome else 0, max_attempts=CONFIG["MAX_ATTEMPTS"], priority=False, no_shows=0,
                            cooldown_until=day_start(now) + (cd + 1) * DAY if cd else None, prev_state=M.PASSED if state == M.REVOKED else "",
                            revoked_at=now, awarded_at=now, badge=badge, pk=0)
        journeys = [j] + ([SimpleNamespace(state=M.LOCKED, badge=badge.next_badge, pk=1)] if has else [])
        progress = [{"key": k, "name": SKILLS[k], "need": lvl, "have": lvl, "ok": True} for k, lvl in badge.required_skills]
        c = {"student": SimpleNamespace(name="Aarav Sharma", campus="Hyderabad"), "journey": j, "badge": badge, "journeys": journeys,
             "progress": progress, "iv": None, "last_outcome": outcome, "miles": miles, "rank": rank, "qualiflyer": has,
             "deadline": svc.deadline(now), "days_left": 0, "seats_week": 0, "attempts_left": CONFIG["MAX_ATTEMPTS"] - j.attempts_used,
             "shelf": badge_shelf(journeys, badge.track), "weak": weak_areas(outcome.scores) if outcome else [], "now": now,
             "stepper": stepper(j, progress, None), "demo": True}
        c["message"], c["detail"] = self.message(c, key), self.detail(c, now)
        return c

    @staticmethod
    def message(c, key):
        s, b = c["journey"].state, copy.badge_dict(c["badge"])
        if s == M.AWAITING_RESULT:
            return {"title": "No new message", "body": "Student is waiting. Dashboard says the result arrives within 24 hours.", "tone": "muted"}
        if s == M.PASSED:
            return copy.render("PASSED", {"badge": b, "first": "Aarav", "rank": c["rank"], "next_badge": copy.badge_dict(c["badge"].next_badge), "next_have": 2})
        if s in (M.RETAKE, M.FAILED):
            return copy.render(s, {"badge": b, "feedback": FEEDBACK[key], "until": to_ms(c["journey"].cooldown_until), "used": 1, "max": 3})
        return copy.render("REVOKED", {"badge": b, "had_badge": True})

    @staticmethod
    def detail(c, now):
        j = c["journey"]
        rebook = f"Can rebook {fmt_date(j.cooldown_until)}" if j.cooldown_until else ""
        return {M.AWAITING_RESULT: "Interviewed today · Priya Raman", M.PASSED: f"Awarded {fmt_date(now)}", M.RETAKE: rebook,
                M.FAILED: rebook, M.REVOKED: f"Nullified {fmt_date(now)}"}.get(j.state, "")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = clock_now()
        key = self.request.GET.get("scenario", "pass")
        key = key if key in SCENARIOS else "pass"
        _, before_state, after_state = SCENARIOS[key]
        ctx.update(key=key, scenarios=[(k, v[0]) for k, v in SCENARIOS.items()],
                   before=self.synthetic(key, before_state, now), after=self.synthetic(key, after_state, now))
        return ctx


class HowItWorksView(RoleRequiredMixin, NavMixin, TemplateView):
    """States, transitions and policies rendered from machine.py / utils.py — the code that enforces them."""
    template_name = "interviews/how_it_works.html"
    nav = "spec"
    LAYOUT = {M.LOCKED: (10, 140), M.ELIGIBLE: (190, 140), M.SCHEDULED: (370, 140), M.AWAITING_RESULT: (550, 140), M.PASSED: (760, 140),
              M.NO_SHOW: (330, 260), M.RETAKE: (470, 260), M.FAILED: (610, 260), M.CLOSED: (770, 260), M.ON_HOLD: (550, 20), M.REVOKED: (760, 20)}

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = clock_now()
        b = copy.badge_dict(Badge.objects.get(code="APM-NOV"))
        start = day_start(now) + 2 * DAY + 16 * HOUR
        key_msgs = [
            ("When the last required skill is cleared", "Turns “eligible” into “booked”. States the reward, the effort (30 min), and the deadline — one button.",
             copy.render("ELIGIBLE", {"badge": b})),
            ("Right after booking", "Prevents no-shows: exact time, how to join, what to keep ready, and how to change it.",
             copy.render("BOOKED", {"badge": b, "start": to_ms(start), "interviewer": "Priya Raman"})),
            ("When the outcome is “Retake later”", "The hardest message. Leads with “not yet”, quotes the interviewer, gives a date and an honest attempt count.",
             copy.render("RETAKE", {"badge": b, "feedback": FEEDBACK["retake"], "until": to_ms(day_start(now) + 8 * DAY), "used": 1, "max": 3})),
        ]
        policies = [("Interview length", f"{CONFIG['SLOT_MIN']} min"),
                    ("Book at least", f"{CONFIG['BOOK_MIN_LEAD_H']}h ahead ({CONFIG['PRIORITY_LEAD_H']}h with priority)"),
                    ("Booking window", f"{CONFIG['BOOK_WINDOW_DAYS']} days"),
                    ("Reschedule", f"{CONFIG['RESCHEDULE_LIMIT']}× per booking, until {CONFIG['CHANGE_CUTOFF_H']}h before"),
                    ("Cancel", f"until {CONFIG['CHANGE_CUTOFF_H']}h before, no attempt used"),
                    ("Join opens", f"{CONFIG['JOIN_OPENS_MIN']} min before"), ("Outcome SLA", f"{CONFIG['OUTCOME_SLA_H']}h"),
                    ("Attempts per badge", f"{CONFIG['MAX_ATTEMPTS']} per season"),
                    ("Cooldowns", "Retake {RETAKE}d · Fail {FAILED}d · No-show {NO_SHOW}d".format(**CONFIG["COOLDOWN_DAYS"])),
                    ("Feedback", f"≥ {CONFIG['MIN_FEEDBACK_CHARS']} chars for non-pass outcomes")]
        ctx.update(states=list(STATE_META.items()), transitions=M.TRANSITIONS, key_msgs=key_msgs, policies=policies,
                   nodes=[(s, STATE_META[s]["label"].split(" · ")[0], x, y, STATE_META[s]["tone"]) for s, (x, y) in self.LAYOUT.items()])
        return ctx
