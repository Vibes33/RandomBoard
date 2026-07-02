"""
Rejoue une piscine TERMINÉE, jour par jour, depuis l'API 42 (données réelles figées).
Chaque jour est traité comme s'il se déroulait normalement : ingestion des locations
du jour → événements datés → snapshot du jour (comme le cron de minuit le ferait en live).

But : valider tout le pipeline sur de vraies données AVANT la piscine à venir.

    python manage.py ft_replay                         # mois FT_POOL_MONTH/YEAR (.env)
    python manage.py ft_replay --from 2025-07-01 --to 2025-07-10
    python manage.py ft_replay --campus 62 --pool "Piscine Le Havre Juillet 2025"

En LIVE (vraie piscine), on n'utilise PAS cette commande : le polling `poll_42`
récupère les données au fil de l'eau et `nightly_snapshot` fige chaque nuit.

La logique est partagée avec la tâche Celery `run_sync` (panel) via core.sync_runner.
"""
import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from core.ft_api import FtClient
from core.services import standings
from core.sync_runner import get_or_create_pool, resolve_target, run_full_sync


class Command(BaseCommand):
    help = "Rejoue une piscine terminée jour par jour (données réelles API 42)."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="d_from", help="Jour de début (AAAA-MM-JJ).")
        parser.add_argument("--to", dest="d_to", help="Jour de fin inclus (AAAA-MM-JJ).")
        parser.add_argument("--campus", type=int, default=None)
        parser.add_argument("--cursus", type=int, default=None, help="cursus_id piscine (défaut settings).")
        parser.add_argument("--pool", default=None, help="Nom de la Piscine cible.")
        parser.add_argument("--skip-users", action="store_true", help="Ne pas ré-importer les étudiants.")

    def handle(self, *args, **o):
        client = FtClient()
        if not client.configured:
            raise CommandError("Aucune clé API (voir ft_doctor).")
        campus, cursus = resolve_target(o["campus"], o["cursus"])
        if not campus:
            raise CommandError("Campus non défini (FT_CAMPUS_ID ou --campus).")

        pool, _ = get_or_create_pool(campus, name=o["pool"])
        d_from = dt.date.fromisoformat(o["d_from"]) if o["d_from"] else None
        d_to = dt.date.fromisoformat(o["d_to"]) if o["d_to"] else None

        self.stdout.write(self.style.MIGRATE_HEADING(f"Piscine : {pool.name}\n"))
        try:
            summary = run_full_sync(
                client=client, pool=pool, campus=campus, cursus=cursus,
                d_from=d_from, d_to=d_to, skip_users=o["skip_users"],
                on_log=lambda line: self.stdout.write(f"  {line}"),
            )
        except ValueError as ex:
            raise CommandError(str(ex))

        self.stdout.write(self.style.MIGRATE_HEADING("\nTop 10 de la piscine rejouée :\n"))
        for r in standings(pool, include_today=False)[:10]:
            self.stdout.write(f"  {r['rank']:>2}. {r['login']:<12} {round(r['total']):>8}")
        self.stdout.write(self.style.SUCCESS(
            f"\nRejeu terminé · {summary['total_events']} events ingérés.\n"))
