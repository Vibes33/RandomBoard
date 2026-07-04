"""
midnight_bonus : bascule de « heure de connexion 23:50–23:59 » vers « logtime
total du jour ∈ [23h50, 23h59] ». On met à jour les params de la version
courante (les events passés gardent leur raw_points figé → historique intact).
Idempotent.
"""
from django.db import migrations

NEW_PARAMS = {"type": "fixed", "points": 500}
NEW_LABEL = "Logtime quasi-plein (23h50–23h59 sur la journée)"


def migrate(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    RuleVersion = apps.get_model("core", "RuleVersion")
    rule = Rule.objects.filter(key="midnight_bonus").first()
    if not rule:
        return
    rule.label = NEW_LABEL
    rule.save(update_fields=["label"])
    cur = rule.versions.filter(valid_to__isnull=True).order_by("-version").first()
    if cur and cur.params.get("type") != "fixed":
        cur.params = NEW_PARAMS
        cur.save(update_fields=["params"])


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0013_pool_campus_id_pool_cursus_id"),
    ]

    operations = [
        migrations.RunPython(migrate, migrations.RunPython.noop),
    ]
