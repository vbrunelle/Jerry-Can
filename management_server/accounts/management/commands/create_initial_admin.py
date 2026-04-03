import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = 'Create the initial admin superuser from environment variables'

    def handle(self, *args, **options):
        password = os.environ.get('DJANGO_ADMIN_PASSWORD')
        if not password:
            self.stderr.write(self.style.ERROR(
                'DJANGO_ADMIN_PASSWORD environment variable is not set.'
            ))
            return

        if User.objects.filter(username='admin').exists():
            self.stdout.write(self.style.WARNING(
                'Admin user already exists. Skipping creation.'
            ))
            return

        User.objects.create_superuser(
            username='admin',
            password=password,
            role='admin',
            must_change_password=True,
        )
        self.stdout.write(self.style.SUCCESS(
            'Initial admin superuser created successfully.'
        ))
