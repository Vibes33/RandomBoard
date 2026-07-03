"""
Seed des règles ajoutées après 0006 (assiduity_streak, project_perfect,
cluster_bonus…). Même logique idempotente que 0006 : crée toute règle de
rules_config absente, avec sa version 1 et son range de multiplicateur par
défaut. Sur une base fraîche (0006 a déjà tout créé) → no-op.
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
        if not created and rule.mult_min == Decimal("1") and rule.mult_max == Decimal("1"):
            rule.mult_min, rule.mult_max = DEFAULT_MULT_MIN, DEFAULT_MULT_MAX
            rule.save(update_fields=["mult_min", "mult_max"])
        if not rule.versions.exists():
            RuleVersion.objects.create(rule=rule, version=1, params=params, valid_from=now)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_deactivate_dead_rules"),
    ]

    operations = [
        migrations.RunPython(seed_rules, migrations.RunPython.noop),
    ]
