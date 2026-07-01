"""
Diagnostic de la connexion API 42 (pool de clés).

    python manage.py ft_doctor

Teste l'authentification de CHAQUE clé et affiche son état.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from core.ft_api import FtClient


class Command(BaseCommand):
    help = "Vérifie l'authentification de chaque clé API 42."

    def handle(self, *args, **o):
        client = FtClient()
        self.stdout.write(f"Clés configurées : {len(client.keys)}")
        self.stdout.write(f"Campus cible : {settings.FT_CAMPUS_ID or '(non défini)'} · "
                          f"session {settings.FT_POOL_MONTH} {settings.FT_POOL_YEAR}\n")
        if not client.configured:
            self.stdout.write(self.style.ERROR(
                "Aucune clé. Renseigne FT_API_UID_1/FT_API_SECRET_1… dans .env."))
            return
        ok = 0
        for label, good, detail in client.verify():
            mark = self.style.SUCCESS("✓") if good else self.style.ERROR("✗")
            self.stdout.write(f"  {mark} {label:<20} {detail}")
            ok += int(good)
        self.stdout.write(self.style.SUCCESS(f"\n{ok}/{len(client.keys)} clé(s) opérationnelle(s)."))
