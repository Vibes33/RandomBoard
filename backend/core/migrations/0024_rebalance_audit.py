"""
Audit d'équilibrage (07/2026) — objectif : classement « un peu random », aucun
signal de mérite ne doit dominer.

1. project_perfect : BUG — le « bonus nerfé » donnait +30 au lieu d'en enlever.
   Rendre un projet pile à 100 = MALUS -30 (catégorie loss). Cohérent avec
   shell_malus (rendre Shell 00/01 à 100 = malus).
2. assiduity_streak : facteur 10 → 5 (max 35/j au lieu de 70/j). C'était le
   1er créateur d'écart du classement (sd inter-user 391, moyenne +690) —
   signal de mérite trop fort face à l'aléatoire.
3. logtime_high : -200 → -100. Martelait les mêmes 29 campeurs jour après jour
   (sd inter-user 521, moyenne -621) — 2e créateur d'écart.
4. project_random : aléa ±(-50..80) → ±(-100..120). Booste la part d'ALÉA PUR
   (123 users touchés), moyenne quasi neutre. Le `luck` figé de chaque event se
   rééchelonne sur la nouvelle range (pas de re-tirage).

Le recalcul du passé (reapply_rule_points + snapshots) est fait par un script
de suivi hors migration, comme pour 0020/0022.
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
    now = timezone.now()

    # 1. bug : rendre un projet à 100 doit ENLEVER des points
    r = Rule.objects.filter(key="project_perfect").first()
    if r:
        r.label = "Rendre un projet pile à 100 (malus)"
        r.category = "loss"
        r.save(update_fields=["label", "category"])
        _revise(RuleVersion, r, {"type": "fixed", "points": -30}, now)

    # 2. assiduité : facteur divisé par 2
    r = Rule.objects.filter(key="assiduity_streak").first()
    if r:
        _revise(RuleVersion, r,
                {"type": "multiplier", "value_key": "streak", "factor": 5, "cap": 7}, now)

    # 3. malus 14 h adouci
    r = Rule.objects.filter(key="logtime_high").first()
    if r:
        _revise(RuleVersion, r, {"type": "fixed", "points": -100}, now)

    # 4. plus d'aléa pur sur les projets évalués
    r = Rule.objects.filter(key="project_random").first()
    if r:
        _revise(RuleVersion, r,
                {"type": "random_modifier", "base": 0,
                 "rand_min": -100, "rand_max": 120}, now)


def revert(apps, schema_editor):
    # Le versioning temporel garde tout l'historique ; un retour arrière se fait
    # en rééditant les règles au panel (pas de restauration automatique ici).
    pass


class Migration(migrations.Migration):
    dependencies = [("core", "0023_midnight_bonus_label")]
    operations = [migrations.RunPython(apply, revert)]
