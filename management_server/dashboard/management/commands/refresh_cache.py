import logging

from django.core.management.base import BaseCommand

from dashboard.services import refresh_inspection_cache

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Refresh the inspection cache from the latest data."

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help="Force refresh even when manual inspection mode is enabled.",
        )

    def handle(self, *args, **options):
        if not options.get('force'):
            try:
                from dashboard.models import SiteConfiguration
                config = SiteConfiguration.load()
                if config.manual_inspection_enabled:
                    self.stdout.write(
                        self.style.WARNING(
                            "Manual inspection mode is enabled — skipping automatic refresh. "
                            "Use --force to override."
                        )
                    )
                    return
            except Exception:
                logger.debug("Could not load SiteConfiguration; proceeding with refresh.")

        refresh_inspection_cache()
        self.stdout.write(self.style.SUCCESS("Inspection cache refreshed."))
