from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .cache import invalidate_catalog_cache
from .models import CentreTest, DiagnosticCentre, DiagnosticTest


@receiver([post_save, post_delete], sender=DiagnosticCentre)
@receiver([post_save, post_delete], sender=DiagnosticTest)
@receiver([post_save, post_delete], sender=CentreTest)
def on_catalog_change(sender, **kwargs):
    invalidate_catalog_cache()
