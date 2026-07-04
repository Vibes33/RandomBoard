"""
P4 — Réactivation du contenu muet (audit 07/2026).

- randominette : réactivée, recalibrée (+150 / −25) — comeback quotidien pour
  la moitié basse du classement (câblée dans daily_derived + rejeu).
- exam_time : réactivée — temps passé en examen via overlap locations/fenêtres.
- last_day_corrections : réactivée, factor 1000 → 100 (points par correction
  donnée le dernier jour).
- daily_blessed / daily_cursed : NOUVELLES règles — les élus du jour
  (DailyDesignation) reçoivent enfin un effet réel (± pct % des gains du jour).
- cluster_bonus : map par défaut (c1 +15 / c2 0 / c3 −10) si non configurée.

Params inline (indépendants de rules_config). Installs neuves : seed 0006 +
INACTIVE_RULES mis à jour produisent le même état.
"""
from decimal import Decimal

from django.db import migrations
from django.utils import timezone

REACTIVATE = {
    "randominette": {"type": "probability", "proba": 0.5, "points": 150, "else_points": -25},
    "exam_time": None,        # params inchangés, juste is_active=True
    "last_day_corrections": {"type": "multiplier", "value_key": "correction_points",
                             "factor": 100},
}

NEW_RULES = [
    ("daily_blessed", "gain", "Béni du jour : bonus % des gains du jour",
     {"type": "from_context", "value_key": "points", "pct": 30}),
    ("daily_cursed", "loss", "Maudit du jour : malus % des gains du jour",
     {"type": "from_context", "value_key": "points", "pct": 30}),
]

CLUSTER_DEFAULT = {"type": "map_lookup", "key_field": "cluster", "default": 0,
                   "map": {"c1": 15, "c2": 0, "c3": -10}}

LABELS = {
    "randominette": "Randominette (pile ou face, moitié basse du classement)",
    "last_day_corrections": "Dernier jour : points par correction donnée",
}


def _new_version(RuleVersion, rule, params, now):
    cur = rule.versions.filter(valid_to__isnull=True).order_by("-version").first()
    if cur and cur.params == params:
        return
    if cur:
        cur.valid_to = now
        cur.save(update_fields=["valid_to"])
    RuleVersion.objects.create(rule=rule, version=(cur.version + 1) if cur else 1,
                               params=params, valid_from=now)


def reactivate(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    RuleVersion = apps.get_model("core", "RuleVersion")
    now = timezone.now()

    for key, params in REACTIVATE.items():
        rule = Rule.objects.filter(key=key).first()
        if not rule:
            continue
        rule.is_active = True
        if key in LABELS:
            rule.label = LABELS[key]
        rule.save(update_fields=["is_active", "label"])
        if params:
            _new_version(RuleVersion, rule, params, now)

    for key, category, label, params in NEW_RULES:
        rule, created = Rule.objects.get_or_create(
            key=key, defaults={"category": category, "label": label,
                               "mult_min": Decimal("0.5"), "mult_max": Decimal("2.0")})
        if not rule.versions.exists():
            RuleVersion.objects.create(rule=rule, version=1, params=params, valid_from=now)

    # cluster_bonus : map par défaut UNIQUEMENT si vide (ne pas écraser un réglage staff)
    rule = Rule.objects.filter(key="cluster_bonus").first()
    if rule:
        cur = rule.versions.filter(valid_to__isnull=True).order_by("-version").first()
        if cur and not (cur.params or {}).get("map"):
            _new_version(RuleVersion, rule, CLUSTER_DEFAULT, now)


class Migration(migrations.Migration):
    dependencies = [("core", "0016_rebalance_core")]
    operations = [migrations.RunPython(reactivate, migrations.RunPython.noop)]
