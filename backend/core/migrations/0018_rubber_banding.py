"""
P5 — Rubber-banding (audit 07/2026).

- podium_tax / comeback_boost : le top 3 du classement (à J-1) rend pct % de
  ses gains DU JOUR, redistribués à parts égales aux 10 derniers (zéro-somme).
- plague_payout : 100 → 400 pts/personne (défaut modèle 300 → 400 aussi) pour
  que la fin de piscine Peste & Choléra puisse réellement renverser le podium.
"""
from decimal import Decimal

from django.db import migrations, models
from django.utils import timezone

NEW_RULES = [
    ("podium_tax", "loss", "Taxe du podium : top 3 du jour, % des gains",
     {"type": "from_context", "value_key": "points", "pct": 5, "top": 3, "bottom": 10}),
    ("comeback_boost", "gain", "Boost comeback : la taxe du podium, redistribuée",
     {"type": "from_context", "value_key": "points"}),
]


def seed(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    RuleVersion = apps.get_model("core", "RuleVersion")
    PoolConfig = apps.get_model("core", "PoolConfig")
    now = timezone.now()
    for key, category, label, params in NEW_RULES:
        rule, _ = Rule.objects.get_or_create(
            key=key, defaults={"category": category, "label": label,
                               "mult_min": Decimal("0.5"), "mult_max": Decimal("2.0")})
        if not rule.versions.exists():
            RuleVersion.objects.create(rule=rule, version=1, params=params, valid_from=now)
    # enjeu de fin de piscine : 400 pts/personne pour la coalition gagnante
    PoolConfig.objects.all().update(plague_payout=Decimal("400"))


class Migration(migrations.Migration):
    dependencies = [("core", "0017_reactivate_content")]
    operations = [
        migrations.AlterField(
            model_name="poolconfig", name="plague_payout",
            field=models.DecimalField(decimal_places=2, default=Decimal("400"),
                                      max_digits=10),
        ),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
