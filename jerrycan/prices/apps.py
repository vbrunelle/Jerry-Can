from django.apps import AppConfig
import os

from django.db.utils import OperationalError

ADJECTIVES = [
    "amber", "bold", "calm", "damp", "eager", "faint", "grand", "harsh",
    "idle", "jolly", "keen", "lush", "mild", "noble", "oval", "proud",
    "quick", "rapid", "sharp", "tall", "urban", "vivid", "warm", "young",
]

NOUNS = [
    "badger", "cedar", "dingo", "ember", "fjord", "gecko", "heron", "ibex",
    "jackal", "kiwi", "lemur", "maple", "newt", "otter", "panda", "quail",
    "raven", "stoat", "thorn", "umbra", "viper", "walrus", "xenon", "yak",
]


def _random_username():
    adj = secrets.choice(ADJECTIVES)
    noun = secrets.choice(NOUNS)
    num = secrets.randbelow(100)
    return f"{adj}{noun}{num}"


class PricesConfig(AppConfig):
    name = 'prices'

    def ready(self):
        from django.db.backends.signals import connection_created
        from django.utils import timezone

        def _set_sqlite_pragmas(sender, connection, **kwargs):
            """Set SQLite performance pragmas on every new connection."""
            if connection.vendor == 'sqlite':
                try:
                    with connection.cursor() as cur:
                        cur.execute('PRAGMA journal_mode=WAL')
                        cur.execute('PRAGMA synchronous=NORMAL')
                        cur.execute('PRAGMA temp_store=MEMORY')
                        cur.execute('PRAGMA cache_size=-65536')  # 64 MB
                        cur.execute('PRAGMA busy_timeout=30000')
                except OperationalError:
                    # If the database is briefly locked during connection setup,
                    # let Django retry queries using connection timeout/busy_timeout.
                    pass

        connection_created.connect(_set_sqlite_pragmas)

        import sys
        # Start background threads when running the dev server or gunicorn.
        # Excluded: migrate, shell, test, and any other management commands.
        is_runserver = 'runserver' in sys.argv
        is_gunicorn = 'gunicorn' in sys.modules
        if is_runserver and os.environ.get('RUN_MAIN') != 'true':
            return
        if not (is_runserver or is_gunicorn):
            return
        from .models import Analysis, AnalysisTransferTask

        interrupted_tasks = AnalysisTransferTask.objects.filter(
            status__in=[
                AnalysisTransferTask.Status.PENDING,
                AnalysisTransferTask.Status.RUNNING,
            ]
        )
        for task in interrupted_tasks:
            task.status = (
                AnalysisTransferTask.Status.CANCELLED
                if task.cancel_requested
                else AnalysisTransferTask.Status.ERROR
            )
            task.status_detail = (
                'Task cancelled during server reload'
                if task.cancel_requested
                else 'Task interrupted by server reload'
            )
            if not task.error_message and not task.cancel_requested:
                task.error_message = 'The server restarted before this background task finished.'
            task.completed_at = timezone.now()
            task.activity_log = (
                f"{task.activity_log}\n[{timezone.localtime().strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{task.status_detail}"
            ).strip()
            task.save(update_fields=[
                'status', 'status_detail', 'error_message', 'completed_at', 'activity_log'
            ])

        for analysis in Analysis.objects.filter(active=True, run_automatically=True):
            analysis.run_analysis()
