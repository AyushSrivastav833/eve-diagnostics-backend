from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import CentreTestViewSet, DiagnosticCentreViewSet, DiagnosticTestViewSet

router = DefaultRouter(trailing_slash=True)
router.include_root_view = False
router.register("centres", DiagnosticCentreViewSet, basename="centre")
router.register("tests", DiagnosticTestViewSet, basename="test")

offering_list = CentreTestViewSet.as_view({"get": "list", "post": "create"})
offering_detail = CentreTestViewSet.as_view(
    {"get": "retrieve", "put": "update", "patch": "partial_update", "delete": "destroy"}
)

urlpatterns = [
    path("centres/<int:centre_pk>/tests/", offering_list, name="centre-test-list"),
    path("centres/<int:centre_pk>/tests/<int:pk>/", offering_detail, name="centre-test-detail"),
    *router.urls,
]
