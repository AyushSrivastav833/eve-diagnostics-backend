from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import PaymentViewSet, PaymentWebhookView

router = DefaultRouter()
router.include_root_view = False
router.register("", PaymentViewSet, basename="payment")

urlpatterns = [
    # Must precede the router so "webhook" isn't captured as a payment ID.
    path("webhook/", PaymentWebhookView.as_view(), name="payment-webhook"),
    *router.urls,
]
