"""
Synchronise les données depuis l'API 42 (étape 4).

    python manage.py ft_sync --demo        # données simulées (sans clés API)
    python manage.py ft_sync               # live (nécessite FT_API_UID/SECRET)
    python manage.py ft_sync --campus 9    # endpoint campus précis
    python manage.py ft_sync --demo --snapshot   # + recalcule les snapshots

Le mode live ne récupère pour l'instant que les locations ; feedbacks &
évaluations sont mappés en --demo (la couche FETCH live est à compléter selon
ton scope OAuth — voir sync.fetch_live).
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from core.ft_api import FtAuthError, FtClient
from core.models import Pool
from core.services import backfill
from core.sync import demo_payload, fetch_live, sync_all


class Command(BaseCommand):
    help = "Synchronise les événements depuis l'API 42."

    def add_arguments(self, parser):
        parser.add_argument("--demo", action="store_true", help="Données simulées (sans API).")
        parser.add_argument("--campus", type=int, default=None, help="ID campus (sinon settings).")
        parser.add_argument("--since", help="ISO datetime : ne récupère que depuis cette date.")
        parser.add_argument("--snapshot", action="store_true", help="Recalcule les snapshots après.")

    def handle(self, *args, **o):
        pool = Pool.objects.filter(is_active=True).order_by("-starts_on").first()
        if not pool:
            self.stdout.write(self.style.ERROR("Aucune Piscine active (lance seed_demo)."))
            return

        if o["demo"]:
            data = demo_payload()
            self.stdout.write("Mode DÉMO : données simulées injectées.")
        else:
            client = FtClient()
            if not client.configured:
                self.stdout.write(self.style.ERROR(
                    "Clés API absentes. Renseigne FT_API_UID/FT_API_SECRET dans .env, "
                    "ou utilise --demo."))
                return
            campus = o["campus"] if o["campus"] is not None else settings.FT_CAMPUS_ID
            try:
                self.stdout.write(f"Récupération live (campus={campus or 'global'})…")
                data = fetch_live(client, campus_id=campus, since=o["since"])
            except FtAuthError as e:
                self.stdout.write(self.style.ERROR(f"Auth 42 échouée : {e}"))
                return
            self.stdout.write(f"Reçu : {len(data['locations'])} location(s).")

        counts = sync_all(pool, data)
        self.stdout.write(self.style.SUCCESS(
            f"Ingestion : {counts['locations']} (locations) · "
            f"{counts['feedbacks']} (feedbacks) · {counts['evaluations']} (évals)."
        ))

        if o["snapshot"]:
            days = backfill(pool)
            self.stdout.write(f"Snapshots recalculés sur {len(days)} jour(s).")
