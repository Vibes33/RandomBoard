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
"""
import calendar
import datetime as dt

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.text import slugify

from core.ft_api import FtClient, FtRateLimit
from core.models import Pool
from core.services import snapshot_day, standings
from core.sync import (
    fetch_campus_users, fetch_locations_range, fetch_scale_teams_range,
    sync_evaluations, sync_feedbacks, sync_flags, sync_locations, sync_users,
)

MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


class Command(BaseCommand):
    help = "Rejoue une piscine terminée jour par jour (données réelles API 42)."

    def add_arguments(self, parser):
        parser.add_argument("--from", dest="d_from", help="Jour de début (AAAA-MM-JJ).")
        parser.add_argument("--to", dest="d_to", help="Jour de fin inclus (AAAA-MM-JJ).")
        parser.add_argument("--campus", type=int, default=None)
        parser.add_argument("--cursus", type=int, default=None, help="cursus_id piscine (défaut settings).")
        parser.add_argument("--pool", default=None, help="Nom de la Piscine cible.")
        parser.add_argument("--skip-users", action="store_true", help="Ne pas ré-importer les étudiants.")

    def _detect_dates(self, client, logins, cursus_id):
        """Récupère begin_at/end_at du cursus piscine depuis un inscrit."""
        for login in logins[:5]:
            try:
                u = client.get(f"/v2/users/{login}").json()
                for cu in (u.get("cursus_users") or []):
                    if (cu.get("cursus") or {}).get("id") == cursus_id and cu.get("begin_at"):
                        b = dt.date.fromisoformat(cu["begin_at"][:10])
                        e = dt.date.fromisoformat((cu.get("end_at") or cu["begin_at"])[:10])
                        return b, e
            except Exception:  # noqa: BLE001
                continue
        return None, None

    def handle(self, *args, **o):
        client = FtClient()
        if not client.configured:
            raise CommandError("Aucune clé API (voir ft_doctor).")
        campus = o["campus"] if o["campus"] is not None else settings.FT_CAMPUS_ID
        if not campus:
            raise CommandError("Campus non défini (FT_CAMPUS_ID ou --campus).")

        cursus = o["cursus"] if o["cursus"] is not None else settings.FT_CURSUS_ID
        year = int(settings.FT_POOL_YEAR)
        month = MONTHS.get(str(settings.FT_POOL_MONTH).lower(), 7)

        pool_name = o["pool"] or f"Piscine {settings.FT_POOL_MONTH.capitalize()} {year} (campus {campus})"
        pool, _ = Pool.objects.get_or_create(
            slug=slugify(pool_name)[:50],
            defaults=dict(name=pool_name, starts_on=dt.date(year, month, 1),
                          ends_on=dt.date(year, month, 28), last_day=dt.date(year, month, 28),
                          is_active=True),
        )

        # 1) étudiants de la session
        if not o["skip_users"]:
            data = fetch_campus_users(client, campus, pool_year=str(year),
                                      pool_month=settings.FT_POOL_MONTH)
            res = sync_users(pool, data)
            self.stdout.write(f"Étudiants : {len(data)} ({res['created']} créés, {res['updated']} maj)")

        users = {u.login: u for u in pool.users.all()}
        tz = timezone.get_current_timezone()

        # 2) dates : fournies, sinon détectées via le cursus piscine (cursus_id)
        if o["d_from"] and o["d_to"]:
            d_from, d_to = dt.date.fromisoformat(o["d_from"]), dt.date.fromisoformat(o["d_to"])
        else:
            d_from, d_to = self._detect_dates(client, list(users), cursus)
            if not d_from:
                d_from = dt.date(year, month, 1)
                d_to = dt.date(year, month, calendar.monthrange(year, month)[1])
            self.stdout.write(f"Dates piscine détectées (cursus {cursus}) : {d_from} → {d_to}")
        if d_to < d_from:
            raise CommandError("--to est avant --from.")
        Pool.objects.filter(pk=pool.pk).update(starts_on=d_from, ends_on=d_to, last_day=d_to)

        # 2) rejeu jour par jour
        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Rejeu {d_from} → {d_to} · campus {campus} · {len(users)} étudiants\n"))
        day = d_from
        total_events = 0
        while day <= d_to:
            start = dt.datetime.combine(day, dt.time.min, tzinfo=tz).astimezone(dt.timezone.utc)
            end = start + dt.timedelta(days=1)
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            try:
                s, e = start.strftime(fmt), end.strftime(fmt)
                locs = fetch_locations_range(client, campus, s, e)
                scale = fetch_scale_teams_range(client, campus, s, e, cursus_id=cursus)
                n_loc = sync_locations(pool, locs, users)
                n_fb = sync_feedbacks(pool, scale["feedbacks"], users)
                n_ev = sync_evaluations(pool, scale["evaluations"], users)
                n_fl = sync_flags(pool, scale["flags"], users)
                snapshot_day(pool, day)  # fige le jour, comme le ferait le cron de minuit
                n = n_loc + n_fb + n_ev + n_fl
                total_events += n
                self.stdout.write(
                    f"  {day} · {len(locs):>4} loc / {len(scale['feedbacks']):>3} fb / "
                    f"{len(scale['evaluations']):>3} év → {n:>3} events "
                    f"(loc {n_loc}, fb {n_fb}, év {n_ev}, flag {n_fl})")
            except FtRateLimit as ex:
                self.stdout.write(self.style.WARNING(f"  {day} · rate-limit ({ex}) — jour ignoré"))
            day += dt.timedelta(days=1)

        # 3) classement final
        self.stdout.write(self.style.MIGRATE_HEADING("\nTop 10 de la piscine rejouée :\n"))
        for r in standings(pool, include_today=False)[:10]:
            self.stdout.write(f"  {r['rank']:>2}. {r['login']:<12} {round(r['total']):>8}")
        self.stdout.write(self.style.SUCCESS(f"\nRejeu terminé · {total_events} events ingérés.\n"))
