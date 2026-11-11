from django.core.management.base import BaseCommand

from interviews.services import run_system


class Command(BaseCommand):
    help = "Run time-based jobs (slot ended, cooldown ended, roll availability). Schedule every minute via cron."

    def handle(self, *args, **opts):
        run_system()
        self.stdout.write("Jobs complete.")
