from decimal import Decimal

from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from .models import CentreTest, DiagnosticCentre, DiagnosticTest


class DiagnosticTestSerializer(serializers.ModelSerializer):
    code = serializers.CharField(
        max_length=30,
        validators=[
            UniqueValidator(DiagnosticTest.objects.all(), lookup="iexact", message="Test code already exists.")
        ],
    )

    class Meta:
        model = DiagnosticTest
        fields = ["id", "code", "name", "description", "is_active"]

    def validate_code(self, value):
        return value.strip().upper()


class CentreTestSerializer(serializers.ModelSerializer):
    """A test offered by a centre, with that centre's price."""

    test = DiagnosticTestSerializer(read_only=True)
    test_id = serializers.PrimaryKeyRelatedField(
        source="test", queryset=DiagnosticTest.objects.filter(is_active=True), write_only=True
    )
    price = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))

    class Meta:
        model = CentreTest
        fields = ["id", "test", "test_id", "price", "is_available"]

    def validate(self, attrs):
        centre = self.context["centre"]
        test = attrs.get("test")
        if test is not None:
            duplicate = CentreTest.objects.filter(centre=centre, test=test)
            if self.instance is not None:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError({"test_id": ["This centre already offers this test."]})
        return attrs


class DiagnosticCentreSerializer(serializers.ModelSerializer):
    tests = serializers.SerializerMethodField()

    class Meta:
        model = DiagnosticCentre
        fields = ["id", "name", "address", "city", "pincode", "phone", "is_active", "tests"]
        validators = [
            serializers.UniqueTogetherValidator(
                queryset=DiagnosticCentre.objects.all(),
                fields=["name", "city"],
                message="A centre with this name already exists in this city.",
            )
        ]

    def get_tests(self, centre) -> list[dict]:
        # `available_offerings` is prefetched by the view to avoid N+1 queries.
        offerings = getattr(centre, "available_offerings", None)
        if offerings is None:
            offerings = centre.offerings.filter(is_available=True, test__is_active=True).select_related("test")
        return CentreTestSerializer(offerings, many=True, context=self.context).data
