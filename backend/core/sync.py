"""
Synchronisation API 42 → événements (étape 4).

Architecture en 2 couches :
  1. FETCH (live)  : interroge l'API 42 et NORMALISE chaque objet en dict simple.
  2. MAP           : transforme ces dicts normalisés en appels record_event()
                     (donc passage par le moteur de règles, random figé, dedup).

Le mode --demo injecte directement des dicts normalisés → tout le pipeline de
MAP/ingestion est testable sans API live. Seule la couche FETCH a besoin des clés.

Formes normalisées :
  location   : {id, login, begin_at, end_at, host}
  feedback   : {id, login, comment, created_at}
  evaluation : {id, login, project, begin_at, end_at}
"""
import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta, timezone as dttz

from django.utils import timezone

from .engine import record_event
from .models import AppUser, DailyHost, DailyPresence, EventLog, Pool, Workstation

log = logging.getLogger(__name__)
API = EventLog.Source.API_42

# Règles dont les points dépendent de l'état FINAL de la journée (total de logtime
# ou présence en fin de journée) : scorées uniquement une fois le jour terminé
# (à minuit), jamais en cours de journée.
_LOGTIME_RULES = ("logtime_minute", "logtime_high",
                  "midnight_bonus", "assiduity_streak")

# Note minimale pour considérer un projet comme VALIDÉ : en dessous, un projet
# évalué ne rapporte aucun point (project_random). 42 valide à la moitié.
_PROJECT_PASS_MARK = 50


def _parse(s):
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _users(pool):
    return {u.login: u for u in AppUser.objects.filter(pool=pool)}


def _void_daily(user, pool, event_type, day):
    """Agrégat journalier évolutif : on annule l'ancien avant de réécrire."""
    EventLog.objects.filter(
        user=user, pool=pool, event_type=event_type, event_date=day, is_voided=False
    ).update(is_voided=True)


def _cap_to_day(dt, day):
    """
    Ramène un datetime dans les bornes du JOUR `day` (heure locale). Sert à dater
    les agrégats journaliers (logtime, cluster…) à l'heure RÉELLE de la dernière
    session — sans jamais déborder sur le jour suivant (une session qui finit
    après minuit resterait sinon comptée le lendemain via localdate()).
    """
    lo = timezone.make_aware(datetime.combine(day, dtime.min))
    hi = timezone.make_aware(datetime.combine(day, dtime(23, 59, 59)))
    return min(max(dt, lo), hi)


def _split_by_day(b, e):
    """
    Découpe une session [b, e] en tranches par JOUR LOCAL, avec découpe à minuit.
    Renvoie [(day, minutes, end_dans_le_jour), …]. Sans cette découpe, une session
    à cheval sur minuit était créditée EN ENTIER au jour de son begin_at : le jour
    J était gonflé des heures d'après-minuit, et le jour J+1 vidé — ce qui pouvait
    offrir le jackpot logtime_minute à un étudiant présent toute la journée
    (incident prod 07/2026).
    """
    out = []
    d, last = timezone.localdate(b), timezone.localdate(e)
    while d <= last:
        lo = timezone.make_aware(datetime.combine(d, dtime.min))
        hi = timezone.make_aware(datetime.combine(d + timedelta(days=1), dtime.min))
        end_in_day = min(e, hi)
        sec = (end_in_day - max(b, lo)).total_seconds()
        if sec > 0:
            out.append((d, sec / 60.0, end_in_day))
        d += timedelta(days=1)
    return out


def locs_beginning_on(locations, day):
    """
    Locations (normalisées) dont le begin_at tombe le jour LOCAL `day`. Sert aux
    consommateurs « à la connexion » (sync_host_effects) quand la fenêtre de
    fetch est élargie à la veille : ils ne doivent voir que les connexions du jour.
    """
    out = []
    for loc in locations:
        b = _parse(loc.get("begin_at"))
        if b and timezone.localdate(b) == day:
            out.append(loc)
    return out


def _cluster_of(host_list):
    """Extrait le cluster d'un poste (ex: 'c1r2p3' → 'c1'). '' si introuvable."""
    for h in host_list:
        m = re.match(r"^([a-zA-Z]+\d+)r\d+", h or "")
        if m:
            return m.group(1).lower()
    return ""


def _presence_set(pool):
    """
    Jours de présence {(user_id, date)} de toute la piscine, en UNE requête.
    Sert au calcul des streaks en mémoire (avant : 1 requête SQL par jour de
    streak × par étudiant × par jour syncé — N+1 massif en fin de piscine).

    Source = DailyPresence, marqueur FACTUEL écrit dès qu'un jour a du logtime
    (poll live inclus). Avant, la présence était déduite des events
    assiduity_streak eux-mêmes : chaîne auto-référentielle où un seul jour sans
    scoring (fetch de minuit raté, jour ignoré en replay) cassait les streaks
    de tout le campus le lendemain.
    """
    return set(DailyPresence.objects.filter(pool=pool).values_list("user_id", "day"))


