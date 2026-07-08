"""
Trolls (07/2026) — intégration du Google Sheet des trolls (Apps Script).

Nouvelle règle `troll_victim` : se faire troller son PC = malus, points PAR
NIVEAU de troll (map éditable : 1 → -10, 2 → -20, 3 → -30) multipliés par le
multiplicateur de la SEMAINE de piscine (map 1..4, éditable au panel).
L'ingestion vit dans sync.fetch_trolls/sync_trolls (poll + replay), l'URL et
le mot de passe du sheet dans .env.
"""
from django.db import migrations
from django.utils import timezone


PARAMS = {"type": "level_week", "level_key": "level", "default": 0,
          "levels": {"1": -10, "2": -20, "3": -30},
          "week_mult": {"1": 1, "2": 1, "3": 1, "4": 1}}


def apply(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    RuleVersion = apps.get_model("core", "RuleVersion")
    r, _ = Rule.objects.get_or_create(
        key="troll_victim",
        defaults={"category": "loss",
                  "label": "Trollé sur son PC (points par niveau × semaine)",
                  "mult_min": 1, "mult_max": 1},
    )
    if not r.versions.exists():
        RuleVersion.objects.create(rule=r, version=1, params=PARAMS,
                                   valid_from=timezone.now())


def revert(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    Rule.objects.filter(key="troll_victim").update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("core", "0029_appuser_is_banned")]
    operations = [migrations.RunPython(apply, revert)]
