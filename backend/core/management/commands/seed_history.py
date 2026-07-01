"""
Génère des events sur les ~10 jours passés (+ coefficients variés) pour rendre
le mécanisme de snapshots démontrable. Le random est figé à l'écriture (principe A).

    python manage.py seed_history
    python manage.py seed_history --days 14 --reset

Idempotent : ne re-crée pas d'historique si déjà présent (sauf --reset).
"""
import datetime as dt
import random
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import AppUser, DailyCoefficient, EventLog, Pool, Rule

EVENT_TYPES = ["logtime", "project_bsq", "feedback_keyword", "exam"]


class Command(BaseCommand):
    help = "Crée un historique d'events sur les jours passés (démo snapshots)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=10)
        parser.add_argument("--reset", action="store_true", help="Purge l'historique passé d'abord.")

    def handle(self, *args, **o):
        pool = Pool.objects.filter(is_active=True).order_by("-starts_on").first()
        if not pool:
            self.stdout.write(self.style.WARNING("Aucune Piscine. Lance d'abord seed_demo."))
            return
        users = list(AppUser.objects.filter(pool=pool))
        if not users:
            self.stdout.write(self.style.WARNING("Aucun étudiant. Lance d'abord seed_demo."))
            return

        today = timezone.localdate()
        rng = random.Random(42)  # reproductible
        rules = {r.key: r for r in Rule.objects.all()}

        if o["reset"]:
            n, _ = EventLog.objects.filter(pool=pool, event_date__lt=today,
                                           source=EventLog.Source.SYSTEM).delete()
            self.stdout.write(f"Purge : {n} ligne(s) supprimée(s).")

        already = EventLog.objects.filter(pool=pool, event_date=today - dt.timedelta(days=1)).exists()
        if already and not o["reset"]:
            self.stdout.write("Historique déjà présent (utilise --reset pour régénérer).")
            return

        created = 0
        for d_off in range(o["days"], 0, -1):
            day = today - dt.timedelta(days=d_off)
            DailyCoefficient.objects.update_or_create(
                pool=pool, day=day,
                defaults={"coefficient": Decimal(str(round(rng.uniform(0.8, 1.6), 2))),
                          "is_weekend": day.weekday() >= 5},
            )
            for user in users:
                for _ in range(rng.randint(1, 3)):
                    etype = rng.choice(EVENT_TYPES)
                    # random figé : points positifs ou négatifs selon le type
                    pts = rng.randint(20, 200) if etype != "feedback_keyword" else rng.choice([-40, 50])
                    when = timezone.make_aware(dt.datetime.combine(day, dt.time(rng.randint(8, 22))))
                    EventLog.objects.create(
                        user=user, pool=pool, event_type=etype,
                        source=EventLog.Source.SYSTEM, occurred_at=when, event_date=day,
                        rule=rules.get("logtime_low") if etype == "logtime" else None,
                        rule_version=1, raw_points=Decimal(str(pts)),
                        random_roll={"seed": 42, "value": pts},
                    )
                    created += 1

        self.stdout.write(self.style.SUCCESS(
            f"{created} events créés sur {o['days']} jours. "
            f"Lance maintenant : python manage.py run_snapshots --backfill"
        ))
