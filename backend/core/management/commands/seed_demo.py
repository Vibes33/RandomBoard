"""
Remplit la base avec un jeu de démo : 1 Piscine, des règles versionnées,
des étudiants, un coefficient du jour et quelques events.

    python manage.py seed_demo

Idempotent : relançable sans créer de doublons.
"""
import datetime as dt
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.engine import new_rule_version
from core.models import (
    AppUser, DailyCoefficient, EventLog, HostConfig, Pool, Rule, RuleVersion,
)
# Source de vérité unique des règles (aussi utilisée par la migration 0006).
from core.rules_config import RULES as DEMO_RULES

# Coalitions des étudiants de démo (pour l'aura "1er de coalition")
COALITIONS = {
    "abelle": "Assembleurs", "bnguyen": "Assembleurs", "cdupont": "Assembleurs",
    "dmartin": "Alliance", "erossi": "Alliance",
}

# Postes spéciaux de démo (matchent les hosts de sync.demo_payload)
HOSTS = [
    ("e1r2p3", "shiny", {"points": 200}),
    ("e2r1p2", "cursed", {"points": -150}),
]

DEMO_USERS = ["abelle", "bnguyen", "cdupont", "dmartin", "erossi"]


class Command(BaseCommand):
    help = "Crée un jeu de données de démonstration."

    def handle(self, *args, **opts):
        today = timezone.localdate()
        pool, created = Pool.objects.get_or_create(
            slug="piscine-demo",
            defaults=dict(
                name="Piscine Démo",
                starts_on=today - dt.timedelta(days=14),
                ends_on=today + dt.timedelta(days=14),
                last_day=today + dt.timedelta(days=14),
            ),
        )
        self.stdout.write(f"Piscine: {pool.name} ({'créée' if created else 'existante'})")

        now = timezone.now()
        migrated = 0
        for key, category, label, params in DEMO_RULES:
            rule, _ = Rule.objects.get_or_create(
                key=key, defaults=dict(category=category, label=label)
            )
            # range de multiplicateur par défaut (éditable ensuite dans le panel)
            if rule.mult_min == 1 and rule.mult_max == 1:
                rule.mult_min, rule.mult_max = Decimal("0.5"), Decimal("2.0")
                rule.save(update_fields=["mult_min", "mult_max"])
            cur = rule.current_version
            if cur is None:
                RuleVersion.objects.create(rule=rule, version=1, params=params, valid_from=now)
            elif cur.params != params:
                # la config a changé → nouvelle version (le passé figé reste intact)
                new_rule_version(rule, params)
                migrated += 1
        self.stdout.write(f"Règles: {Rule.objects.count()} "
                          f"({migrated} mise(s) à jour)")

        users = []
        for login in DEMO_USERS:
            u, _ = AppUser.objects.get_or_create(
                login=login, defaults=dict(pool=pool, display_name=login.capitalize())
            )
            if not u.coalition and login in COALITIONS:
                u.coalition = COALITIONS[login]
                u.save(update_fields=["coalition"])
            users.append(u)
        self.stdout.write(f"Étudiants: {len(users)} (coalitions assignées)")

        for hostname, kind, params in HOSTS:
            HostConfig.objects.get_or_create(
                hostname=hostname, kind=kind, defaults=dict(params=params)
            )

        DailyCoefficient.objects.get_or_create(
            pool=pool, day=today,
            defaults=dict(coefficient=Decimal("1.5"), is_weekend=today.weekday() >= 5),
        )

        # Quelques events du jour (random déjà figé dans raw_points/random_roll)
        logtime_rule = Rule.objects.get(key="logtime_low")
        bsq_rule = Rule.objects.get(key="bsq_eval")
        if not EventLog.objects.filter(pool=pool).exists():
            samples = [
                (users[0], logtime_rule, "logtime", Decimal("180"), None),
                (users[1], logtime_rule, "logtime", Decimal("120"), None),
                (users[2], bsq_rule, "project_bsq", Decimal("118"), {"roll": 18}),
                (users[3], logtime_rule, "logtime", Decimal("90"), None),
                (users[4], bsq_rule, "project_bsq", Decimal("82"), {"roll": -18}),
            ]
            for user, rule, etype, pts, roll in samples:
                EventLog.objects.create(
                    user=user, pool=pool, event_type=etype, source=EventLog.Source.SYSTEM,
                    occurred_at=now, event_date=today,
                    rule=rule, rule_version=1,
                    raw_points=pts, random_roll=roll,
                )
        self.stdout.write(f"Events: {EventLog.objects.filter(pool=pool).count()}")
        self.stdout.write(self.style.SUCCESS("Seed terminé. Va voir /admin/ 🎉"))
