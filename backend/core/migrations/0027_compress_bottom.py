"""
Équilibrage « resserrer le bas » (07/2026) — le flop du classement décrochait
trop (jusqu'à −2037) sous l'empilement des malus. Quatre ajustements, tous
réappliqués à l'historique par le script de suivi :

1. cursed_host : range aléatoire −300..−100 → malus FIXE −100 (demande staff :
   les assis sur poste maudit perdaient trop, jusqu'à −890 cumulés).
2. logtime_high : −100 → −50 (1er contributeur du flop-15 : martelage quotidien
   des campeurs).
3. rush_malus : palier note 0 (et défaut) −150 → −100.
4. reconnect_same_pc : −20..−10 → −10..−5 (taxe ambiante divisée par 2).
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

    r = Rule.objects.filter(key="cursed_host").first()
    if r:
        r.label = "Place Maudite — malus FIXE à la connexion"
        r.save(update_fields=["label"])
        _revise(RuleVersion, r, {"type": "fixed", "points": -100}, now)

    r = Rule.objects.filter(key="logtime_high").first()
    if r:
        _revise(RuleVersion, r, {"type": "fixed", "points": -50}, now)

    r = Rule.objects.filter(key="rush_malus").first()
    if r:
        _revise(RuleVersion, r, {"type": "tiers", "value_key": "mark", "default": -100,
                                 "tiers": {"0": -100, "1": -50, "50": 50}}, now)

    r = Rule.objects.filter(key="reconnect_same_pc").first()
    if r:
        _revise(RuleVersion, r, {"type": "fixed", "min": -10, "max": -5}, now)


class Migration(migrations.Migration):
    dependencies = [("core", "0026_alter_poolconfig_plague_payout_and_more")]
    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
