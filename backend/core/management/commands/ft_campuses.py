"""
Liste les campus 42 (pour trouver l'ID du Havre).

    python manage.py ft_campuses
    python manage.py ft_campuses --filter havre
"""
from django.core.management.base import BaseCommand

from core.ft_api import FtClient
from core.sync import fetch_campuses


class Command(BaseCommand):
    help = "Liste les campus 42 (id + nom)."

    def add_arguments(self, parser):
        parser.add_argument("--filter", help="Filtre sur le nom (insensible à la casse).")

    def handle(self, *args, **o):
        client = FtClient()
        if not client.configured:
            self.stdout.write(self.style.ERROR("Aucune clé API (voir ft_doctor)."))
            return
        needle = (o.get("filter") or "").lower()
        for cid, name in sorted(fetch_campuses(client), key=lambda t: t[0]):
            if needle and needle not in (name or "").lower():
                continue
            self.stdout.write(f"  {cid:>5}  {name}")
