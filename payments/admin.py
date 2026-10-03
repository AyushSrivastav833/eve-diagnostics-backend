from django.contrib import admin

from .models import Payment, WebhookEvent


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ["provider_reference", "booking", "amount", "status", "needs_refund", "created_at"]
    list_filter = ["status", "needs_refund", "provider"]
    search_fields = ["provider_reference", "booking__id", "user__email"]
    readonly_fields = [f.name for f in Payment._meta.fields]


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ["event_id", "event_type", "payment_reference", "status", "attempts", "received_at"]
    list_filter = ["status", "event_type"]
    search_fields = ["event_id", "payment_reference"]
    readonly_fields = [f.name for f in WebhookEvent._meta.fields]
