"""
Synchronise les étudiants de la piscine (campus + session) depuis l'API 42.

    python manage.py ft_users                       # campus & session = settings (.env)
    python manage.py ft_users --campus 41 --year 2026 --month july
    python manage.py ft_users --pool "Piscine Le Havre Juillet 2026"

Crée/actualise les AppUser dans la Piscine active (ou celle nommée par --pool).
"""
import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from core.ft_api import FtClient
from core.models import Pool
from core.sync import fetch_campus_users, sync_users


class Command(BaseCommand):
    help = "Importe les étudiants d'une piscine (campus + session)."

    def add_arguments(self, parser):
        parser.add_argument("--campus", type=int, default=None)
        parser.add_argument("--year", default=None)
        parser.add_argument("--month", default=None)
        parser.add_argument("--pool", help="Nom de la Piscine cible (créée si absente).")

    def handle(self, *args, **o):
        client = FtClient()
        if not client.configured:
            self.stdout.write(self.style.ERROR("Aucune clé API (voir ft_doctor)."))
            return
        campus = o["campus"] if o["campus"] is not None else settings.FT_CAMPUS_ID
        year = o["year"] or settings.FT_POOL_YEAR
        month = o["month"] or settings.FT_POOL_MONTH
        if not campus:
            self.stdout.write(self.style.ERROR(
                "Campus non défini. Renseigne FT_CAMPUS_ID (voir ft_campuses)."))
            return

        if o["pool"]:
            today = dt.date.today()
            pool, _ = Pool.objects.get_or_create(
                slug=slugify(o["pool"])[:50],
                defaults=dict(name=o["pool"], starts_on=today, ends_on=today,
                              last_day=today, is_active=True),
            )
        else:
            pool = Pool.objects.filter(is_active=True).order_by("-starts_on").first()
            if not pool:
                self.stdout.write(self.style.ERROR("Aucune Piscine active (utilise --pool)."))
                return

        self.stdout.write(f"Récupération campus={campus} · session {month} {year}…")
        data = fetch_campus_users(client, campus, pool_year=year, pool_month=month)
        res = sync_users(pool, data)
        self.stdout.write(self.style.SUCCESS(
            f"[{pool.name}] {len(data)} étudiant(s) : "
            f"{res['created']} créé(s), {res['updated']} actualisé(s)."))
