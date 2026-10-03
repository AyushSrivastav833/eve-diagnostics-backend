from django.contrib import admin

from .models import Booking


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "test", "centre", "appointment_at", "amount", "status", "created_at"]
    list_filter = ["status", "centre__city"]
    search_fields = ["id", "user__email", "test__code", "centre__name"]
    readonly_fields = ["id", "amount", "currency", "status", "created_at", "updated_at", "cancelled_at"]
    raw_id_fields = ["user", "centre", "test"]
