"""
Seed des règles = configuration figée.

Crée les règles (et leur version 1) définies dans core.rules_config si elles
n'existent pas encore. Idempotent : sur une base déjà peuplée, ne touche à rien.
Résultat : après un `migrate`, les 26 règles sont présentes sans seed manuel.
"""
from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def seed_rules(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    RuleVersion = apps.get_model("core", "RuleVersion")
    from core.rules_config import RULES, DEFAULT_MULT_MIN, DEFAULT_MULT_MAX

    now = timezone.now()
    for key, category, label, params in RULES:
        rule, created = Rule.objects.get_or_create(
            key=key,
            defaults={
                "category": category, "label": label,
                "mult_min": DEFAULT_MULT_MIN, "mult_max": DEFAULT_MULT_MAX,
            },
        )
        # Range par défaut si la règle existait sans multiplicateur configuré.
        if not created and rule.mult_min == Decimal("1") and rule.mult_max == Decimal("1"):
            rule.mult_min, rule.mult_max = DEFAULT_MULT_MIN, DEFAULT_MULT_MAX
            rule.save(update_fields=["mult_min", "mult_max"])

        # Version 1 seulement si la règle n'a encore aucune version (ne réécrit
        # jamais une config déjà éditée en prod).
        if not rule.versions.exists():
            RuleVersion.objects.create(
                rule=rule, version=1, params=params, valid_from=now,
            )


def unseed_rules(apps, schema_editor):
    # Volontairement non destructif : on ne supprime pas les règles au rollback.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_dailyhost_workstation"),
    ]

    operations = [
        migrations.RunPython(seed_rules, unseed_rules),
    ]
