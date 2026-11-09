import os
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Crée l'utilisateur admin depuis ADMIN_USER / ADMIN_PASSWORD s'il n'existe pas"

    def handle(self, *args, **options):
        user, pw = os.environ.get("ADMIN_USER"), os.environ.get("ADMIN_PASSWORD")
        if not user or not pw:
            return
        User = get_user_model()
        if not User.objects.filter(username=user).exists():
            User.objects.create_superuser(username=user, password=pw)
            self.stdout.write(f"Admin '{user}' créé")