def _weekday_streak(presence, user_id, day):
    """
    Nombre de JOURS OUVRÉS consécutifs (lun–ven) avec une connexion, se terminant
    à `day`. Le week-end est NEUTRE : il ne casse pas la série et ne l'incrémente
    pas (on le saute). Un jour ouvré sans connexion casse la série.
    Pur : lit le set `presence` (le jour courant doit y avoir été ajouté).
    """
    streak, d = 0, day
    while True:
        if d.weekday() >= 5:            # samedi/dimanche : neutre, on saute
            d -= timedelta(days=1)
            continue
        if (user_id, d) not in presence:
            break
        streak += 1
        d -= timedelta(days=1)
    return streak


# ─────────────────────────────────────────────────────────────
# MAP : locations → logtime, midnight bonus, reconnexion même PC
# ─────────────────────────────────────────────────────────────
def sync_locations(pool, locations, users=None, *, score_cumulative=True,
                   only_days=None):
    """
    score_cumulative=False (jour EN COURS) : on agrège la présence (workstations,
    cluster, reconnexion — discrets) mais on ne donne AUCUN point de la famille
    logtime (total non figé). Les points logtime tombent à minuit, quand le jour
    est final (score_cumulative=True), via tasks.nightly_snapshot.

    only_days : ensemble de dates à scorer (None = toutes celles du payload).
    Indispensable quand la fenêtre de fetch est élargie à la veille (pour capter
    les sessions à cheval sur minuit) : les tranches qui retombent sur J-1 ou
    J+1 ne doivent pas être re-scorées ici — J-1 est déjà figé (re-void + re-roll
    du random changerait des scores snapshotés) et J+1 sera scoré par SON fetch.

    Le logtime d'un jour = somme des tranches de sessions DÉCOUPÉES À MINUIT
    (cf. _split_by_day) : une session 23h → 3h crédite 1 h au jour J et 3 h au
    jour J+1, jamais 4 h au jour J.
    """
    users = users or _users(pool)
    minutes = defaultdict(float)
    hosts = defaultdict(list)   # (uid, jour du BEGIN) → postes (connexions du jour)
    last_end = {}  # (uid, day) → fin de la DERNIÈRE tranche du jour (heure réelle)
    seen_hosts = set()
    created = 0

    for loc in locations:
        u = users.get(loc.get("login"))
        if not u:
            continue
        b = _parse(loc.get("begin_at"))
        if not b:
            continue
        e = _parse(loc.get("end_at")) or timezone.now()
        host = loc.get("host", "")
        # minutes découpées à minuit : chaque jour reçoit SA part de la session
        for day, mins_part, end_in_day in _split_by_day(b, e):
            minutes[(u.id, day)] += mins_part
            cur = last_end.get((u.id, day))
            if cur is None or end_in_day > cur:
                last_end[(u.id, day)] = end_in_day
        # postes/cluster/reconnexion : liés à la CONNEXION → jour du begin_at
        hosts[(u.id, timezone.localdate(b))].append(host)
        if host:
            seen_hosts.add(host)

    # registre des postes connus (pool de tirage des places)
    if seen_hosts:
        Workstation.objects.bulk_create(
            [Workstation(pool=pool, hostname=h, last_seen=timezone.localdate()) for h in seen_hosts],
            ignore_conflicts=True)

    # Présence FACTUELLE : une ligne par (user, jour) ayant du logtime — y compris
    # week-ends, jour courant (poll) et jours hors only_days (un fait n'est pas un
    # score). Écrite AVANT le chargement du set : le jour scoré y figure déjà.
    # Résilience : le poll du jour J écrit la présence en direct → si le fetch de
    # minuit échoue, seuls les points de J manquent, la chaîne d'assiduité survit.
    if minutes:
        DailyPresence.objects.bulk_create(
            [DailyPresence(pool=pool, user_id=uid, day=day) for (uid, day) in minutes],
            ignore_conflicts=True)

    id_to_user = {u.id: u for u in users.values()}
    presence = _presence_set(pool)  # 1 requête, streaks calculés en mémoire
    for (uid, day), mins in minutes.items():
        if only_days is not None and day not in only_days:
            continue  # tranche hors cible (veille figée / lendemain pas encore dû)
        u = id_to_user[uid]
        # heure RÉELLE (dernière session du jour) au lieu d'un midi figé
        occurred = _cap_to_day(last_end[(uid, day)], day)

        # ── Famille logtime : SEULEMENT si le jour est final (score_cumulative) ──
        if not score_cumulative:
            # Jour en cours : aucun point logtime, et on retire tout point partiel
            # éventuellement écrit par un poll précédent (le jour n'est pas figé).
            for rk in _LOGTIME_RULES:
                _void_daily(u, pool, rk, day)
        else:
            # (la présence du jour est déjà dans le set : DailyPresence écrit plus haut)

            # Jackpot de la minute : logtime TOTAL du jour = 1 min pile → gros bonus.
            # « Pile » = la minute entière affichée : 60 s ≤ total < 120 s.
            # (avant : round(mins) == 1 acceptait de 31 s à 89 s — trop laxiste)
            _void_daily(u, pool, "logtime_minute", day)
            if 1.0 <= mins < 2.0:
                record_event(user=u, pool=pool, rule_key="logtime_minute", occurred_at=occurred,
                             context={"minutes": round(mins, 2)}, source=API)
                created += 1

            # Malus des 14 h : au-delà de 840 min sur la journée → malus FIXE unique.
            _void_daily(u, pool, "logtime_high", day)
            if mins >= 840:
                record_event(user=u, pool=pool, rule_key="logtime_high", occurred_at=occurred,
                             context={"minutes": round(mins)}, source=API)
                created += 1

            # Bonus « minuit » : logtime TOTAL du jour entre 23h50 et 23h59, soit
            # 1430 min ≤ total < 1440 min (minute entière, même logique que le jackpot).
            _void_daily(u, pool, "midnight_bonus", day)
            if 1430 <= mins < 1440:
                record_event(user=u, pool=pool, rule_key="midnight_bonus", occurred_at=occurred,
                             context={"minutes": round(mins)}, source=API)
                created += 1

            # assiduité : jours ouvrés consécutifs (week-ends neutres) — le jour
            # même compte car sa présence vient d'être ajoutée ci-dessus.
            if day.weekday() < 5:
                streak = _weekday_streak(presence, uid, day)
                _void_daily(u, pool, "assiduity_streak", day)
                record_event(user=u, pool=pool, rule_key="assiduity_streak", occurred_at=occurred,
                             context={"streak": streak}, source=API)
                created += 1

        # cluster où l'élève est assis (points via la table configurée)
        cluster = _cluster_of(hosts[(uid, day)])
        if cluster:
            _void_daily(u, pool, "cluster_bonus", day)
            cev = record_event(user=u, pool=pool, rule_key="cluster_bonus", occurred_at=occurred,
                               context={"cluster": cluster}, source=API)
            if cev and cev.raw_points == 0:      # cluster non configuré → pas de bruit
                cev.is_voided = True
                cev.save(update_fields=["is_voided"])
            elif cev:
                created += 1

    # reconnexion sur le MÊME pc dans la journée
    for (uid, day), hlist in hosts.items():
        if only_days is not None and day not in only_days:
            continue  # connexions de la veille (fenêtre élargie) : jour déjà figé
        if len(hlist) != len(set(hlist)):
            u = id_to_user[uid]
            # fallback midi : sessions de durée nulle → aucune tranche dans last_end
            fallback = timezone.make_aware(datetime.combine(day, dtime(12, 0)))
            occurred = _cap_to_day(last_end.get((uid, day), fallback), day)
            _void_daily(u, pool, "reconnect_same_pc", day)
            record_event(user=u, pool=pool, rule_key="reconnect_same_pc", occurred_at=occurred,
                         context={}, source=API)
            created += 1

    return created


