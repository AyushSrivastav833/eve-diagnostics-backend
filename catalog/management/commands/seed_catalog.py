import os
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import CentreTest, DiagnosticCentre, DiagnosticTest

TESTS = [
    ("CBC", "Complete Blood Count", "Measures red cells, white cells, haemoglobin and platelets."),
    ("LFT", "Liver Function Test", "Panel of enzymes and proteins that reflect liver health."),
    ("KFT", "Kidney Function Test", "Urea, creatinine and electrolytes."),
    ("TSH", "Thyroid Stimulating Hormone", "Screens for thyroid disorders."),
    ("HBA1C", "HbA1c", "Average blood sugar over the last 3 months."),
    ("LIPID", "Lipid Profile", "Cholesterol (HDL, LDL) and triglycerides."),
    ("VITD", "Vitamin D (25-OH)", "Vitamin D levels in blood."),
    ("XRAY-CHEST", "Chest X-Ray (PA view)", "Radiograph of the chest."),
]

CENTRES = [
    {
        "name": "EVE Diagnostics - MG Marg",
        "address": "12 MG Marg",
        "city": "Gangtok",
        "pincode": "737101",
        "phone": "+913592200001",
        "prices": {"CBC": "350", "LFT": "750", "KFT": "700", "TSH": "450", "HBA1C": "550", "LIPID": "650"},
    },
    {
        "name": "EVE Diagnostics - Indiranagar",
        "address": "100 Feet Road, Indiranagar",
        "city": "Bengaluru",
        "pincode": "560038",
        "phone": "+918040000002",
        "prices": {"CBC": "399", "LFT": "899", "TSH": "499", "VITD": "1299", "XRAY-CHEST": "600", "LIPID": "699"},
    },
    {
        "name": "EVE Diagnostics - Saket",
        "address": "Press Enclave Road, Saket",
        "city": "New Delhi",
        "pincode": "110017",
        "phone": "+911140000003",
        "prices": {"CBC": "375", "KFT": "725", "HBA1C": "575", "VITD": "1199", "XRAY-CHEST": "550"},
    },
]


class Command(BaseCommand):
    help = "Load demo diagnostic centres, tests and prices (safe to run repeatedly)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--with-users",
            action="store_true",
            help="Also create a staff user and a patient user (credentials from SEED_* env vars or defaults).",
        )

    @transaction.atomic
    def handle(self, *args, with_users=False, **options):
        tests = {}
        for code, name, description in TESTS:
            tests[code], _ = DiagnosticTest.objects.update_or_create(
                code=code, defaults={"name": name, "description": description, "is_active": True}
            )

        offerings = 0
        for spec in CENTRES:
            centre, _ = DiagnosticCentre.objects.update_or_create(
                name=spec["name"],
                city=spec["city"],
                defaults={
                    "address": spec["address"],
                    "pincode": spec["pincode"],
                    "phone": spec["phone"],
                    "is_active": True,
                },
            )
            for code, price in spec["prices"].items():
                CentreTest.objects.update_or_create(
                    centre=centre, test=tests[code], defaults={"price": Decimal(price), "is_available": True}
                )
                offerings += 1

        self.stdout.write(
            self.style.SUCCESS(f"Seeded {len(TESTS)} tests, {len(CENTRES)} centres, {offerings} offerings.")
        )

        if with_users:
            self._create_users()

    def _create_users(self):
        User = get_user_model()
        users = [
            (
                os.environ.get("SEED_ADMIN_EMAIL", "admin@eve.local"),
                os.environ.get("SEED_ADMIN_PASSWORD", "Admin@12345"),
                "EVE Admin",
                True,
            ),
            (
                os.environ.get("SEED_PATIENT_EMAIL", "patient@eve.local"),
                os.environ.get("SEED_PATIENT_PASSWORD", "Patient@12345"),
                "Demo Patient",
                False,
            ),
        ]
        for email, password, name, is_staff in users:
            user, created = User.objects.get_or_create(
                email=email, defaults={"full_name": name, "is_staff": is_staff, "is_superuser": is_staff}
            )
            if created:
                user.set_password(password)
                user.save()
            self.stdout.write(
                f"{'Created' if created else 'Exists '} {'staff  ' if is_staff else 'patient'} user: {email}"
            )
