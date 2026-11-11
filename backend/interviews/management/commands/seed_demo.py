from django.core.management.base import BaseCommand

from interviews.models import Student
from interviews.seed import DEMO_PASSWORD, reset_demo


class Command(BaseCommand):
    help = "Wipe and recreate the demo data (students, interviewers, ops user, bookings)."

    def add_arguments(self, parser):
        parser.add_argument("--if-empty", action="store_true", help="Only seed when there is no data yet (safe for deploy scripts).")

    def handle(self, *args, if_empty=False, **opts):
        if if_empty and Student.objects.exists():
            self.stdout.write("Data already present — skipping seed.")
            return
        reset_demo()
        self.stdout.write(self.style.SUCCESS(f"Demo data ready. Log in as aarav / priya / ops with password '{DEMO_PASSWORD}'."))
