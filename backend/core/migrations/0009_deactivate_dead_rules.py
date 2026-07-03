"""
Désactive les règles mortes / doublons / porte-config (INACTIVE_RULES).

is_active=False plutôt que suppression : l'historique (EventLog déjà figés)
reste intact, mais ces règles disparaissent du panel et ne s'appliquent plus.
Idempotent et réversible.
"""
from django.db import migrations


def deactivate(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    from core.rules_config import INACTIVE_RULES
    Rule.objects.filter(key__in=INACTIVE_RULES).update(is_active=False)


def reactivate(apps, schema_editor):
    Rule = apps.get_model("core", "Rule")
    from core.rules_config import INACTIVE_RULES
    Rule.objects.filter(key__in=INACTIVE_RULES).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_syncrun_cancel_requested_alter_syncrun_status"),
    ]

    operations = [
        migrations.RunPython(deactivate, reactivate),
    ]
