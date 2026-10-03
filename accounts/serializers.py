from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import RegexValidator
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User

phone_validator = RegexValidator(r"^\+?[0-9]{10,15}$", "Enter a valid phone number (10-15 digits, optional +).")


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "full_name", "phone", "date_joined"]
        read_only_fields = fields


class SignupSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False, style={"input_type": "password"})
    full_name = serializers.CharField(max_length=150, trim_whitespace=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, validators=[phone_validator])

    class Meta:
        model = User
        fields = ["email", "password", "full_name", "phone"]
        # The model-level unique validator is case-sensitive; validate_email handles it instead.
        extra_kwargs = {"email": {"validators": []}}

    def validate_email(self, value):
        email = value.strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("An account with this email already exists.", code="email_taken")
        return email

    def validate(self, attrs):
        # Run Django's password validators (length, common passwords, similarity to email/name...).
        candidate = User(email=attrs.get("email"), full_name=attrs.get("full_name", ""))
        try:
            password_validation.validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)}) from exc
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class AuthTokensSerializer(serializers.Serializer):
    """Response shape for signup/login (documentation only)."""

    access = serializers.CharField()
    refresh = serializers.CharField()
    user = UserSerializer()


class LoginSerializer(TokenObtainPairSerializer):
    """Email + password login returning a JWT pair and the user profile."""

    def validate(self, attrs):
        attrs[self.username_field] = attrs.get(self.username_field, "").strip().lower()
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


def tokens_for(user: User) -> dict:
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}
