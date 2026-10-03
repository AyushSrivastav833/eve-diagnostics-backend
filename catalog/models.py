from decimal import Decimal

from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class DiagnosticCentre(TimeStampedModel):
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    city = models.CharField(max_length=100, db_index=True)
    pincode = models.CharField(max_length=10)
    phone = models.CharField(max_length=20, blank=True)
    # Centres are never hard-deleted because bookings reference them.
    is_active = models.BooleanField(default=True)
    tests = models.ManyToManyField("DiagnosticTest", through="CentreTest", related_name="centres")

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["name", "city"], name="unique_centre_name_per_city")]

    def __str__(self):
        return f"{self.name} ({self.city})"


class DiagnosticTest(TimeStampedModel):
    """A test from the global catalogue (e.g. CBC). Prices are set per centre via CentreTest."""

    code = models.CharField(max_length=30, unique=True, help_text="Short unique code, e.g. CBC")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.code} - {self.name}"

    def save(self, *args, **kwargs):
        self.code = self.code.strip().upper()
        super().save(*args, **kwargs)


class CentreTest(TimeStampedModel):
    """A test offered by a specific centre at a specific price."""

    centre = models.ForeignKey(DiagnosticCentre, on_delete=models.CASCADE, related_name="offerings")
    test = models.ForeignKey(DiagnosticTest, on_delete=models.CASCADE, related_name="offerings")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ["test__name"]
        constraints = [
            models.UniqueConstraint(fields=["centre", "test"], name="unique_test_per_centre"),
            models.CheckConstraint(condition=models.Q(price__gt=Decimal("0")), name="centre_test_price_positive"),
        ]

    def __str__(self):
        return f"{self.test.code} @ {self.centre.name}: {self.price}"
