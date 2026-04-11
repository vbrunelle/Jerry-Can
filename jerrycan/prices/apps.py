from django.apps import AppConfig

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
        import sys
        # Only start background threads when actually running the development server.
        # Excluded: migrate, shell, test, and any other management commands.
        if 'runserver' not in sys.argv:
            return
        from .models import Analysis
        for analysis in Analysis.objects.filter(active=True, run_automatically=True):
            analysis.run_analysis()
