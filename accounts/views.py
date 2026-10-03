import logging

from django.db import IntegrityError
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .serializers import AuthTokensSerializer, LoginSerializer, SignupSerializer, UserSerializer, tokens_for

logger = logging.getLogger(__name__)


class SignupView(generics.GenericAPIView):
    """Register a new patient account and return a JWT pair."""

    serializer_class = SignupSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "auth"

    @extend_schema(responses={201: AuthTokensSerializer})
    def post(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            user = serializer.save()
        except IntegrityError as exc:
            # Two concurrent signups with the same email: the DB constraint wins.
            raise ValidationError(
                {"email": ["An account with this email already exists."]}, code="email_taken"
            ) from exc
        logger.info("user_signed_up", extra={"user_id": user.id})
        return Response({**tokens_for(user), "user": UserSerializer(user).data}, status=status.HTTP_201_CREATED)


class LoginView(TokenObtainPairView):
    """Exchange email + password for an access/refresh token pair."""

    serializer_class = LoginSerializer
    throttle_scope = "auth"


class RefreshView(TokenRefreshView):
    """Exchange a refresh token for a new access token."""

    throttle_scope = "auth"


class MeView(generics.RetrieveAPIView):
    """Profile of the authenticated user."""

    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user