# ─────────────────────────────────────────────────────────────
# MAP : feedbacks → mots-clés
# ─────────────────────────────────────────────────────────────
def sync_feedbacks(pool, feedbacks, users=None):
    """Une seule règle 'feedback_keywords' évalue tous les mots-clés (config)."""
    users = users or _users(pool)
    created = 0
    for f in feedbacks:
        u = users.get(f.get("login"))
        if not u:
            continue
        ev = record_event(user=u, pool=pool, rule_key="feedback_keywords",
                          occurred_at=_parse(f.get("created_at")) or timezone.now(),
                          context={"comment": f.get("comment")}, source=API,
                          dedup_key=f"feedback:{f.get('id')}")
        # on ne garde l'event que s'il a matché quelque chose (sinon 0 pt inutile)
        if ev and ev.raw_points == 0 and not (ev.random_roll or {}).get("matched"):
            ev.is_voided = True
            ev.save(update_fields=["is_voided"])
        elif ev:
            created += 1
    return created


# ─────────────────────────────────────────────────────────────
# MAP : évaluations → BSQ (points + contrainte de durée)
# ─────────────────────────────────────────────────────────────
def sync_evaluations(pool, evaluations, users=None):
    """Évaluations 42 → BSQ (+ durée), Shell 00/01, Rushs. Projet = nom résolu."""
    users = users or _users(pool)
    created = 0
    for ev in evaluations:
        u = users.get(ev.get("login"))
        if not u:
            continue
        proj = (ev.get("project") or "").lower()
        b, e = _parse(ev.get("begin_at")), _parse(ev.get("end_at"))
        occurred = b or timezone.now()
        dur = round((e - b).total_seconds() / 60) if b and e else 0
        mark = ev.get("mark") if ev.get("mark") is not None else 0

        if "bsq" in proj:
            # Une seule éval BSQ comptée par étudiant, même si le projet a été
            # corrigé plusieurs fois (cohérent avec Shell/Rush/projets, dédupliqués
            # par projet et non par correction).
            if record_event(user=u, pool=pool, rule_key="bsq_eval", occurred_at=occurred,
                            context={}, source=API, dedup_key=f"bsq:{u.login}:{proj}"):
                created += 1
            if record_event(user=u, pool=pool, rule_key="bsq_duration", occurred_at=occurred,
                            context={"duration_min": dur}, source=API,
                            dedup_key=f"bsqdur:{u.login}:{proj}"):
                created += 1
        elif "shell" in proj and ("00" in proj or "01" in proj):
            # Shell 00/01 : malus UNIQUEMENT si la note vaut exactement 100.
            if mark == 100 and record_event(
                    user=u, pool=pool, rule_key="shell_malus", occurred_at=occurred,
                    context={"project": proj, "mark": mark}, source=API,
                    dedup_key=f"shell:{u.login}:{proj}"):
                created += 1
        elif "rush" in proj:
            if record_event(user=u, pool=pool, rule_key="rush_malus", occurred_at=occurred,
                            context={"mark": mark}, source=API,
                            dedup_key=f"rush:{u.login}:{proj}"):
                created += 1
        else:
            # Projet évalué générique → points random ±, UNE SEULE fois par projet
            # (dedup) et SEULEMENT si le projet est VALIDÉ (note ≥ seuil). Une
            # correction ratée ne crée rien → elle ne consomme pas le dedup, donc
            # une re-correction réussie plus tard pourra bien attribuer les points.
            if mark >= _PROJECT_PASS_MARK and record_event(
                    user=u, pool=pool, rule_key="project_random", occurred_at=occurred,
                    context={"project": proj, "mark": mark}, source=API,
                    dedup_key=f"proj:{u.login}:{proj}"):
                created += 1
            # bonus si le projet est validé PILE à 100
            if mark == 100 and record_event(
                    user=u, pool=pool, rule_key="project_perfect", occurred_at=occurred,
                    context={"mark": mark}, source=API,
                    dedup_key=f"perfect:{u.login}:{proj}"):
                created += 1
    return created


