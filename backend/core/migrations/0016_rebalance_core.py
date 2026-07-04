"""
P3 — Rééquilibrage du cœur de l'économie (audit 07/2026).

Crée de NOUVELLES RuleVersions (le passé figé n'est pas touché) pour :
  - assiduity_streak : plafonné à 7 jours (rente à intérêts composés → 53 %
    des gains totaux, écrasait tout le reste) ;
  - reconnect_same_pc : malus léger −20/−10 (taxait un comportement normal) ;
  - rush_malus : paliers via l'évaluateur `tiers` — note ≥ 50 : +50 (le rush
    devient risque/récompense), 1–49 : −50, note 0 : −150 (au lieu de −300) ;
  - midnight_bonus : paliers — ≥ 20 h de log : +100, ≥ 23h50 : +300 (au lieu
    d'un 500 binaire).

Params INLINE (pas d'import de rules_config) : la migration reste vraie même
si la config évolue encore. Les installs neuves reçoivent directement ces
valeurs via rules_config (seed 0006).
"""
from django.db import migrations
from django.utils import timezone

REBALANCE = {
    "assiduity_streak": {"type": "multiplier", "value_key": "streak", "factor": 10, "cap": 7},
    "reconnect_same_pc": {"type": "fixed", "min": -20, "max": -10},
    "rush_malus": {"type": "tiers", "value_key": "mark", "default": -150,
                   "tiers": {"0": -150, "1": -50, "50": 50}},
    "midnight_bonus": {"type": "tiers", "value_key": "minutes", "default": 0,
                       "tiers": {"1200": 100, "1430": 300}},
}

LABELS = {
    "assiduity_streak": "Assiduité : jours consécutifs (week-ends neutres, capé à 7 j)",
    "rush_malus": "Rush (paliers : ≥50 bonus, raté malus, 0 sanction)",
    "midnight_bonus": "Logtime quasi-plein (paliers : 20h / 23h50)",
}


def apply_rebalance(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    RuleVersion = apps.get_model("core", "RuleVersion")
    now = timezone.now()
    for key, params in REBALANCE.items():
        rule = Rule.objects.filter(key=key).first()
        if not rule:
            continue
        if key in LABELS and rule.label != LABELS[key]:
            rule.label = LABELS[key]
            rule.save(update_fields=["label"])
        cur = rule.versions.filter(valid_to__isnull=True).order_by("-version").first()
        if cur and cur.params == params:
            continue  # déjà à jour (install neuve seedée avec ces valeurs)
        if cur:
            cur.valid_to = now
            cur.save(update_fields=["valid_to"])
        RuleVersion.objects.create(rule=rule, version=(cur.version + 1) if cur else 1,
                                   params=params, valid_from=now)


class Migration(migrations.Migration):
    dependencies = [("core", "0015_dailydesignation")]
    operations = [migrations.RunPython(apply_rebalance, migrations.RunPython.noop)]
