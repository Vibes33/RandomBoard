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
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone as dttz

from django.utils import timezone

from .engine import record_event
from .models import AppUser, EventLog, HostConfig, Pool

log = logging.getLogger(__name__)
API = EventLog.Source.API_42


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


# ─────────────────────────────────────────────────────────────
# MAP : locations → logtime, midnight bonus, reconnexion même PC
# ─────────────────────────────────────────────────────────────
def sync_locations(pool, locations, users=None):
    users = users or _users(pool)
    minutes = defaultdict(float)
    hosts = defaultdict(list)
    created = 0

    for loc in locations:
        u = users.get(loc.get("login"))
        if not u:
            continue
        b = _parse(loc.get("begin_at"))
        if not b:
            continue
        e = _parse(loc.get("end_at")) or timezone.now()
        day = timezone.localdate(b)
        minutes[(u.id, day)] += max(0.0, (e - b).total_seconds() / 60)
        hosts[(u.id, day)].append(loc.get("host", ""))

        # bonus connexion 23:50–23:59
        local = timezone.localtime(b)
        if dtime(23, 50) <= local.time() <= dtime(23, 59):
            if record_event(user=u, pool=pool, rule_key="midnight_bonus", occurred_at=b,
                            context={"time": local.strftime("%H:%M")}, source=API,
                            dedup_key=f"midnight:{loc.get('id')}"):
                created += 1

        # poste Shiny / Maudit
        hc = HostConfig.objects.filter(hostname=loc.get("host", ""), is_active=True).first()
        if hc:
            rule_key = "shiny_host" if hc.kind == HostConfig.Kind.SHINY else "cursed_host"
            if record_event(user=u, pool=pool, rule_key=rule_key, occurred_at=b,
                            context={"points": hc.params.get("points", 0), "host": hc.hostname},
                            source=API, dedup_key=f"host:{loc.get('id')}"):
                created += 1

    id_to_user = {u.id: u for u in users.values()}
    for (uid, day), mins in minutes.items():
        u = id_to_user[uid]
        occurred = timezone.make_aware(datetime.combine(day, dtime(12, 0)))
        _void_daily(u, pool, "logtime_low", day)
        record_event(user=u, pool=pool, rule_key="logtime_low", occurred_at=occurred,
                     context={"minutes": round(mins)}, source=API)
        created += 1
        if mins >= 720:  # logtime haut (≥12h)
            _void_daily(u, pool, "logtime_high", day)
            record_event(user=u, pool=pool, rule_key="logtime_high", occurred_at=occurred,
                         context={"minutes": round(mins)}, source=API)
            created += 1

    # reconnexion sur le MÊME pc dans la journée
    for (uid, day), hlist in hosts.items():
        if len(hlist) != len(set(hlist)):
            u = id_to_user[uid]
            occurred = timezone.make_aware(datetime.combine(day, dtime(12, 0)))
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
            if record_event(user=u, pool=pool, rule_key="bsq_eval", occurred_at=occurred,
                            context={}, source=API, dedup_key=f"eval:{ev.get('id')}"):
                created += 1
            if record_event(user=u, pool=pool, rule_key="bsq_duration", occurred_at=occurred,
                            context={"duration_min": dur}, source=API,
                            dedup_key=f"evaldur:{ev.get('id')}"):
                created += 1
        elif "shell" in proj and ("00" in proj or "01" in proj):
            # « rendre Shell 00/01 » = 1 malus par étudiant & par projet (pas par correction)
            if record_event(user=u, pool=pool, rule_key="shell_malus", occurred_at=occurred,
                            context={"project": proj}, source=API,
                            dedup_key=f"shell:{u.login}:{proj}"):
                created += 1
        elif "rush" in proj:
            if record_event(user=u, pool=pool, rule_key="rush_malus", occurred_at=occurred,
                            context={"mark": mark}, source=API,
                            dedup_key=f"rush:{u.login}:{proj}"):
                created += 1
        else:
            # tout autre projet évalué → points random ± (1 fois par étudiant & projet)
            if record_event(user=u, pool=pool, rule_key="project_random", occurred_at=occurred,
                            context={"project": proj}, source=API,
                            dedup_key=f"proj:{u.login}:{proj}"):
                created += 1
    return created


def sync_flags(pool, flags, users=None):
    """Flags de correction négatifs (Empty work, Crash, Norme, Can't explain…)."""
    users = users or _users(pool)
    created = 0
    for fl in flags:
        u = users.get(fl.get("login"))
        if not u:
            continue
        ev = record_event(user=u, pool=pool, rule_key="correction_flag", occurred_at=timezone.now(),
                          context={"flag": fl.get("flag")}, source=API,
                          dedup_key=f"flag:{fl.get('id')}")
        if ev and ev.raw_points == 0:  # flag non pénalisant → on ne garde pas
            ev.is_voided = True
            ev.save(update_fields=["is_voided"])
        elif ev:
            created += 1
    return created


def sync_all(pool, data):
    users = _users(pool)
    return {
        "locations": sync_locations(pool, data.get("locations", []), users),
        "feedbacks": sync_feedbacks(pool, data.get("feedbacks", []), users),
        "evaluations": sync_evaluations(pool, data.get("evaluations", []), users),
        "flags": sync_flags(pool, data.get("flags", []), users),
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
    feedbacks, evaluations, flags = [], [], []
    for st in client.paginate("/v2/scale_teams", params, page_size=100):
        sid = st.get("id")
        corrector = (st.get("corrector") or {}).get("login")
        comment = st.get("comment")
        begin, filled = st.get("begin_at"), st.get("filled_at")
        if corrector and comment:
            feedbacks.append({"id": sid, "login": corrector, "comment": comment, "created_at": filled})

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
            if project:
                evaluations.append({"id": f"{sid}:{login}", "login": login, "project": project,
                                    "begin_at": begin, "end_at": filled, "mark": mark})
            if flag_neg:
                flags.append({"id": f"{sid}:{login}", "login": login, "flag": flag_name})
    return {"feedbacks": feedbacks, "evaluations": evaluations, "flags": flags}


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


def sync_users(pool, users_data):
    """Crée/actualise les AppUser de la Piscine à partir des données 42."""
    created = updated = 0
    for u in users_data:
        login = u.get("login")
        if not login:
            continue
        obj, was_created = AppUser.objects.update_or_create(
            login=login,
            defaults={"pool": pool, "intra_id": u.get("intra_id"),
                      "display_name": u.get("display_name", "")},
        )
        created += int(was_created)
        updated += int(not was_created)
    return {"created": created, "updated": updated}


def _day_bounds_utc(day):
    """Bornes UTC [00:00, 24:00) d'un jour local, au format attendu par l'API."""
    tz = timezone.get_current_timezone()
    start = datetime.combine(day, dtime.min, tzinfo=tz).astimezone(dttz.utc)
    end = start + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start.strftime(fmt), end.strftime(fmt)


def fetch_live(client, campus_id=0, day=None, cursus_id=None):
    """
    Mode temps réel : récupère les données du JOUR courant (locations + corrections).
    On refetch le jour entier à chaque poll → le logtime journalier reste exact
    (agrégat réécrit) et les corrections sont dédupliquées.
    """
    day = day or timezone.localdate()
    start, end = _day_bounds_utc(day)
    locations = fetch_locations_range(client, campus_id, start, end)
    scale = fetch_scale_teams_range(client, campus_id, start, end, cursus_id=cursus_id)
    return {"locations": locations, **scale}


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