def sync_flags(pool, flags, users=None):
    """
    Flags de correction négatifs (Empty work, Crash, Norme, Can't explain…).

    Deux corrections bêtes évitées ici :
      - l'event est daté de la CORRECTION (begin_at), pas de l'heure de synchro
        (sinon tous les flags s'empilent sur le jour du fetch, hors piscine) ;
      - un même flag sur un même projet ne compte QU'UNE fois par étudiant, même
        si le projet a été corrigé plusieurs fois (cohérent avec les évaluations,
        dédupliquées par projet).
    """
    users = users or _users(pool)
    created = 0
    for fl in flags:
        u = users.get(fl.get("login"))
        if not u:
            continue
        proj = (fl.get("project") or "").lower()
        flag_name = fl.get("flag") or ""
        dedup = (f"flag:{u.login}:{proj}:{flag_name.lower()}" if proj
                 else f"flag:{fl.get('id')}")
        ev = record_event(user=u, pool=pool, rule_key="correction_flag",
                          occurred_at=_parse(fl.get("at")) or timezone.now(),
                          context={"flag": flag_name, "project": proj}, source=API,
                          dedup_key=dedup)
        if ev and ev.raw_points == 0:  # flag non pénalisant → on ne garde pas
            ev.is_voided = True
            ev.save(update_fields=["is_voided"])
        elif ev:
            created += 1
    return created


def sync_host_effects(pool, day, locations, pairs=None, users=None):
    """
    Places Bénites/Maudites, effet « à la connexion » : l'étudiant qui S'ASSOIT
    (se connecte) sur une place shiny/cursed du jour reçoit le bonus/malus.

    UNE SEULE FOIS par personne et par jour : se déconnecter puis se reconnecter
    sur la même place (ou sur une autre place du même type) ne redonne pas de
    points. Le dedup_key `{tag}:{login}:{jour}` fige le premier effet du jour.
    `pairs` n'est plus utilisé (effet lié à la présence, pas à la correction) mais
    reste dans la signature pour les appelants existants.
    """
    users = users or _users(pool)
    kinds = {dh.hostname: dh.kind for dh in DailyHost.objects.filter(pool=pool, day=day)}
    if not kinds:
        return 0
    # première place spéciale sur laquelle chaque étudiant s'est assis ce jour-là,
    # avec l'heure RÉELLE de cette connexion (au lieu d'un midi figé).
    shiny_host_of, cursed_host_of = {}, {}
    for loc in locations:
        login = loc.get("login")
        k = kinds.get(loc.get("host") or "")
        if not login or not k:
            continue
        when = _parse(loc.get("begin_at"))
        if k == "shiny" and login not in shiny_host_of:
            shiny_host_of[login] = (loc.get("host"), when)
        elif k == "cursed" and login not in cursed_host_of:
            cursed_host_of[login] = (loc.get("host"), when)

    created = 0
    seats = ([("shiny_host", "shinyhost", lg, h, w) for lg, (h, w) in shiny_host_of.items()]
             + [("cursed_host", "cursedhost", lg, h, w) for lg, (h, w) in cursed_host_of.items()])
    for rule_key, tag, login, host, when in seats:
        u = users.get(login)
        if not u:
            continue
        occurred = _cap_to_day(when, day) if when else \
            timezone.make_aware(datetime.combine(day, dtime(12, 0)))
        if record_event(user=u, pool=pool, rule_key=rule_key, occurred_at=occurred,
                        context={"host": host}, source=API,
                        dedup_key=f"{tag}:{login}:{day.isoformat()}"):
            created += 1
    return created


