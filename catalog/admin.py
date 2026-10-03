from django.contrib import admin

from .models import CentreTest, DiagnosticCentre, DiagnosticTest


class CentreTestInline(admin.TabularInline):
    model = CentreTest
    extra = 0
    autocomplete_fields = ["test"]


@admin.register(DiagnosticCentre)
class DiagnosticCentreAdmin(admin.ModelAdmin):
    list_display = ["name", "city", "pincode", "is_active"]
    list_filter = ["city", "is_active"]
    search_fields = ["name", "address", "city"]
    inlines = [CentreTestInline]


@admin.register(DiagnosticTest)
class DiagnosticTestAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["code", "name"]
