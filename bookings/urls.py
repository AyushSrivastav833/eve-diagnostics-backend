from rest_framework.routers import DefaultRouter

from .views import BookingViewSet

router = DefaultRouter()
router.include_root_view = False
router.register("", BookingViewSet, basename="booking")

urlpatterns = router.urls