def sync_exam_time(pool, day, locations, windows, users=None):
    """
    Temps passé en examen = chevauchement entre la présence (locations) et les
    fenêtres d'examen du jour. Zéro appel API supplémentaire par étudiant :
    les fenêtres sont campus-wide, l'overlap se calcule sur les locations déjà
    fetchées. Agrégat journalier (voidé/réécrit) → idempotent.
    """
    users = users or _users(pool)
    todays = [(b, e) for b, e in windows if timezone.localdate(b) == day]
    if not todays:
        return 0
    mins = defaultdict(float)
    for loc in locations:
        u = users.get(loc.get("login"))
        if not u:
            continue
        lb = _parse(loc.get("begin_at"))
        if not lb:
            continue
        le = _parse(loc.get("end_at")) or timezone.now()
        for wb, we in todays:
            ov = (min(le, we) - max(lb, wb)).total_seconds() / 60
            if ov > 0:
                mins[u.id] += ov
    id_to_user = {u.id: u for u in users.values()}
    occurred = timezone.make_aware(datetime.combine(day, dtime(12, 0)))
    created = 0
    for uid, m in mins.items():
        u = id_to_user[uid]
        _void_daily(u, pool, "exam_time", day)
        record_event(user=u, pool=pool, rule_key="exam_time", occurred_at=occurred,
                     context={"minutes": round(m)}, source=API)
        created += 1
    return created


def sync_last_day(pool, day, pairs, users=None):
    """
    Dernier jour de la piscine : chaque correction DONNÉE rapporte factor points
    au correcteur (règle last_day_corrections, multiplier sur le compte).
    """
    if day != pool.ends_on:
        return 0
    users = users or _users(pool)
    counts = defaultdict(int)
    for corrector, *_ in pairs:
        counts[corrector] += 1
    occurred = timezone.make_aware(datetime.combine(day, dtime(12, 0)))
    created = 0
    for login, n in counts.items():
        u = users.get(login)
        if not u:
            continue
        _void_daily(u, pool, "last_day_corrections", day)
        record_event(user=u, pool=pool, rule_key="last_day_corrections",
                     occurred_at=occurred, context={"correction_points": n}, source=API)
        created += 1
    return created


def fetch_trolls(diagnostic=None):
    """
    Récupère le journal des trolls depuis le Google Sheet (Apps Script).
    URL + mot de passe vivent dans .env (settings) — jamais dans le code.
    Renvoie des dicts normalisés : {at, troll, author, victim, level,
    destructive, permanent}. Liste vide si non configuré ou si l'endpoint échoue.

    `diagnostic` (dict optionnel) : on y écrit l'état pour le débogage prod
    (configured, http_status, rows, error) — cf. management command ft_trolls.
    """
    import requests
    from django.conf import settings as dj_settings
    diag = diagnostic if diagnostic is not None else {}
    url = dj_settings.TROLL_SHEET_URL
    pwd = dj_settings.TROLL_SHEET_PASSWORD
    diag["configured"] = bool(url and pwd)
    if not url or not pwd:
        # Cause n°1 en prod : le .env de prod n'a pas TROLL_SHEET_URL/PASSWORD
        # (le .env est gitignoré → il faut les ajouter côté serveur + recreate).
        log.warning("fetch_trolls: TROLL_SHEET_URL/PASSWORD absent du .env — aucun troll fetché")
        diag["error"] = "TROLL_SHEET_URL/PASSWORD non configuré (.env)"
        return []
    try:
        resp = requests.get(url, params={"password": pwd}, timeout=20)
        diag["http_status"] = resp.status_code
        resp.raise_for_status()
        rows = resp.json()
    except Exception as ex:  # noqa: BLE001 — sheet KO ⇒ on n'ingère rien
        log.warning("fetch_trolls: échec HTTP/réseau vers le Google Sheet: %s", ex)
        diag["error"] = str(ex)
        return []
    out = []
    for row in rows[1:]:  # ligne 0 = en-têtes ; colonnes >7 = stats annexes, ignorées
        if not row or not row[0]:
            continue
        try:
            out.append({"at": str(row[0]), "troll": str(row[1] or ""),
                        "author": str(row[2] or ""), "victim": str(row[3] or ""),
                        "level": int(float(row[4] or 1)),
                        "destructive": bool(row[5]), "permanent": bool(row[6])})
        except (ValueError, TypeError, IndexError):
            continue
    diag["rows"] = len(out)
    return out


