"""
Lance le calcul des snapshots à la main (équivalent du job de minuit).

    python manage.py run_snapshots                # fige hier (toutes Piscines actives)
    python manage.py run_snapshots --backfill     # recalcule tout l'historique
    python manage.py run_snapshots --from 2026-06-20   # recompute ciblé (jours 'dirty')
    python manage.py run_snapshots --day  2026-06-25   # un jour précis
"""
import datetime as dt

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Pool
from core.services import backfill, recompute_from, snapshot_day


def _parse(d):
    try:
        return dt.date.fromisoformat(d)
    except ValueError:
        raise CommandError(f"Date invalide : {d} (format attendu AAAA-MM-JJ)")


class Command(BaseCommand):
    help = "Calcule les snapshots quotidiens (somme courante figée)."

    def add_arguments(self, parser):
        parser.add_argument("--backfill", action="store_true", help="Recalcule tout l'historique.")
        parser.add_argument("--from", dest="from_day", help="Recompute depuis ce jour (dirty).")
        parser.add_argument("--day", help="Snapshot d'un seul jour.")

    def handle(self, *args, **o):
        pools = list(Pool.objects.filter(is_active=True))
        if not pools:
            self.stdout.write(self.style.WARNING("Aucune Piscine active."))
            return

        for pool in pools:
            if o["backfill"]:
                days = backfill(pool)
                self.stdout.write(f"[{pool.slug}] backfill : {len(days)} jour(s) recalculé(s).")
            elif o["from_day"]:
                days = recompute_from(pool, _parse(o["from_day"]))
                self.stdout.write(f"[{pool.slug}] recompute : {len(days)} jour(s) depuis {o['from_day']}.")
            elif o["day"]:
                n = snapshot_day(pool, _parse(o["day"]))
                self.stdout.write(f"[{pool.slug}] jour {o['day']} : {n} snapshot(s).")
            else:
                y = timezone.localdate() - dt.timedelta(days=1)
                n = snapshot_day(pool, y)
                self.stdout.write(f"[{pool.slug}] hier ({y}) : {n} snapshot(s).")

        self.stdout.write(self.style.SUCCESS("Snapshots terminés."))
