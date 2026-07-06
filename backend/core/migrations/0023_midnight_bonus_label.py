"""
Précision midnight_bonus (07/2026) : le bonus se déclenche quand le LOGTIME
TOTAL du jour est entre 23h50 et 23h59 (1430–1439 min, quasi 24 h), et non selon
l'heure d'horloge de connexion. Seul le libellé change ici (le gating vit dans
sync_locations ; les points restent fixes, éditables au panel).
"""
from django.db import migrations


LABEL = "Logtime total du jour 23h50–23h59 (bonus fixe)"


def apply(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    Rule.objects.filter(key="midnight_bonus").update(label=LABEL)


def revert(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    Rule.objects.filter(key="midnight_bonus").update(
        label="Présent entre 23h50 et 23h59 (bonus fixe)")


class Migration(migrations.Migration):
    dependencies = [("core", "0022_logtime_midnight_rework")]
    operations = [migrations.RunPython(apply, revert)]