def sync_trolls(pool, trolls, users=None):
    """
    Victime trollée → malus `troll_victim` (points par niveau × multiplicateur
    de la semaine de piscine, cf. évaluateur level_week). Idempotent par
    (horodatage, victime, nom du troll) ; renvoie (créés, 1er jour touché) pour
    permettre un recompute si des events atterrissent sur des jours déjà figés.
    """
    users = users or _users(pool)
    created, first_day = 0, None
    for t in trolls:
        u = users.get(t.get("victim"))
        at = _parse(t.get("at"))
        if not u or not at:
            continue
        day = timezone.localdate(at)
        if not (pool.starts_on <= day <= pool.ends_on):
            continue  # troll hors piscine (autre session) — ignoré
        week = min(4, max(1, (day - pool.starts_on).days // 7 + 1))
        if record_event(
                user=u, pool=pool, rule_key="troll_victim", occurred_at=at,
                context={"level": t.get("level", 1), "week": week,
                         "troll": t.get("troll", ""), "author": t.get("author", "")},
                source=API,
                dedup_key=f"troll:{t.get('at')}:{t.get('victim')}:{t.get('troll')}"[:200]):
            created += 1
            if first_day is None or day < first_day:
                first_day = day
    return created, first_day


def sync_all(pool, data, *, score_cumulative=False):
    """
    Ingestion « jour courant » (poll live). score_cumulative=False par défaut :
    le logtime N'EST PAS scoré tant que le jour n'est pas terminé (il tombera à
    minuit, cf. tasks.nightly_snapshot). Le mode démo peut forcer True pour
    montrer le logtime immédiatement.
    """
    from .chaos import spread_plague
    users = _users(pool)
    pairs = data.get("pairs", [])
    locations = data.get("locations", [])
    today = timezone.localdate()
    return {
        "locations": sync_locations(pool, locations, users, score_cumulative=score_cumulative),
        "feedbacks": sync_feedbacks(pool, data.get("feedbacks", []), users),
        "evaluations": sync_evaluations(pool, data.get("evaluations", []), users),
        "flags": sync_flags(pool, data.get("flags", []), users),
        "hosts": sync_host_effects(pool, today, locations, pairs, users),
        # exam_time = malus basé sur le TEMPS → jour final uniquement (comme le logtime)
        "exams": (sync_exam_time(pool, today, locations, data.get("exam_windows", []), users)
                  if score_cumulative else 0),
        "last_day": sync_last_day(pool, today, pairs, users),
        "plague": spread_plague(pool, pairs),
    }


# ─────────────────────────────────────────────────────────────
# FETCH (live) — normalisation des réponses 42
# ─────────────────────────────────────────────────────────────
def fetch_locations_range(client, campus_id, start_iso, end_iso):
    """
    Locations d'un campus dont begin_at ∈ [start_iso, end_iso) — données réelles.
    Sert au rejeu jour par jour d'une piscine passée.
    """
    params = {"range[begin_at]": f"{start_iso},{end_iso}"}
    endpoint = f"/v2/campus/{campus_id}/locations"
    return [
        {"id": l.get("id"),
         "login": (l.get("user") or {}).get("login"),
         "begin_at": l.get("begin_at"),
         "end_at": l.get("end_at"),
         "host": l.get("host")}
        for l in client.paginate(endpoint, params, page_size=100)
    ]


_PROJECT_CACHE = {}


def _project_name(client, project_id):
    """Résout project_id → nom de projet (mis en cache : ~15 projets par piscine)."""
    if project_id in _PROJECT_CACHE:
        return _PROJECT_CACHE[project_id]
    name = ""
    try:
        name = client.get(f"/v2/projects/{project_id}").json().get("name", "")
    except Exception:  # noqa: BLE001
        name = ""
    _PROJECT_CACHE[project_id] = name
    return name


def fetch_scale_teams_range(client, campus_id, start_iso, end_iso, cursus_id=None):
    """
    Corrections d'un campus sur [start,end). Une correction (scale_team) donne :
      - un feedback (comment écrit par le correcteur → mots-clés),
      - une évaluation par corrigé (projet + durée + note → BSQ/Shell/Rush),
      - éventuellement un flag négatif (Empty work, Crash…).
    cursus_id (9 = C Piscine) isole la piscine du reste du campus.
    """
    params = {"filter[campus_id]": campus_id, "range[begin_at]": f"{start_iso},{end_iso}"}
    if cursus_id:
        params["filter[cursus_id]"] = cursus_id
    feedbacks, evaluations, flags, pairs = [], [], [], []
    for st in client.paginate("/v2/scale_teams", params, page_size=100):
        sid = st.get("id")
        corrector = (st.get("corrector") or {}).get("login")
        comment = st.get("comment")
        begin, filled = st.get("begin_at"), st.get("filled_at")
        if corrector and comment:
            feedbacks.append({"id": sid, "login": corrector, "comment": comment, "created_at": filled})

        # Un scale_team NON REMPLI (filled_at absent) = correction jamais réalisée :
        # booking expiré ou projet « give up » tout seul après ~1 jour sans
        # correcteur. On n'en tire NI éval, NI flag, NI couple de correction —
        # sinon un give-up collait un flag bidon (ex : « Norme » sur shell 00,
        # qui n'a pourtant pas de norme). Une vraie correction a toujours filled_at.
        if not filled:
            continue

        team = st.get("team") or {}
        project = _project_name(client, team.get("project_id"))
        mark = team.get("final_mark")
        flag = st.get("flag") or {}
        flag_name = flag.get("name")
        flag_neg = flag_name and not flag.get("positive", True)

        for c in (st.get("correcteds") or []):
            login = (c or {}).get("login")
            if not login:
                continue
            # couple correcteur↔corrigé (propagation Peste & Choléra + places)
            if corrector and corrector != login:
                pairs.append((corrector, login, sid))
            if project:
                evaluations.append({"id": f"{sid}:{login}", "login": login, "project": project,
                                    "begin_at": begin, "end_at": filled, "mark": mark})
            if flag_neg:
                flags.append({"id": f"{sid}:{login}", "login": login, "flag": flag_name,
                              "project": project, "at": begin})
    return {"feedbacks": feedbacks, "evaluations": evaluations, "flags": flags, "pairs": pairs}


def fetch_campuses(client):
    """Liste (id, nom) des campus — pour trouver l'ID du Havre."""
    return [(c.get("id"), c.get("name")) for c in client.paginate("/v2/campus", page_size=100)]


def fetch_campus_users(client, campus_id, pool_year=None, pool_month=None, include_staff=False):
    """
    Récupère les étudiants d'une session de piscine via /v2/users et les filtres
    documentés : primary_campus_id (campus PRINCIPAL), pool_year, pool_month,
    et staff? (exclu par défaut). Doublé d'un filtre client par sécurité.
    """
    params = {"filter[primary_campus_id]": campus_id}
    if pool_year:
        params["filter[pool_year]"] = pool_year
    if pool_month:
        params["filter[pool_month]"] = pool_month
    if not include_staff:
        params["filter[staff?]"] = "false"

    out = []
    for u in client.paginate("/v2/users", params, page_size=100):
        if pool_year and str(u.get("pool_year")) != str(pool_year):
            continue
        if pool_month and str(u.get("pool_month") or "").lower() != str(pool_month).lower():
            continue
        if not include_staff and u.get("staff?"):
            continue
        out.append({
            "intra_id": u.get("id"),
            "login": u.get("login"),
            "display_name": u.get("displayname") or u.get("usual_full_name") or "",
        })
    return out


MONTHS_EN = ["", "January", "February", "March", "April", "May", "June",
             "July", "August", "September", "October", "November", "December"]


def _cluster_exam_dates(pairs, gap_days=20, start_offset_days=5):
    """
    pairs = [(begin_date, end_date), …] d'examens. Regroupe en SESSIONS : un
    écart > gap_days entre deux examens consécutifs ouvre une nouvelle piscine
    (les examens d'une même piscine sont ~hebdomadaires). Pour chaque session :
    début = 1er examen − start_offset_days, fin = dernier examen.
    Retourne [{starts_on, ends_on, exams}] trié du plus récent au plus ancien.
    """
    pairs = sorted(pairs)
    if not pairs:
        return []
    clusters = [[pairs[0]]]
    for b, e in pairs[1:]:
        if (b - clusters[-1][-1][0]).days > gap_days:
            clusters.append([(b, e)])
        else:
            clusters[-1].append((b, e))
    out = []
    for cl in clusters:
        first = min(b for b, _ in cl)
        last = max(e for _, e in cl)
        out.append({"starts_on": first - timedelta(days=start_offset_days),
                    "ends_on": last, "exams": len(cl)})
    out.sort(key=lambda s: s["starts_on"], reverse=True)
    return out


def fetch_exam_windows(client, campus_id, cursus_id=None):
    """
    Fenêtres [(begin_dt, end_dt)] des examens du cursus sur le campus — sert au
    calcul du temps passé en examen (overlap avec les locations).
    """
    out = []
    for e in client.paginate(f"/v2/campus/{campus_id}/exams", page_size=100):
        cursus_ids = e.get("cursus_ids")
        if not cursus_ids:
            cursus_ids = [c.get("id") for c in (e.get("cursus") or [])]
        if cursus_id and cursus_id not in cursus_ids:
            continue
        b, en = _parse(e.get("begin_at")), _parse(e.get("end_at"))
        if b and en:
            out.append((b, en))
    return out


def fetch_exam_sessions(client, campus_id, cursus_id):
    """
    Découvre les sessions de piscine d'un campus À PARTIR DES EXAMENS.
    Fiable (contrairement à pool_month, qui remonte des étudiants ayant fait leur
    piscine ailleurs → faux mois type « Février »). On isole le cursus C Piscine,
    on regroupe les examens rapprochés en sessions et on en déduit des dates
    précises. Retourne [{name, starts_on, ends_on, exams}].
    """
    pairs = []
    for e in client.paginate(f"/v2/campus/{campus_id}/exams", page_size=100):
        cursus_ids = e.get("cursus_ids")
        if not cursus_ids:
            cursus_ids = [c.get("id") for c in (e.get("cursus") or [])]
        if cursus_id and cursus_id not in cursus_ids:
            continue
        begin = e.get("begin_at")
        if not begin:
            continue
        end = e.get("end_at") or begin
        try:
            pairs.append((date.fromisoformat(begin[:10]), date.fromisoformat(end[:10])))
        except ValueError:
            continue
    sessions = _cluster_exam_dates(pairs)
    for s in sessions:
        d = s["starts_on"]
        s["name"] = f"Piscine {MONTHS_EN[d.month]} {d.year} (campus {campus_id})"
    return sessions


def sync_users(pool, users_data):
    """
    Crée/actualise les AppUser de la Piscine à partir des données 42.

    GARDE-FOU : un user EXISTANT n'est JAMAIS déplacé vers une autre piscine —
    seule son identité (intra_id, nom) est rafraîchie. Avant : update_or_create
    écrasait `pool`, et un fetch de cohorte mal ciblé (cron avec l'année/mois
    globaux du .env) FUSIONNAIT silencieusement deux piscines en une.
    """
    created = updated = skipped = 0
    for u in users_data:
        login = u.get("login")
        if not login:
            continue
        obj, was_created = AppUser.objects.get_or_create(
            login=login,
            defaults={"pool": pool, "intra_id": u.get("intra_id"),
                      "display_name": u.get("display_name", "")},
        )
        if was_created:
            created += 1
            continue
        if obj.pool_id != pool.id:  # appartient à une AUTRE piscine → on ne touche pas
            skipped += 1
            continue
        fields = []
        if u.get("intra_id") and obj.intra_id != u["intra_id"]:
            obj.intra_id = u["intra_id"]
            fields.append("intra_id")
        if u.get("display_name") and obj.display_name != u["display_name"]:
            obj.display_name = u["display_name"]
            fields.append("display_name")
        if fields:
            obj.save(update_fields=fields)
        updated += 1
    return {"created": created, "updated": updated, "skipped_other_pool": skipped}


def _day_bounds_utc(day):
    """Bornes UTC [00:00, 24:00) d'un jour local, au format attendu par l'API."""
    tz = timezone.get_current_timezone()
    start = datetime.combine(day, dtime.min, tzinfo=tz).astimezone(dttz.utc)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def fetch_day(client, campus_id, day, cursus_id=None, parallel=False,
              loc_lookback_days=0):
    """
    Récupère TOUTES les données d'un jour, en séparant clairement les deux
    familles de requêtes :
      - LOGTIMES (locations) : très lourdes (tout le campus présent) ;
      - PROJETS/EXAMS (scale_teams) : corrections, feedbacks, flags.
    parallel=True lance les deux familles en parallèle (2 threads) — utile pour
    le live (1 seul jour). En rejeu multi-jours on préfère False et on
    parallélise plutôt AU NIVEAU DES JOURS (voir sync_runner) pour ne pas
    sur-solliciter le pool de clés.

    loc_lookback_days : élargit la fenêtre des LOCATIONS de N jours en arrière
    (l'API filtre sur begin_at). Nécessaire pour scorer un jour terminé : une
    session commencée la veille à 23 h et finie à 3 h n'a pas de begin_at dans
    le jour cible — sans lookback elle est invisible et le logtime du jour est
    faux (cf. _split_by_day). Les scale_teams gardent la fenêtre du jour seul.
    """
    start, end = _day_bounds_utc(day)
    loc_start = start if not loc_lookback_days else \
        _day_bounds_utc(day - timedelta(days=loc_lookback_days))[0]
    if parallel:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_loc = ex.submit(fetch_locations_range, client, campus_id, loc_start, end)
            f_scale = ex.submit(fetch_scale_teams_range, client, campus_id, start, end,
                                cursus_id=cursus_id)
            locations, scale = f_loc.result(), f_scale.result()
    else:
        locations = fetch_locations_range(client, campus_id, loc_start, end)
        scale = fetch_scale_teams_range(client, campus_id, start, end, cursus_id=cursus_id)
    return {"locations": locations, **scale}


def fetch_live(client, campus_id=0, day=None, cursus_id=None):
    """
    Mode temps réel : récupère les données du JOUR courant (logtimes + corrections)
    EN PARALLÈLE. On refetch le jour entier à chaque poll → le logtime journalier
    reste exact (agrégat réécrit) et les corrections sont dédupliquées.
    """
    day = day or timezone.localdate()
    return fetch_day(client, campus_id, day, cursus_id=cursus_id, parallel=True)


# ─────────────────────────────────────────────────────────────
# Données de démo (mode --demo) — datées d'aujourd'hui
# ─────────────────────────────────────────────────────────────
def demo_payload():
    now = timezone.now()
    today = timezone.localdate()

    def at(h, m=0):
        return timezone.make_aware(datetime.combine(today, dtime(h, m))).isoformat()

    return {
        "locations": [
            # abelle : logtime bas (~90 min) → gros gain
            {"id": 1, "login": "abelle", "begin_at": at(10), "end_at": at(11, 30), "host": "e1r2p3"},
            # bnguyen : reconnexion MÊME pc (2 sessions, host identique) → malus
            {"id": 2, "login": "bnguyen", "begin_at": at(9), "end_at": at(12), "host": "e1r2p5"},
            {"id": 3, "login": "bnguyen", "begin_at": at(14), "end_at": at(18), "host": "e1r2p5"},
            # cdupont : connexion à 23:55 → bonus minuit
            {"id": 4, "login": "cdupont", "begin_at": at(23, 55), "end_at": at(23, 59), "host": "e2r1p1"},
            # dmartin : très grosse présence (~13h) → logtime haut (malus)
            {"id": 5, "login": "dmartin", "begin_at": at(8), "end_at": at(21), "host": "e2r1p2"},
        ],
        "feedbacks": [
            {"id": 11, "login": "abelle", "comment": "trop bien ce projet quoi feur", "created_at": now.isoformat()},
            {"id": 12, "login": "erossi", "comment": "c'était nul quoi", "created_at": now.isoformat()},
            {"id": 13, "login": "dmartin", "comment": "coubeh franchement", "created_at": now.isoformat()},
        ],
        "evaluations": [
            {"id": 21, "login": "abelle", "project": "BSQ", "begin_at": at(15), "end_at": at(15, 45)},  # 45min OK
            {"id": 22, "login": "erossi", "project": "BSQ", "begin_at": at(16), "end_at": at(16, 20)},  # 20min → malus durée
        ],
    }
