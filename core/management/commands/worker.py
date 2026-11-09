from django.core.management.base import BaseCommand
from core.worker import main


class Command(BaseCommand):
    help = "Lance le worker de scan et de transcodage"

    def handle(self, *args, **options):
        main()
