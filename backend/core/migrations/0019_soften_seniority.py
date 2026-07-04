"""
Adoucissement du malus d'ancienneté (retour utilisateur post-rééquilibrage).

Le seniority_malus creusait des scores très négatifs : −1 800 uniformes sur la
piscine, y compris pour des étudiants présents 0 jour sur 26. Décision :
  - max_malus −200 → −50 (soit ~−12,5/jour en semaine 4 au lieu de −150) ;
  - appliqué UNIQUEMENT aux présents du jour (code, apply_seniority) ;
  - appliqué à la clôture du jour (nightly), plus à 00h06 pour le jour à venir.
"""
from django.db import migrations
from django.utils import timezone

PARAMS = {"type": "linear_growth", "value_key": "weeks", "min": 0, "max": 4,
          "max_malus": -50}
LABEL = "Malus d'ancienneté (présents du jour, croît avec les semaines)"


def soften(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    RuleVersion = apps.get_model("core", "RuleVersion")
    rule = Rule.objects.filter(key="seniority_malus").first()
    if not rule:
        return
    if rule.label != LABEL:
        rule.label = LABEL
        rule.save(update_fields=["label"])
    now = timezone.now()
    cur = rule.versions.filter(valid_to__isnull=True).order_by("-version").first()
    if cur and cur.params == PARAMS:
        return
    if cur:
        cur.valid_to = now
        cur.save(update_fields=["valid_to"])
    RuleVersion.objects.create(rule=rule, version=(cur.version + 1) if cur else 1,
                               params=PARAMS, valid_from=now)


class Migration(migrations.Migration):
    dependencies = [("core", "0018_rubber_banding")]
    operations = [migrations.RunPython(soften, migrations.RunPython.noop)]
