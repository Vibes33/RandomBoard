"""
Diagnostic + (re)synchro des trolls (Google Sheet).

    python manage.py ft_trolls           # DIAGNOSTIC : config, HTTP, lignes lues
    python manage.py ft_trolls --sync     # + ingère dans la piscine active

À lancer SUR LE SERVEUR DE PROD quand « les scripts ne se fetchent pas » : il
dit précisément pourquoi (config .env absente, blocage réseau, HTTP KO…).
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Pool
from core.sync import fetch_trolls, sync_trolls


class Command(BaseCommand):
    help = "Diagnostic et synchro des trolls depuis le Google Sheet."

    def add_arguments(self, parser):
        parser.add_argument("--sync", action="store_true",
                            help="Ingère les trolls dans la piscine active après le diagnostic.")

    def handle(self, *args, **o):
        w = self.stdout.write
        url = settings.TROLL_SHEET_URL
        pwd = settings.TROLL_SHEET_PASSWORD
        w("── Config (.env de CE serveur) ─────────────────────────")
        w(f"  TROLL_SHEET_URL      : {'défini ('+url[:45]+'…)' if url else '❌ ABSENT'}")
        w(f"  TROLL_SHEET_PASSWORD : {'défini ('+str(len(pwd))+' car.)' if pwd else '❌ ABSENT'}")
        if not url or not pwd:
            w(self.style.ERROR(
                "\n→ CAUSE : le .env de ce serveur n'a pas les variables du sheet.\n"
                "  Ajoute TROLL_SHEET_URL et TROLL_SHEET_PASSWORD au .env de PROD,\n"
                "  puis `docker compose up -d` (un `restart` ne recharge pas le .env)."))
            return

        diag = {}
        w("\n── Appel du Google Sheet ───────────────────────────────")
        rows = fetch_trolls(diagnostic=diag)
        w(f"  HTTP status : {diag.get('http_status', '—')}")
        if diag.get("error"):
            w(self.style.ERROR(f"  Erreur      : {diag['error']}"))
            w(self.style.WARNING(
                "\n→ Le serveur de prod n'arrive pas à joindre script.google.com "
                "(pare-feu / egress bloqué ?) ou le mot de passe est faux."))
            return
        w(self.style.SUCCESS(f"  Lignes lues : {len(rows)}"))
        if rows:
            r = rows[0]
            w(f"  Exemple     : {r['at']} · {r['victim']} niv {r['level']} ({r['troll']})")

        if o["sync"]:
            pool = Pool.objects.filter(is_active=True).order_by("-starts_on").first()
            if not pool:
                w(self.style.ERROR("Aucune piscine active."))
                return
            n, first = sync_trolls(pool, rows)
            w(f"\n── Synchro dans « {pool.name} » ────────────────────────")
            w(self.style.SUCCESS(f"  {n} events troll créés (période {pool.starts_on}→{pool.ends_on})"))
            in_period = sum(1 for r in rows
                            if pool.starts_on <= timezone.datetime.fromisoformat(
                                r["at"].replace("Z", "+00:00")).date() <= pool.ends_on)
            if not in_period:
                w(self.style.WARNING(
                    "  ⚠️ Aucune ligne du sheet ne tombe dans la période de la piscine "
                    "active → vérifie les dates de la piscine."))
