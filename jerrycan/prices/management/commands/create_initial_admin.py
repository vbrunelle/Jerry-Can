import secrets
import string

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from prices.apps import ADJECTIVES, NOUNS


def _random_username():
    adj = secrets.choice(ADJECTIVES)
    noun = secrets.choice(NOUNS)
    num = secrets.randbelow(100)
    return f"{adj}{noun}{num}"


def _random_password():
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(16))


class Command(BaseCommand):
    help = 'Ensure an initial admin superuser exists and display credentials if not yet changed.'

    def handle(self, *args, **options):
        from prices.models import UserProfile

        admin = User.objects.filter(is_superuser=True).select_related('profile').first()

        if admin is None:
            username = _random_username()
            password = _random_password()
            admin = User.objects.create_superuser(username=username, email='', password=password)
            profile, _ = UserProfile.objects.get_or_create(user=admin)
            profile.must_change_password = True
            profile.temporary_password = password
            profile.save()
            self._show_credentials(admin.username, password)
            return

        profile = getattr(admin, 'profile', None)
        if profile is None or not profile.must_change_password:
            return

        if not profile.temporary_password:
            # Password was lost (e.g. created before temporary_password field existed) — reset it.
            password = _random_password()
            admin.set_password(password)
            admin.save()
            profile.temporary_password = password
            profile.save()

        self._show_credentials(admin.username, profile.temporary_password)

    def _show_credentials(self, username, password):
        border = '=' * 60
        self.stdout.write(self.style.WARNING(border))
        self.stdout.write(self.style.WARNING('  ⛽  Jerry Can — initial admin credentials'))
        self.stdout.write(self.style.WARNING(f'  Username : {username}'))
        self.stdout.write(self.style.WARNING(f'  Password : {password}'))
        self.stdout.write(self.style.WARNING('  Change your password on first login.'))
        self.stdout.write(self.style.WARNING(border))
