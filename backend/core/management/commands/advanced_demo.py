"""
Démonstration des règles avancées (étape 5).

    python manage.py advanced_demo

Montre : désignation des maudits/bénis, malédiction des binômes avec doublement
sur éval croisée, malus d'ancienneté, aura du 1er de coalition, coef week-end.
"""
from django.core.management.base import BaseCommand

from core.derived import (
    apply_aura_penalty, apply_binome_effects, apply_seniority,
    assign_designations, ensure_weekend_coefficients,
)
from core.models import AppUser, EventLog, Pool
from core.services import standings


class Command(BaseCommand):
    help = "Démontre les règles avancées."

    def handle(self, *args, **o):
        pool = Pool.objects.filter(is_active=True).first()
        if not pool:
            self.stdout.write(self.style.ERROR("Lance d'abord seed_demo."))
            return

        h = self.style.MIGRATE_HEADING

        # 1) désignations (seed fixe pour la démo)
        self.stdout.write(h("\n1) Désignation hebdo (maudits / bénis)\n"))
        chosen = assign_designations(pool, n_cursed=1, n_blessed=1, seed=1)
        for login, status in chosen:
            self.stdout.write(f"   {login} → {status}")
        cursed = next(l for l, s in chosen if s == "cursed")
        blessed = next(l for l, s in chosen if s == "blessed")
        designated = {l for l, _ in chosen}
        # un tiers NON désigné, pour illustrer le cas ×1
        third = next(
            u.login for u in AppUser.objects.filter(pool=pool) if u.login not in designated
        )

        # 2) malédiction des binômes
        self.stdout.write(h("\n2) Malédiction des binômes\n"))
        EventLog.objects.filter(pool=pool, event_type__startswith="binome_").delete()  # reset démo
        pairs = [
            {"id": 901, "corrector_login": cursed, "corrected_login": blessed},  # croisé → ×2
            {"id": 902, "corrector_login": cursed, "corrected_login": third},    # 1 seul désigné → ×1
        ]
        apply_binome_effects(pool, pairs)
        for e in EventLog.objects.filter(event_type__startswith="binome_", is_voided=False).select_related("user"):
            w = (e.random_roll or {}).get("weight")
            self.stdout.write(f"   {e.user.login:<9} {e.event_type:<16} {e.raw_points:>7}  (×{w}, vs {e.raw_payload.get('counterpart')})")

        # 3) ancienneté
        self.stdout.write(h("\n3) Malus d'ancienneté\n"))
        weeks, n = apply_seniority(pool)
        self.stdout.write(f"   {weeks} semaine(s) écoulée(s) → malus appliqué à {n} étudiant(s)")

        # 4) coefficient week-end
        self.stdout.write(h("\n4) Coefficient week-end\n"))
        factor, upd = ensure_weekend_coefficients(pool)
        self.stdout.write(f"   facteur week-end = {factor} ({upd} jour(s) mis à jour)")

        # 5) aura (1er de coalition)
        self.stdout.write(h("\n5) Aura — 1er de coalition\n"))
        auras = apply_aura_penalty(pool)
        self.stdout.write(f"   {auras} leader(s) de coalition pénalisé(s)")

        self.stdout.write(h("\n→ Classement après règles avancées\n"))
        for r in standings(pool):
            self.stdout.write(f"   {r['rank']:>2}. {r['login']:<9} {round(r['total']):>8}")
        self.stdout.write(self.style.SUCCESS("\nRègles avancées opérationnelles. ✅\n"))
