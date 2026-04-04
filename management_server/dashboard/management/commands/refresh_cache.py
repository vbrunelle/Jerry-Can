from django.core.management.base import BaseCommand

from dashboard.services import refresh_inspection_cache


class Command(BaseCommand):
    help = "Refresh the inspection cache from the latest data."

    def handle(self, *args, **options):
        refresh_inspection_cache()
        self.stdout.write(self.style.SUCCESS("Inspection cache refreshed."))
