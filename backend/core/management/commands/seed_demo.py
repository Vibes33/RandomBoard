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

# (key, category, label, params de la version v1) — format moteur (champ "type")
DEMO_RULES = [
    ("logtime_low", "gain", "Logtime bas (1min–5h)",
     {"type": "linear_decay", "value_key": "minutes", "min": 1, "max": 300,
      "max_points": 200, "min_points": 0}),
    ("kw_quoi_feur", "gain", 'Feedback contient "quoi feur"',
     {"type": "fixed", "points": 50}),
    ("curl_leaderboard", "gain", "curl sur le leaderboard",
     {"type": "fixed", "points": 5}),
    ("bsq_eval", "gain", "Évaluation projet BSQ",
     {"type": "random_modifier", "base": 100, "rand_min": -30, "rand_max": 30}),
    ("bsq_duration", "loss", "BSQ : durée d'éval hors [30–60 min]",
     {"type": "threshold_window", "value_key": "duration_min", "lo": 30, "hi": 60,
      "in_points": 0, "out_points": -80}),
    ("exam_time", "gain", "Temps passé en examen",
     {"type": "linear_growth", "value_key": "minutes", "min": 0, "max": 240, "max_points": 300}),
    ("midnight_bonus", "gain", "Connexion entre 23:50 et 23:59",
     {"type": "time_window_bonus", "value_key": "time", "start": "23:50", "end": "23:59",
      "points": 500}),
    ("randominette", "event", "Randominette (pile ou face)",
     {"type": "probability", "proba": 0.5, "points": 150, "else_points": -50}),
    ("kw_quoi_sans_feur", "loss", 'Feedback "quoi" sans "feur" / "coubeh"',
     {"type": "fixed", "points": -40, "rank_penalty": 1}),
    ("reconnect_same_pc", "loss", "Reco sur le MÊME pc dans la journée",
     {"type": "fixed", "points": -25}),
    ("logtime_high", "loss", "Logtime haut (12h–24h)",
     {"type": "linear_growth", "value_key": "minutes", "min": 720, "max": 1440,
      "max_malus": -300}),
    ("aura_first_coalition", "loss", "1er de sa coalition (perte d'aura)",
     {"type": "fixed", "points": -1000}),
    ("last_day_corrections", "event", "Dernier jour : corrections × 1000",
     {"type": "multiplier", "value_key": "correction_points", "factor": 1000}),
    # ─── règles avancées (contextuelles) ───
    ("shiny_host", "gain", "Poste Shiny (bonus à la connexion)",
     {"type": "from_context", "value_key": "points"}),
    ("cursed_host", "loss", "Poste Maudit (malus à la connexion)",
     {"type": "from_context", "value_key": "points"}),
    ("binome_cursed", "loss", "Malédiction binôme — Maudit (×2 si croisé)",
     {"type": "weighted", "points": -120}),
    ("binome_blessed", "gain", "Malédiction binôme — Béni (×2 si croisé)",
     {"type": "weighted", "points": 120}),
    ("seniority_malus", "loss", "Malus d'ancienneté (croît avec les semaines)",
     {"type": "linear_growth", "value_key": "weeks", "min": 0, "max": 4, "max_malus": -200}),
    ("config_weekend", "event", "[config] coefficient du week-end",
     {"type": "fixed", "points": 0, "factor": 0.5}),
    # ─── mots-clés feedbacks (configurable : ajoute un mot = édite la règle) ───
    ("feedback_keywords", "gain", "Mots-clés dans les feedbacks",
     {"type": "keywords",
      "map": {"quoi feur": 50, "apple": 80, "frieren": 80, "dedavid": 100, "rydelepi": 100},
      "quoi_alone_points": -40, "coubeh_points": -40,
      "rank_penalty_on": ["quoi_alone", "coubeh"]}),
    # ─── random journalier (chaque jour vaut +/- ) ───
    ("config_daily", "event", "[config] random du coefficient journalier",
     {"type": "fixed", "points": 0, "coef_min": 0.8, "coef_max": 1.6}),
    # ─── ajustement manuel par le staff (points fournis à la création) ───
    ("manual_adjust", "event", "Ajustement manuel (staff)",
     {"type": "from_context", "value_key": "points"}),
    # ─── projets & corrections (issus de scale_teams) ───
    ("shell_malus", "loss", "Rendre Shell 00 / Shell 01",
     {"type": "fixed", "points": -60}),
    ("rush_malus", "loss", "Rush (malus, énorme si note 0)",
     {"type": "threshold_window", "value_key": "mark", "lo": 1, "hi": 125,
      "in_points": -50, "out_points": -300}),
    ("correction_flag", "loss", "Flag de correction (Empty work, Crash…)",
     {"type": "map_lookup", "key_field": "flag", "default": 0,
      "map": {"Empty work": -100, "Crash": -80, "Norme": -60,
              "Can't explain": -80, "Cheat": -200}}),
    ("project_random", "event", "Projet évalué (points random ±, au pif)",
     {"type": "random_modifier", "base": 0, "rand_min": -50, "rand_max": 80}),
]

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
            cur = rule.current_version
            if cur is None:
                RuleVersion.objects.create(rule=rule, version=1, params=params, valid_from=now)
            elif "type" not in (cur.params or {}):
                # migration vers le format moteur via versioning (le passé reste figé)
                new_rule_version(rule, params)
                migrated += 1
        self.stdout.write(f"Règles: {Rule.objects.count()} "
                          f"({migrated} migrée(s) vers le format moteur)")

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
