"""Domain + permission + page tests. Run: python manage.py test interviews"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from interviews import machine as M
from interviews import services as svc
from interviews.utils import DomainError, Forbidden, DAY, HOUR, MIN, now
from interviews.models import AvailabilitySlot, Interview, InterviewOutcome, Journey, JourneyEvent, Notification, Student
from interviews.seed import seed
from interviews.selectors import miles, rank_of

FB = "Explain your pipeline error handling more clearly and add fallbacks for bad model output."
SCORES = {"problem": 3, "concepts": 2, "build": 3, "comm": 3}


def user(name):
    return get_user_model().objects.get(username=name)


def journey(code, badge="APM-NOV"):
    return Journey.objects.get(student__code=code, badge_id=badge)


def first_time(j, **kw):
    days = svc.available_times(j, now(), **kw)
    return days[0][1][0]["start"]


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed()


class StateMachineTests(Base):
    def test_seed_covers_every_state(self):
        present = set(Journey.objects.values_list("state", flat=True))
        self.assertEqual(present, set(M.STATES))

    def test_illegal_transition_rejected(self):
        with self.assertRaises(DomainError):
            M.next_state(M.LOCKED, "BOOK")
        with self.assertRaises(DomainError):
            M.next_state(M.REVOKED, "RECORD_PASS")

    def test_past_interviews_moved_to_awaiting(self):
        self.assertEqual(journey("s6").state, M.AWAITING_RESULT)
        self.assertTrue(JourneyEvent.objects.filter(event="SLOT_ENDED", student__code="s6").exists())


class BookingTests(Base):
    def test_book_happy_path(self):
        j = journey("s1")
        iv = svc.book(user("aarav"), j.pk, first_time(j))
        j.refresh_from_db()
        self.assertEqual(j.state, M.SCHEDULED)
        self.assertEqual(iv.slot.status, AvailabilitySlot.BOOKED)
        self.assertEqual(Notification.objects.filter(student=j.student).latest("created_at").kind, "BOOKED")

    def test_lead_time_and_window(self):
        j = journey("s1")
        with self.assertRaisesMessage(DomainError, "12 hours"):
            svc.book(user("aarav"), j.pk, now() + 2 * HOUR)
        with self.assertRaisesMessage(DomainError, "14 days"):
            svc.book(user("aarav"), j.pk, now() + 20 * DAY)

    def test_cannot_book_someone_elses_journey(self):
        with self.assertRaises(Forbidden):
            svc.book(user("aarav"), journey("s14").pk, first_time(journey("s14")))

    def test_locked_student_cannot_book(self):
        with self.assertRaises(DomainError):
            svc.book(user("rahul"), journey("s3").pk, now() + 2 * DAY)

    def test_race_second_student_gets_clear_error(self):
        a, b = journey("s1"), journey("s14")
        single = next(t for _, items in svc.available_times(a, now()) for t in items if t["seats"] == 1)
        svc.book(user("aarav"), a.pk, single["start"])
        with self.assertRaisesMessage(DomainError, "just taken"):
            svc.book(user("pooja"), b.pk, single["start"])
        b.refresh_from_db()
        self.assertEqual(b.state, M.ELIGIBLE)

    def test_database_refuses_double_booking(self):
        iv = Interview.objects.filter(status=Interview.SCHEDULED).first()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Interview.objects.create(journey=journey("s1"), slot=iv.slot, interviewer=iv.interviewer, start_at=iv.start_at,
                                     end_at=iv.end_at, attempt_no=1, booked_at=now())

    def test_daily_cap_respected(self):
        for j in Journey.objects.filter(state=M.ELIGIBLE, badge__track="AI Product Mastery").select_related("student__user"):
            try:
                svc.book(j.student.user, j.pk, first_time(j))
            except (DomainError, IndexError):
                pass
        for iv in svc.Interviewer.objects.all():
            per_day = {}
            for s in AvailabilitySlot.objects.filter(interviewer=iv, status=AvailabilitySlot.BOOKED):
                d = svc.day_start(s.start_at)
                per_day[d] = per_day.get(d, 0) + 1
            self.assertTrue(all(n <= iv.max_per_day for n in per_day.values()), iv.name)

    def test_reschedule_limit_and_release(self):
        j = journey("s1")
        times = [t["start"] for _, items in svc.available_times(j, now()) for t in items]
        iv = svc.book(user("aarav"), j.pk, times[0])
        old_slot = iv.slot_id
        svc.reschedule(user("aarav"), iv.pk, times[3])
        self.assertEqual(AvailabilitySlot.objects.get(pk=old_slot).status, AvailabilitySlot.OPEN)
        svc.reschedule(user("aarav"), iv.pk, times[5])
        with self.assertRaisesMessage(DomainError, "both reschedules"):
            svc.reschedule(user("aarav"), iv.pk, times[7])

    def test_cancel_cutoff(self):
        j = journey("s1")
        start = first_time(j)
        iv = svc.book(user("aarav"), j.pk, start)
        svc.advance_clock((start - now() - 3 * HOUR).total_seconds())
        with self.assertRaisesMessage(DomainError, "4 hours"):
            svc.cancel(user("aarav"), iv.pk)


class OutcomeTests(Base):
    def _iv(self, code):
        return Interview.objects.get(journey=journey(code), status=Interview.SCHEDULED)

    def test_only_assigned_interviewer_and_after_start(self):
        iv = self._iv("s9")
        with self.assertRaises(Forbidden):
            svc.record_outcome(user("rohit"), iv.pk, "attended", "pass", {"problem": 4, "concepts": 4, "build": 4, "comm": 4})
        with self.assertRaisesMessage(DomainError, "once the interview starts"):
            svc.record_outcome(user("vikram"), iv.pk, "attended", "pass", {"problem": 4, "concepts": 4, "build": 4, "comm": 4})

    def test_pass_awards_badge_miles_and_next_badge(self):
        s = Student.objects.get(code="s6")
        before = miles(s)
        svc.record_outcome(user("priya"), self._iv("s6").pk, "attended", "pass", {"problem": 4, "concepts": 4, "build": 4, "comm": 4})
        self.assertEqual(journey("s6").state, M.PASSED)
        self.assertEqual(miles(s), before + 500)
        self.assertTrue(rank_of(s)["qualiflyer"])
        self.assertTrue(Journey.objects.filter(student=s, badge_id="APM-SPE", state=M.LOCKED).exists())

    def test_retake_requires_feedback_and_cooldown_unlocks(self):
        iv = self._iv("s7")
        with self.assertRaisesMessage(DomainError, "at least 40"):
            svc.record_outcome(user("priya"), iv.pk, "attended", "retake", SCORES, "too short")
        svc.record_outcome(user("priya"), iv.pk, "attended", "retake", SCORES, FB)
        j = journey("s7")
        self.assertEqual((j.state, j.attempts_used), (M.RETAKE, 1))
        svc.advance_clock((j.cooldown_until - now()).total_seconds() + 60)
        self.assertEqual(journey("s7").state, M.ELIGIBLE)
        self.assertTrue(Notification.objects.filter(student__code="s7", kind="COOLDOWN_ENDED").exists())

    def test_internal_notes_never_reach_student_inbox(self):
        svc.record_outcome(user("priya"), self._iv("s7").pk, "attended", "retake", SCORES, FB, notes="SECRET-INTERNAL")
        self.client.force_login(user("arjun"))
        for url in ("student_home", "student_interview", "student_inbox"):
            self.assertNotContains(self.client.get(reverse(url)), "SECRET-INTERNAL")

    def test_no_show_grace_and_tech_issue(self):
        iv = self._iv("s2")
        with self.assertRaisesMessage(DomainError, "10 minutes"):
            svc.advance_clock((iv.start_at - now()).total_seconds() + 60)
            svc.record_outcome(user("priya"), iv.pk, "no_show")
        svc.record_outcome(user("priya"), iv.pk, "tech_issue", notes="audio dropped")
        j = journey("s2")
        self.assertEqual((j.state, j.attempts_used, j.priority), (M.ELIGIBLE, 0, True))

    def test_feedback_is_escaped_in_html(self):
        svc.record_outcome(user("priya"), self._iv("s7").pk, "attended", "retake", SCORES, "<script>alert(1)</script> " + FB)
        self.client.force_login(user("arjun"))
        r = self.client.get(reverse("student_home"))
        self.assertNotContains(r, "<script>alert(1)</script>")
        self.assertContains(r, "&lt;script&gt;")


class TeamTests(Base):
    def test_release_reassigns_or_cancels_with_priority(self):
        j = journey("s1")
        times = [t for _, items in svc.available_times(j, now()) for t in items]
        two = next(t for t in times if t["seats"] >= 2)
        iv = svc.book(user("aarav"), j.pk, two["start"])
        orig = iv.interviewer
        self.assertEqual(svc.release_interview(orig.user, iv.pk, "Interviewer unwell"), "reassigned")
        iv.refresh_from_db()
        self.assertNotEqual(iv.interviewer_id, orig.pk)
        self.assertEqual(journey("s1").state, M.SCHEDULED)

    def test_flag_on_passed_student_nullifies_badge(self):
        s = Student.objects.get(code="s4")
        before = miles(s)
        svc.raise_flag(user("ops"), journey("s4").pk, "Leaked solution used in UI Engineering L2")
        svc.resolve_flag(user("ops"), svc.IntegrityFlag.objects.filter(status="open", journey=journey("s4")).get().pk, "confirmed")
        self.assertEqual(journey("s4").state, M.REVOKED)
        self.assertEqual(miles(s), before - 500)
        self.assertFalse(rank_of(s)["qualiflyer"])

    def test_flag_releases_booking_and_dismiss_restores(self):
        j = journey("s9")
        slot = Interview.objects.get(journey=j, status=Interview.SCHEDULED).slot
        svc.raise_flag(user("ops"), j.pk, "Contest submission matched another student")
        slot.refresh_from_db()
        self.assertEqual(slot.status, AvailabilitySlot.OPEN)
        svc.resolve_flag(user("ops"), svc.IntegrityFlag.objects.get(journey=j, status="open").pk, "dismissed")
        self.assertEqual(journey("s9").state, M.ELIGIBLE)

    def test_only_ops_can_flag(self):
        with self.assertRaises(Forbidden):
            svc.raise_flag(user("priya"), journey("s4").pk, "Not allowed to do this at all")

    def test_extra_attempt_reopens_closed_journey(self):
        svc.grant_extra_attempt(user("ops"), journey("s22").pk)
        j = journey("s22")
        self.assertEqual((j.state, j.max_attempts), (M.ELIGIBLE, 4))

    def test_every_state_change_is_audited(self):
        j = journey("s1")
        n = JourneyEvent.objects.count()
        iv = svc.book(user("aarav"), j.pk, first_time(j))
        svc.cancel(user("aarav"), iv.pk)
        self.assertEqual(JourneyEvent.objects.count(), n + 2)


class PageTests(Base):
    PAGES = {
        "aarav": ["student_home", "student_book", "student_interview", "student_inbox", "compare", "how_it_works"],
        "diya": ["student_home", "student_interview"],
        "karthik": ["student_home", "student_interview"],
        "imran": ["student_home"], "rahul": ["student_home"], "riya": ["student_home"], "ishita": ["student_home"],
        "priya": ["iv_today", "iv_availability", "compare"],
        "ops": ["ops_overview", "ops_tracker", "ops_outcomes", "ops_integrity", "ops_flag_new", "ops_audit", "ops_export", "how_it_works"],
    }

    def test_every_page_renders_for_its_role(self):
        for username, pages in self.PAGES.items():
            self.client.force_login(user(username))
            for name in pages:
                r = self.client.get(reverse(name))
                self.assertEqual(r.status_code, 200, f"{username} → {name}")

    def test_roles_cannot_open_each_others_pages(self):
        self.client.force_login(user("aarav"))
        self.assertEqual(self.client.get(reverse("ops_tracker")).status_code, 403)
        self.assertEqual(self.client.get(reverse("iv_today")).status_code, 403)
        self.client.force_login(user("priya"))
        self.assertEqual(self.client.get(reverse("student_home")).status_code, 403)

    def test_anonymous_redirected_to_login(self):
        r = self.client.get(reverse("student_home"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])

    def test_booking_flow_over_http(self):
        self.client.force_login(user("aarav"))
        start = first_time(journey("s1"))
        r = self.client.post(reverse("student_confirm"), {"start": int(start.timestamp() * 1000), "ack": "on"}, follow=True)
        self.assertContains(r, "booked! Confirmation sent")
        self.assertEqual(journey("s1").state, M.SCHEDULED)

    def test_outcome_form_over_http(self):
        self.client.force_login(user("priya"))
        iv = Interview.objects.get(journey=journey("s7"), status=Interview.SCHEDULED)
        data = {"attendance": "attended", "result": "retake", "feedback": FB, "notes": "", **{f"score_{k}": v for k, v in SCORES.items()}}
        r = self.client.post(reverse("iv_outcome", args=[iv.pk]), data, follow=True)
        self.assertContains(r, "set to retake later")
        self.assertTrue(InterviewOutcome.objects.filter(interview=iv, result="retake").exists())

    def test_csrf_is_enforced(self):
        from django.test import Client

        c = Client(enforce_csrf_checks=True)
        c.force_login(user("aarav"))
        self.assertEqual(c.post(reverse("student_confirm"), {"start": 1, "ack": "on"}).status_code, 403)

    def test_all_app_views_are_class_based(self):
        from interviews.urls import urlpatterns

        function_views = [p.name for p in urlpatterns if not hasattr(p.callback, "view_class")]
        self.assertEqual(function_views, [], "every view must be a class-based view")

    def test_password_login_redirects_to_role_home(self):
        r = self.client.post(reverse("login"), {"username": "priya", "password": "grit-demo-2026"}, follow=True)
        self.assertEqual(r.redirect_chain[-1][0], reverse("iv_today"))



class CsrfTests(Base):
    """Reproduces '403 CSRF verification failed' on sign-out and checks the production origin setup."""
    HOST = "dipenmerchant.pythonanywhere.com"

    def _token(self, client, path):
        import re

        return re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', client.get(path).content.decode()).group(1)

    def _switch_with_stale_token(self):
        from django.test import Client

        c = Client(enforce_csrf_checks=True)
        c.get(reverse("demo_login", args=["aarav"]))
        stale = self._token(c, reverse("student_home"))     # page opened as Aarav…
        c.get(reverse("demo_login", args=["aarav"]))         # …then signed in again (token rotates)
        return c, stale

    def test_sign_out_works_from_a_stale_page(self):
        c, stale = self._switch_with_stale_token()
        r = c.post(reverse("logout"), {"csrfmiddlewaretoken": stale})
        self.assertRedirects(r, reverse("login"), fetch_redirect_response=False)

    def test_demo_buttons_work_from_a_stale_page(self):
        c, stale = self._switch_with_stale_token()
        r = c.post(reverse("demo_clock"), {"csrfmiddlewaretoken": stale, "seconds": "3600", "next": "/student/"})
        self.assertEqual(r.status_code, 302)

    def test_real_actions_still_reject_stale_tokens_with_friendly_page(self):
        c, stale = self._switch_with_stale_token()
        start = first_time(journey("s1"))
        r = c.post(reverse("student_confirm"), {"csrfmiddlewaretoken": stale, "start": int(start.timestamp() * 1000), "ack": "on"})
        self.assertContains(r, "This page has expired", status_code=403)
        self.assertEqual(journey("s1").state, M.ELIGIBLE, "a forged booking must not go through")

    def test_only_low_risk_views_are_csrf_exempt(self):
        from interviews.urls import urlpatterns

        exempt = sorted(p.name for p in urlpatterns if getattr(p.callback, "csrf_exempt", False))
        self.assertEqual(exempt, ["demo_clear_skill", "demo_clock", "demo_reset", "demo_skip_cooldown", "logout"])

    def _https_logout(self, forwarded_proto):
        from django.test import Client, override_settings

        extra = {"HTTP_X_FORWARDED_PROTO": "https"} if forwarded_proto else {}
        with override_settings(DEBUG=False, ALLOWED_HOSTS=[".pythonanywhere.com"], SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True,
                               SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"), SECURE_SSL_REDIRECT=False):
            c = Client(enforce_csrf_checks=True, HTTP_HOST=self.HOST, **extra)
            c.get(reverse("demo_login", args=["aarav"]), secure=True)
            tok = self._token(c, reverse("student_home"))
            return c.post(reverse("logout"), {"csrfmiddlewaretoken": tok}, secure=True,
                          HTTP_ORIGIN=f"https://{self.HOST}", HTTP_REFERER=f"https://{self.HOST}/student/")

    def test_https_sign_out_works_behind_proxy(self):
        self.assertEqual(self._https_logout(forwarded_proto=True).status_code, 302)

    def test_https_sign_out_works_without_proxy_header(self):
        # Even if the host doesn't send X-Forwarded-Proto, the default trusted origin covers *.pythonanywhere.com
        self.assertEqual(self._https_logout(forwarded_proto=False).status_code, 302)
