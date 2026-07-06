"""
Équilibrage & refonte Logtime (07/2026).

1. project_perfect : bonus 150 → 30 (÷5) — ne casse plus le classement.
2. randominette : RETIRÉE (is_active=False) — aléa ±150 trop violent.
3. seniority_malus : RETIRÉ (is_active=False) — perte passive punitive.
4. Logtime :
   - logtime_high : malus croissant → malus FIXE -200 au-delà de 14 h.
   - logtime_minute : NOUVELLE règle — jackpot +1000 si le logtime du jour
     vaut 1 min pile.

Nouvelles RuleVersions (le passé figé n'est pas touché) ; params inline pour
rester indépendant de rules_config. Les events randominette/seniority déjà
écrits sont annulés + recompute par un script de suivi (pas dans la migration).
"""
from decimal import Decimal

from django.db import migrations
from django.utils import timezone


def _revise(RuleVersion, rule, params, now):
    """Clôt la version courante et en crée une nouvelle si les params changent."""
    cur = rule.versions.filter(valid_to__isnull=True).order_by("-version").first()
    if cur and cur.params == params:
        return
    if cur:
        cur.valid_to = now
        cur.save(update_fields=["valid_to"])
    RuleVersion.objects.create(rule=rule, version=(cur.version + 1) if cur else 1,
                               params=params, valid_from=now)


def apply(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    RuleVersion = apps.get_model("core", "RuleVersion")
    now = timezone.now()

    # 1. nerf du bonus 100 %
    r = Rule.objects.filter(key="project_perfect").first()
    if r:
        r.label = "Projet validé pile à 100 (bonus nerfé)"
        r.save(update_fields=["label"])
        _revise(RuleVersion, r, {"type": "fixed", "points": 30}, now)

    # 2 & 3. retrait de randominette et du malus d'ancienneté (historique conservé)
    Rule.objects.filter(key__in=["randominette", "seniority_malus"]).update(is_active=False)

    # 4. logtime_high → malus fixe
    r = Rule.objects.filter(key="logtime_high").first()
    if r:
        r.label = "Logtime > 14h (malus fixe du jour)"
        r.save(update_fields=["label"])
        _revise(RuleVersion, r, {"type": "fixed", "points": -200}, now)

    # 4. jackpot de la minute (nouvelle règle)
    r, _ = Rule.objects.get_or_create(
        key="logtime_minute",
        defaults={"category": "gain",
                  "label": "Jackpot de la minute (logtime du jour = 1 min pile)",
                  "mult_min": Decimal("0.5"), "mult_max": Decimal("2.0")})
    if not r.versions.exists():
        RuleVersion.objects.create(rule=r, version=1, valid_from=now,
                                   params={"type": "fixed", "points": 1000})


class Migration(migrations.Migration):
    dependencies = [("core", "0019_soften_seniority")]
    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
