from django.core.management.base import BaseCommand

from payments.services import reprocess_stuck_webhook_events


class Command(BaseCommand):
    help = "Retry webhook events that failed or never finished processing."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--min-age", type=int, default=60, help="Only RECEIVED events older than N seconds")

    def handle(self, *args, limit, min_age, **options):
        summary = reprocess_stuck_webhook_events(limit=limit, min_age_seconds=min_age)
        self.stdout.write(self.style.SUCCESS(f"Reprocessed webhook events: {summary}"))
