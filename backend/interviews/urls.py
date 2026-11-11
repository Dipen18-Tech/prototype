from django.urls import path

from . import views as v

urlpatterns = [
    path("", v.HomeView.as_view(), name="home"),
    path("login/", v.SignInView.as_view(), name="login"),
    path("logout/", v.SignOutView.as_view(), name="logout"),
    # demo-only (404 when GRIT_DEMO_MODE=0)
    path("demo/login/<str:username>/", v.DemoLoginView.as_view(), name="demo_login"),
    path("demo/switch/", v.DemoSwitchView.as_view(), name="demo_switch"),
    path("demo/clock/", v.DemoClockView.as_view(), name="demo_clock"),
    path("demo/reset/", v.DemoResetView.as_view(), name="demo_reset"),
    path("demo/clear-skill/", v.DemoClearSkillView.as_view(), name="demo_clear_skill"),
    path("demo/skip-cooldown/", v.DemoSkipCooldownView.as_view(), name="demo_skip_cooldown"),
    # student
    path("student/", v.StudentHomeView.as_view(), name="student_home"),
    path("student/book/", v.StudentBookView.as_view(), name="student_book"),
    path("student/book/confirm/", v.StudentConfirmView.as_view(), name="student_confirm"),
    path("student/interview/", v.StudentInterviewView.as_view(), name="student_interview"),
    path("student/interview/<int:pk>/cancel/", v.StudentCancelView.as_view(), name="student_cancel"),
    path("student/interview/<int:pk>/join/", v.StudentJoinView.as_view(), name="student_join"),
    path("student/inbox/", v.StudentInboxView.as_view(), name="student_inbox"),
    # interviewer
    path("interviewer/", v.InterviewerTodayView.as_view(), name="iv_today"),
    path("interviewer/interviews/<int:pk>/outcome/", v.InterviewerOutcomeView.as_view(), name="iv_outcome"),
    path("interviewer/interviews/<int:pk>/release/", v.ReleaseInterviewView.as_view(), name="iv_release"),
    path("interviewer/availability/", v.AvailabilityView.as_view(), name="iv_availability"),
    path("interviewer/availability/block/", v.BlockDayView.as_view(), name="iv_block_day"),
    # interview ops
    path("ops/", v.OpsOverviewView.as_view(), name="ops_overview"),
    path("ops/tracker/", v.OpsTrackerView.as_view(), name="ops_tracker"),
    path("ops/outcomes/", v.OpsOutcomesView.as_view(), name="ops_outcomes"),
    path("ops/integrity/", v.OpsIntegrityView.as_view(), name="ops_integrity"),
    path("ops/flags/new/", v.OpsFlagCreateView.as_view(), name="ops_flag_new"),
    path("ops/journeys/<int:pk>/extra-attempt/", v.OpsExtraAttemptView.as_view(), name="ops_extra_attempt"),
    path("ops/audit/", v.OpsAuditView.as_view(), name="ops_audit"),
    path("ops/export.csv", v.OpsExportView.as_view(), name="ops_export"),
    # shared
    path("before-after/", v.CompareView.as_view(), name="compare"),
    path("how-it-works/", v.HowItWorksView.as_view(), name="how_it_works"),
]
