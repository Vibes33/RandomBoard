"""
Démonstration du moteur de règles (étape 2).

    python manage.py rule_demo

Montre :
  1. l'évaluation de chaque règle à partir de son params (zéro hardcode),
  2. le random figé & reproductible,
  3. la modification d'un paramètre à la volée via le versioning temporel.
"""
import random

from django.core.management.base import BaseCommand

from core.engine import evaluate, new_rule_version
from core.models import Rule, RuleVersion

# Contextes d'exemple par règle
CONTEXTS = {
    "logtime_low": {"minutes": 90},
    "kw_quoi_feur": {},
    "bsq_eval": {},
    "bsq_duration": {"duration_min": 20},
    "exam_time": {"minutes": 180},
    "midnight_bonus": {"time": "23:55"},
    "randominette": {},
    "kw_quoi_sans_feur": {},
    "logtime_high": {"minutes": 1200},
    "last_day_corrections": {"correction_points": -2},
}


class Command(BaseCommand):
    help = "Démontre le moteur de règles."

    def handle(self, *args, **o):
        rng = random.Random(42)  # reproductible

        self.stdout.write(self.style.MIGRATE_HEADING("\n1) Évaluation des règles (params → points)\n"))
        self.stdout.write(f"  {'règle':<22}{'type':<18}{'contexte':<26}{'points':>8}")
        self.stdout.write("  " + "-" * 74)
        for key, ctx in CONTEXTS.items():
            rule = Rule.objects.filter(key=key).first()
            rv = rule.current_version if rule else None
            if not rv:
                continue
            res = evaluate(rv, ctx, rng=rng)
            roll = f"  roll={res['roll']}" if res["roll"] else ""
            self.stdout.write(
                f"  {key:<22}{rv.params.get('type',''):<18}{str(ctx):<26}{res['points']:>8}{roll}"
            )

        self.stdout.write(self.style.MIGRATE_HEADING("\n2) Random figé & reproductible (seed=42)\n"))
        r1 = evaluate(Rule.objects.get(key="bsq_eval").current_version, {}, rng=random.Random(7))
        r2 = evaluate(Rule.objects.get(key="bsq_eval").current_version, {}, rng=random.Random(7))
        self.stdout.write(f"  bsq_eval (seed 7) → {r1['points']} puis {r2['points']}  "
                          f"→ {'identique ✅' if r1['points']==r2['points'] else 'DIFFÉRENT ❌'}")

        self.stdout.write(self.style.MIGRATE_HEADING("\n3) Modif de paramètre à la volée (versioning §4)\n"))
        rule = Rule.objects.get(key="kw_quoi_feur")
        before = rule.current_version
        ctx = {}
        self.stdout.write(f"  v{before.version} (points={before.params['points']}) "
                          f"→ {evaluate(before, ctx)['points']} pts")
        nv = new_rule_version(rule, {"type": "fixed", "points": 99})
        rule.refresh_from_db()
        after = rule.current_version
        self.stdout.write(f"  v{after.version} (points={after.params['points']}) "
                          f"→ {evaluate(after, ctx)['points']} pts")
        archived = RuleVersion.objects.get(pk=before.pk).valid_to is not None
        self.stdout.write(f"  → l'ancienne version v{before.version} est archivée "
                          f"(valid_to renseigné: {archived}), le passé figé reste intact.")

        # on remet la valeur d'origine pour ne pas polluer la démo
        new_rule_version(rule, {"type": "fixed", "points": 50})
        self.stdout.write(self.style.SUCCESS("\nMoteur opérationnel. ✅\n"))
