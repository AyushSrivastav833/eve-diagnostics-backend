from celery import shared_task

from .services import process_webhook_event, reprocess_stuck_webhook_events, schedule_webhook_retry


@shared_task(name="payments.tasks.process_webhook_event", acks_late=True)
def process_webhook_event_task(event_pk: int) -> None:
    try:
        process_webhook_event(event_pk)
    except Exception:
        # Failure is already recorded on the event; back off and try again (bounded by WEBHOOK_MAX_ATTEMPTS).
        schedule_webhook_retry(event_pk)


@shared_task(name="payments.tasks.reprocess_failed_webhook_events")
def reprocess_failed_webhook_events(limit: int = 100) -> dict:
    return reprocess_stuck_webhook_events(limit=limit)
