"""
Refonte examens (07/2026).

1. exam_time : n'est plus un GAIN (« temps passé = points ») mais un MALUS —
   plus tu passes de temps en examen, plus tu perds (jusqu'à -300 à 240 min+).
   Passe en catégorie loss ; ré-évalué déterministe sur le temps déjà stocké.
2. exam_regression : NOUVELLE règle — à chaque examen, si ton score (classement)
   est inférieur à celui du précédent examen, malus fixe. Faute de notes 42
   d'examen exposées, on compare le score du leaderboard entre jalons d'examen.
"""
from django.db import migrations
from django.utils import timezone


def _revise(RuleVersion, rule, params, now):
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

    # 1. exam_time → malus
    r = Rule.objects.filter(key="exam_time").first()
    if r:
        r.label = "Temps passé en examen (+ de temps = malus)"
        r.category = "loss"
        r.save(update_fields=["label", "category"])
        _revise(RuleVersion, r, {"type": "linear_growth", "value_key": "minutes",
                                 "min": 0, "max": 240, "max_malus": -300}, now)

    # 2. exam_regression (nouvelle règle)
    r, _ = Rule.objects.get_or_create(
        key="exam_regression",
        defaults={"category": "loss",
                  "label": "Score en baisse depuis l'exam précédent (malus)",
                  "mult_min": 1, "mult_max": 1},
    )
    if not r.versions.exists():
        RuleVersion.objects.create(rule=r, version=1,
                                   params={"type": "fixed", "points": -150}, valid_from=now)


def revert(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    Rule.objects.filter(key="exam_regression").update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [("core", "0024_rebalance_audit")]
    operations = [migrations.RunPython(apply, revert)]
