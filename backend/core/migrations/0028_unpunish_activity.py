"""
Équilibrage « ne plus punir l'activité » (07/2026). Mesure : les 15 étudiants
les plus actifs (nb de projets rendus) plafonnaient à 26 pts de moyenne contre
214 de médiane piscine — chaque geste fort était taxé. Objectif : activité à
espérance ~neutre mais FORTE VARIANCE (l'actif lance plus de dés, il n'est pas
plus puni). Réappliqué à l'historique par le script de suivi.

1. project_perfect : −30 → −10 (le gag reste, il ne coule plus les bons).
2. project_random : ±(−100..120) → ±(−80..140) — EV ≈ +30/projet rendu, très
   bruité : compense le −10 du perfect en espérance, en pur aléa.
3. exam_time : malus max −300 → −150 (les forts restent finir l'exam).
4. aura_first_coalition : −1200..−800 → −300..−150 (aurait nuké le leader).
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
    r = Rule.objects.filter(key="project_perfect").first()
    if r:
        _revise(RuleVersion, r, {"type": "fixed", "points": -10}, now)
    r = Rule.objects.filter(key="project_random").first()
    if r:
        _revise(RuleVersion, r, {"type": "random_modifier", "base": 0,
                                 "rand_min": -80, "rand_max": 140}, now)
    r = Rule.objects.filter(key="exam_time").first()
    if r:
        _revise(RuleVersion, r, {"type": "linear_growth", "value_key": "minutes",
                                 "min": 0, "max": 240, "max_malus": -150}, now)
    r = Rule.objects.filter(key="aura_first_coalition").first()
    if r:
        _revise(RuleVersion, r, {"type": "fixed", "min": -300, "max": -150}, now)


class Migration(migrations.Migration):
    dependencies = [("core", "0027_compress_bottom")]
    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
