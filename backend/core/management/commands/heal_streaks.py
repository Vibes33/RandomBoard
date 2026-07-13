"""
Répare l'historique d'assiduité SANS appel API 42 :

  1. reconstitue la présence (DailyPresence) depuis les events déjà en base
     (voidés inclus : un event annulé prouve quand même la présence du jour ;
     les events assiduity_streak comptent aussi — choix CONSERVATEUR : pour les
     jours d'avant DailyPresence c'est parfois la seule trace, on ne détruit
     jamais d'historique légitime) ;
  2. recalcule la streak de chaque (étudiant × jour ouvré) depuis la présence ;
  3. réécrit les events assiduity_streak à valeur fausse et crée les manquants.
  4. recalcule les snapshots depuis le premier jour modifié.

Idempotent : un second run ne change rien. `--dry-run` = rapport sans écriture.
Limite : un jour sans AUCUNE trace en base (nuit de fetch ratée avant la mise
en place de DailyPresence) reste un trou — seul un refetch logtime le comble.
"""
from datetime import datetime, time as dtime, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.engine import record_event
from core.models import AppUser, DailyPresence, EventLog, Pool
from core.services import recompute_from
from core.sync import _presence_set, _weekday_streak

# tout event de cette famille n'existe que si l'étudiant avait du logtime ce jour
PRESENCE_EVENTS = ["assiduity_streak", "logtime_low", "logtime_high",
                   "logtime_minute", "midnight_bonus", "cluster_bonus",
                   "reconnect_same_pc"]


class Command(BaseCommand):
    help = "Répare les streaks d'assiduité depuis la présence en base (0 appel API)."

    def add_arguments(self, parser):
        parser.add_argument("--pool", help="Slug de la piscine (défaut : piscine active).")
        parser.add_argument("--dry-run", action="store_true",
                            help="Rapport seulement, aucune écriture.")

    def handle(self, *args, **o):
        pool = (Pool.objects.filter(slug=o["pool"]).first() if o.get("pool")
                else Pool.objects.filter(is_active=True).order_by("-starts_on").first())
        if not pool:
            self.stderr.write("Aucune piscine trouvée.")
            return
        dry = o["dry_run"]
        today = timezone.localdate()
        last = min(pool.ends_on, today - timedelta(days=1))
        self.stdout.write(f"Piscine : {pool.name} · {pool.starts_on} → {last}"
                          + (" · DRY-RUN" if dry else ""))

        # 1) présence : complète DailyPresence depuis les events historiques
        seen = set(EventLog.objects.filter(
            pool=pool, event_type__in=PRESENCE_EVENTS,
        ).values_list("user_id", "event_date"))
        have = set(DailyPresence.objects.filter(pool=pool).values_list("user_id", "day"))
        missing = seen - have
        if missing and not dry:
            DailyPresence.objects.bulk_create(
                [DailyPresence(pool=pool, user_id=u, day=d) for u, d in missing],
                ignore_conflicts=True, batch_size=2000)
        self.stdout.write(f"Présence : {len(have)} marqueurs, "
                          f"+{len(missing)} reconstitués depuis les events")
        presence = (have | missing) if dry else _presence_set(pool)

        # 2-3) recalcul des streaks jour ouvré par jour ouvré
        users = {u.id: u for u in AppUser.objects.filter(pool=pool)}
        current = {}
        for e in EventLog.objects.filter(pool=pool, event_type="assiduity_streak",
                                         is_voided=False):
            current.setdefault((e.user_id, e.event_date), []).append(e)

        created = fixed = 0
        first_changed = None
        d = pool.starts_on
        while d <= last:
            if d.weekday() >= 5:  # week-end neutre : jamais d'event assiduité
                d += timedelta(days=1)
                continue
            for uid, u in users.items():
                evs = current.get((uid, d), [])
                present = (uid, d) in presence
                if not present:  # un event impliquerait la présence (cf. étape 1)
                    continue
                want = _weekday_streak(presence, uid, d)
                stored = (evs[0].raw_payload or {}).get("streak") if evs else None
                if len(evs) == 1 and stored == want:
                    continue
                if first_changed is None or d < first_changed:
                    first_changed = d
                if evs:
                    fixed += 1    # valeur fausse ou doublons → réécrit
                else:
                    created += 1  # jour présent sans event (nuit ratée)
                if not dry:
                    for e in evs:
                        e.is_voided = True
                        e.save(update_fields=["is_voided"])
                    occurred = timezone.make_aware(datetime.combine(d, dtime(12, 0)))
                    record_event(user=u, pool=pool, rule_key="assiduity_streak",
                                 occurred_at=occurred, context={"streak": want},
                                 source=EventLog.Source.API_42)
            d += timedelta(days=1)

        self.stdout.write(f"Streaks : {created} créées · {fixed} corrigées")

        # 4) snapshots : recalcul depuis le premier jour touché
        if first_changed and not dry:
            recompute_from(pool, first_changed)
            self.stdout.write(f"Snapshots recalculés depuis le {first_changed}.")
        elif first_changed:
            self.stdout.write(f"(dry-run : recompute nécessaire depuis le {first_changed})")
        else:
            self.stdout.write("Rien à réparer — historique déjà cohérent. ✓")
