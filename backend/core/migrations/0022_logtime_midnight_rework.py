"""
Refonte logtime (07/2026).

1. logtime_low : RETIRÉ. On ne garde que le jackpot de la minute
   (logtime_minute). La règle passe is_active=False et TOUS ses events sont
   annulés (is_voided) pour disparaître du classement.
2. midnight_bonus : n'est plus un palier sur le total de logtime (mal compris
   comme « 23h50 de logtime »). C'est désormais un BONUS FIXE donné uniquement
   si l'étudiant est présent entre 23h50 et 23h59 (heure d'horloge). La fenêtre
   est détectée dans sync_locations ; la règle ne porte plus que les points.
   Les anciens events (paliers) sont annulés — ils seront recréés correctement
   au prochain sync/poll pour les seuls présents dans la fenêtre.

Le recompute des snapshots (pour retirer les points annulés du classement) est
fait par un script de suivi (hors migration), comme pour 0020.
"""
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
    EventLog = apps.get_model("core", "EventLog")
    now = timezone.now()

    # 1. logtime_low retiré : désactivé + events annulés
    Rule.objects.filter(key="logtime_low").update(is_active=False)
    EventLog.objects.filter(event_type="logtime_low", is_voided=False).update(is_voided=True)

    # 2. midnight_bonus → bonus fixe (fenêtre 23h50–23h59 gérée par sync_locations)
    r = Rule.objects.filter(key="midnight_bonus").first()
    if r:
        r.label = "Présent entre 23h50 et 23h59 (bonus fixe)"
        r.save(update_fields=["label"])
        _revise(RuleVersion, r, {"type": "fixed", "points": 300}, now)
        # anciens events (paliers sur le total de logtime) annulés → recréés au sync
        EventLog.objects.filter(event_type="midnight_bonus", is_voided=False).update(is_voided=True)


def revert(apps, schema_editor):
    # Non réversible proprement (events annulés). No-op : on réactive juste la règle.
    Rule = apps.get_model("core", "Rule")
    Rule.objects.filter(key="logtime_low").update(is_active=True)


class Migration(migrations.Migration):
    dependencies = [("core", "0021_poolconfig_secret_board_chance")]
    operations = [migrations.RunPython(apply, revert)]
