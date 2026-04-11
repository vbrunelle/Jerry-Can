import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class PricesConfig(AppConfig):
    name = 'prices'

    def ready(self):
        from django.db.models.signals import post_migrate
        post_migrate.connect(_create_initial_admin, sender=self)


def _create_initial_admin(sender, **kwargs):
    import secrets
    import string

    from django.contrib.auth.models import User

    if User.objects.filter(is_superuser=True).exists():
        return

    alphabet = string.ascii_letters + string.digits
    username = 'admin_' + ''.join(secrets.choice(string.ascii_lowercase) for _ in range(6))
    password = ''.join(secrets.choice(alphabet) for _ in range(16))

    user = User.objects.create_superuser(username=username, email='', password=password)

    try:
        from prices.models import UserProfile
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.must_change_password = True
        profile.save()
    except Exception:
        pass

    logger.warning(
        "\n"
        "=" * 60 + "\n"
        "  Initial admin account created\n"
        "  Username : %s\n"
        "  Password : %s\n"
        "  You will be asked to change the password on first login.\n"
        "=" * 60,
        username,
        password,
    )
