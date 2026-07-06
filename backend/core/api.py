"""
Panel admin — API JSON (reconstruite de zéro).

Onglets : Piscines · Users · Logs · Options de Points · Gestion par Jour · Places & Hosts.
Tout est staff-only + CSRF, opère sur la Piscine active.

Modèle de scoring : UN multiplicateur par (jour × type d'event).
    day_final = Σ events [ raw_points × multiplier(jour, règle) ]   (absent ⇒ ×1)
"""
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from .auth import staff_required
from .derived import randomize_daily_hosts
from .engine import new_rule_version
from .models import (
    AppUser, DailyDesignation, DailyEventMultiplier, DailyHost, EventLog,
    Infection, Pool, Rule, SyncRun, Workstation,
)
from .services import (
    adjust_user_score, day_multipliers, randomize_day_multipliers,
    reapply_rule_points, recompute_from, standings,
)


# ─────────────────────────── helpers ───────────────────────────
def _pool():
    return Pool.objects.filter(is_active=True).order_by("-starts_on").first()


def _json(request):
    try:
        return json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return {}


def _dec(value):
    return Decimal(str(value))


def _rule_cards(pool=None):
    """
    Sérialise les règles ACTIVES pour les onglets Points/Jour : pour chaque règle,
    la liste de ses champs éditables (schéma central RULE_FIELDS) avec leur valeur
    courante, plus son range de multiplicateur. Rend tous les points éditables.
    """
    from .rules_config import RULE_FIELDS
    cards = []
    for r in Rule.objects.filter(is_active=True).order_by("category", "label"):
        p = (r.current_version.params if r.current_version else {}) or {}
        fields = []
        for spec in RULE_FIELDS.get(r.key, []):
            val = p.get(spec["name"])
            if spec["kind"] == "map":
                val = dict(val or {})
            fields.append({**spec, "value": val})
        cards.append({
            "id": r.id, "key": r.key, "label": r.label, "category": r.category,
            "fields": fields,
            "mult_min": float(r.mult_min), "mult_max": float(r.mult_max),
        })
    return cards


# ─────────────────────────── page ───────────────────────────
@staff_required
@ensure_csrf_cookie
def panel(request):
    pool = _pool()
    return render(request, "core/panel.html", {
        "pool": pool,
        "pool_period": (f"{pool.starts_on:%d/%m/%Y} → {pool.ends_on:%d/%m/%Y}" if pool else "—"),
    })


# ─────────────────────────── 0. Piscines ───────────────────────────
@staff_required
def api_pools(request):
    """Liste des piscines (avec filtres) + années disponibles pour le filtre."""
    qs = Pool.objects.all().order_by("-starts_on")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(slug__icontains=q))
    year = request.GET.get("year", "").strip()
    if year.isdigit():
        qs = qs.filter(starts_on__year=int(year))
    if request.GET.get("active") in ("1", "true"):
        qs = qs.filter(is_active=True)
    qs = qs.annotate(n_users=Count("users", distinct=True))

    events = {r["pool"]: r["n"]
              for r in EventLog.objects.values("pool").annotate(n=Count("id"))}
    pools = [{
        "id": p.id, "name": p.name, "slug": p.slug,
        "starts_on": str(p.starts_on), "ends_on": str(p.ends_on),
        "campus": p.campus_id, "cursus": p.cursus_id,
        "is_active": p.is_active, "users": p.n_users, "events": events.get(p.id, 0),
    } for p in qs]
    years = sorted({d.year for d in Pool.objects.values_list("starts_on", flat=True)},
                   reverse=True)
    return JsonResponse({"pools": pools, "years": years})


@staff_required
@require_http_methods(["POST"])
def api_pools_discover(request):
    """Va chercher les piscines sur l'API 42 (via les examens du cursus) et crée
    les Pool manquants avec des dates PRÉCISES, tous inactifs. Ne touche jamais à
    la piscine active. Sert de bouton « réactualiser depuis l'API »."""
    from django.utils.text import slugify
    from .ft_api import FtClient
    from .sync import fetch_exam_sessions
    from .sync_runner import resolve_target

    client = FtClient()
    if not client.configured:
        return HttpResponseBadRequest("Aucune clé API configurée (voir .env).")
    campus, cursus = resolve_target()
    if not campus:
        return HttpResponseBadRequest("FT_CAMPUS_ID non défini.")
    try:
        sessions = fetch_exam_sessions(client, campus, cursus)
    except Exception as ex:  # noqa: BLE001
        return HttpResponseBadRequest(f"Erreur API 42 : {ex}")

    created = updated = 0
    for s in sessions:
        slug = slugify(s["name"])[:50]
        pool = Pool.objects.filter(slug=slug).first()
        if pool is None:
            Pool.objects.create(
                slug=slug, name=s["name"], starts_on=s["starts_on"],
                ends_on=s["ends_on"], last_day=s["ends_on"], is_active=False)
            created += 1
        elif not EventLog.objects.filter(pool=pool).exists():
            # piscine pas encore synchronisée → on rafraîchit les dates exactes
            Pool.objects.filter(pk=pool.pk).update(
                starts_on=s["starts_on"], ends_on=s["ends_on"], last_day=s["ends_on"])
            updated += 1
    return JsonResponse({"ok": True, "found": len(sessions),
                         "created": created, "updated": updated})


@staff_required
@require_http_methods(["POST"])
def api_pool_delete(request, pool_id):
    """Supprime une piscine — uniquement si elle est vide (0 event) et non active,
    pour nettoyer les découvertes parasites sans jamais effacer de données."""
    pool = Pool.objects.filter(id=pool_id).first()
    if not pool:
        return HttpResponseBadRequest("Piscine introuvable.")
    if pool.is_active:
        return HttpResponseBadRequest("Impossible de supprimer la piscine active.")
    if EventLog.objects.filter(pool=pool).exists():
        return HttpResponseBadRequest("Piscine non vide (des events existent).")
    name = pool.name
    pool.delete()
    return JsonResponse({"ok": True, "deleted": name})


@staff_required
@require_http_methods(["POST"])
def api_pool_create(request):
    """
    Crée une piscine custom à tracker : nom + campus_id + cursus_id + dates.
    Elle est créée INACTIVE et apparaît aussitôt dans le sélecteur Workspace.
    """
    from django.utils.text import slugify
    data = _json(request)
    name = (data.get("name") or "").strip()
    if not name:
        return HttpResponseBadRequest("Nom requis.")
    try:
        campus = int(data.get("campus_id"))
        cursus = int(data.get("cursus_id"))
        d_from = date.fromisoformat(data.get("date_from", ""))
        d_to = date.fromisoformat(data.get("date_to", ""))
    except (TypeError, ValueError):
        return HttpResponseBadRequest("Campus/Cursus (entiers) et dates (AAAA-MM-JJ) requis.")
    if d_to < d_from:
        return HttpResponseBadRequest("La date de fin est avant la date de début.")
    slug = slugify(name)[:50]
    if not slug or Pool.objects.filter(slug=slug).exists():
        return HttpResponseBadRequest("Nom invalide ou déjà utilisé.")
    pool = Pool.objects.create(
        name=name, slug=slug, starts_on=d_from, ends_on=d_to, last_day=d_to,
        campus_id=campus, cursus_id=cursus, is_active=False)
    return JsonResponse({"ok": True, "id": pool.id, "name": pool.name})


@staff_required
@require_http_methods(["POST"])
def api_pool_fetch(request, pool_id):
    """
    Lance le FETCH INITIAL d'une piscine en se basant EXCLUSIVEMENT sur ses
    propres campus_id / cursus_id / dates (aucune valeur globale de settings).
    """
    pool = Pool.objects.filter(id=pool_id).first()
    if not pool:
        return HttpResponseBadRequest("Piscine introuvable.")
    if not (pool.campus_id or settings.FT_CAMPUS_ID):
        return HttpResponseBadRequest("Campus ID manquant sur la piscine.")
    if SyncRun.objects.filter(
            status__in=[SyncRun.Status.PENDING, SyncRun.Status.RUNNING]).exists():
        return HttpResponseBadRequest("Une synchronisation est déjà en cours.")
    run = SyncRun.objects.create(
        pool=pool, date_from=pool.starts_on, date_to=pool.ends_on,
        created_by=request.user if request.user.is_authenticated else None)
    from .tasks import run_sync
    run_sync.delay(run.id)
    return JsonResponse({"ok": True, "id": run.id, "pool": pool.name})


@staff_required
@require_http_methods(["POST"])
def api_pool_activate(request, pool_id):
    """Définit UNE piscine active (désactive toutes les autres, atomique)."""
    pool = Pool.objects.filter(id=pool_id).first()
    if not pool:
        return HttpResponseBadRequest("Piscine introuvable.")
    with transaction.atomic():
        Pool.objects.exclude(id=pool.id).update(is_active=False)
        Pool.objects.filter(id=pool.id).update(is_active=True)
    return JsonResponse({"ok": True, "active": pool.name})


# ─────────────────────────── 1. Users ───────────────────────────
@staff_required
def api_users(request):
    pool = _pool()
    if not pool:
        return JsonResponse({"users": []})
    board = {r["login"]: r for r in standings(pool, include_today=True)}
    users = []
    for u in AppUser.objects.filter(pool=pool):
        b = board.get(u.login)
        users.append({"id": u.id, "login": u.login, "name": u.display_name,
                      "total": round(b["total"]) if b else 0})
    users.sort(key=lambda x: x["total"], reverse=True)
    for i, u in enumerate(users, 1):
        u["rank"] = i
    return JsonResponse({"users": users})


@staff_required
@require_http_methods(["POST"])
def api_user_adjust(request, user_id):
    pool = _pool()
    data = _json(request)
    try:
        points = _dec(data.get("points"))
    except (InvalidOperation, TypeError):
        return HttpResponseBadRequest("Nombre de points invalide.")
    # Raison OBLIGATOIRE : chaque ajustement manuel doit être justifié — elle est
    # stockée avec l'event et visible dans les logs globaux + le profil étudiant.
    reason = (data.get("reason") or "").strip()
    if not reason:
        return HttpResponseBadRequest("La raison de l'ajustement est obligatoire.")
    user = AppUser.objects.filter(id=user_id, pool=pool).first()
    if not user:
        return HttpResponseBadRequest("Étudiant introuvable.")
    adjust_user_score(user, pool, points, reason=reason, staff=request.user)
    board = {r["login"]: r for r in standings(pool, include_today=True)}
    b = board.get(user.login)
    return JsonResponse({"ok": True, "total": round(b["total"]) if b else 0})


def _log_detail(event, rule_label, mult, final):
    """Phrase de drill-down : label + métadonnées d'action + effet du multiplicateur."""
    ctx = event.raw_payload or {}
    roll = event.random_roll or {}
    bits = [rule_label]
    if ctx.get("minutes") is not None:
        h, m = divmod(int(ctx["minutes"]), 60)
        bits.append(f"logtime {h}h{m:02d}")
    if ctx.get("streak") is not None:
        bits.append(f"{ctx['streak']} jours consécutifs")
    if ctx.get("cluster"):
        bits.append(f"cluster {ctx['cluster']}")
    if ctx.get("mark") is not None:
        bits.append(f"note {ctx['mark']}")
    if ctx.get("duration_min") is not None:
        bits.append(f"éval en {ctx['duration_min']} min")
    if ctx.get("host"):
        bits.append(f"place {ctx['host']}")
    if ctx.get("corrector"):
        bits.append(f"correcteur sur place spéciale : {ctx['corrector']}")
    if ctx.get("project"):
        bits.append(f"projet {ctx['project']}")
    if ctx.get("flag"):
        bits.append(f"flag « {ctx['flag']} »")
    if ctx.get("reason"):
        bits.append(f"« {ctx['reason']} »")
    if roll.get("matched"):
        m = roll["matched"]
        bits.append("correspond : " + (", ".join(str(x) for x in m)
                                        if isinstance(m, list) else str(m)))
    if roll.get("delta") is not None:
        bits.append(f"aléa {float(roll['delta']):+g}")
    if roll.get("rolled") is not None:
        bits.append(f"tirage {roll['rolled']}")
    if "hit" in roll:
        bits.append("gagné 🎲" if roll["hit"] else "perdu 🎲")
    if roll.get("weight") not in (None, 1):
        bits.append(f"×{roll['weight']} (éval croisée)")
    detail = " · ".join(str(b) for b in bits)
    if float(mult) != 1.0:
        detail += f"  →  ×{float(mult):g} (multiplicateur du jour) = {final:+.0f}"
    return detail


@staff_required
def api_user_logs(request, user_id):
    """
    Profil d'un étudiant : faction + stats + historique chronologique détaillé
    (points bruts, multiplicateur du jour appliqué, points finaux, métadonnées).
    Optimisé : 1 requête events, 1 requête multiplicateurs, 1 classement.
    """
    pool = _pool()
    user = AppUser.objects.filter(id=user_id, pool=pool).first() if pool else None
    if not user:
        return HttpResponseBadRequest("Étudiant introuvable.")

    # faction (Peste & Choléra) : neutre si pas encore infecté
    inf = Infection.objects.filter(pool=pool, user=user).first()
    faction = inf.disease if inf else "neutre"

    # multiplicateurs du pool, chargés en une fois : {(day, rule_id): mult}
    mult_map = {(d, rid): m for d, rid, m in
                DailyEventMultiplier.objects.filter(pool=pool)
                .values_list("day", "rule_id", "multiplier")}

    events = list(EventLog.objects.filter(pool=pool, user=user, is_voided=False)
                  .select_related("rule").order_by("-occurred_at"))

    logs, gains, losses, best, worst, days = [], 0.0, 0.0, None, None, set()
    counts = {}
    for e in events:
        label = e.rule.label if e.rule else e.event_type
        category = e.rule.category if e.rule else "event"
        mult = mult_map.get((e.event_date, e.rule_id), Decimal("1"))
        raw = float(e.raw_points)
        final = round(raw * float(mult), 2)
        gains += final if final > 0 else 0
        losses += final if final < 0 else 0
        days.add(e.event_date)
        counts[label] = counts.get(label, 0) + 1
        if best is None or final > best["final"]:
            best = {"label": label, "final": final}
        if worst is None or final < worst["final"]:
            worst = {"label": label, "final": final}
        logs.append({
            "id": e.id, "at": e.occurred_at.strftime("%d/%m %H:%M"),
            "day": e.event_date.strftime("%d/%m"),
            "event": label, "key": e.event_type, "category": category, "source": e.source,
            "raw": round(raw, 2), "mult": float(mult), "final": final,
            "detail": _log_detail(e, label, mult, final),
        })

    board = {r["login"]: r for r in standings(pool, include_today=True)}
    b = board.get(user.login, {})
    top_event = max(counts, key=counts.get) if counts else "—"
    return JsonResponse({
        "user": {"login": user.login, "name": user.display_name,
                 "faction": faction, "is_patient_zero": bool(inf and inf.is_patient_zero),
                 "total": round(b.get("total", gains + losses)), "rank": b.get("rank")},
        "stats": {
            "gains": round(gains), "losses": round(losses),
            "events": len(events), "days_active": len(days),
            "best": best, "worst": worst, "top_event": top_event,
        },
        "logs": logs,
    })


# ─────────────────────────── 2. Logs ───────────────────────────
@staff_required
def api_logs(request):
    pool = _pool()
    if not pool:
        return JsonResponse({"logs": [], "users": [], "events": []})
    qs = EventLog.objects.filter(pool=pool, is_voided=False).select_related("user")
    if request.GET.get("user"):
        qs = qs.filter(user__login=request.GET["user"])
    if request.GET.get("event"):
        qs = qs.filter(event_type=request.GET["event"])
    logs = [{
        "at": e.occurred_at.strftime("%d/%m %H:%M"), "login": e.user.login,
        "event": e.event_type, "source": e.source,
        "points": float(e.raw_points), "day": e.event_date.strftime("%d/%m"),
        # raison d'un ajustement manuel (+ auteur) → visible dans l'onglet Logs
        "reason": (e.raw_payload or {}).get("reason", ""),
        "by": (e.raw_payload or {}).get("by", ""),
    } for e in qs.order_by("-occurred_at")[:400]]
    return JsonResponse({
        "logs": logs,
        "users": list(AppUser.objects.filter(pool=pool).order_by("login")
                      .values_list("login", flat=True)),
        "events": list(EventLog.objects.filter(pool=pool)
                       .values_list("event_type", flat=True).distinct().order_by("event_type")),
    })


# ─────────────────────── 3. Options de Points ───────────────────────
@staff_required
@require_http_methods(["GET", "POST"])
def api_points(request):
    from .rules_config import RULE_FIELDS
    if request.method == "GET":
        return JsonResponse({"rules": _rule_cards()})

    data = _json(request)
    rule = Rule.objects.filter(id=data.get("id"), is_active=True).first()
    if not rule:
        return HttpResponseBadRequest("Règle introuvable.")

    # 1) range du multiplicateur journalier (porté par la règle)
    try:
        rule.mult_min = _dec(data.get("mult_min", rule.mult_min))
        rule.mult_max = _dec(data.get("mult_max", rule.mult_max))
    except (InvalidOperation, TypeError):
        return HttpResponseBadRequest("Range de multiplicateur invalide.")
    rule.save(update_fields=["mult_min", "mult_max"])

    # 2) points de base : on écrit UNIQUEMENT les champs déclarés au schéma, dans
    #    une nouvelle RuleVersion, PUIS on réapplique aux events passés + on
    #    recalcule le classement (le hasard déjà tiré est préservé).
    specs = {s["name"]: s for s in RULE_FIELDS.get(rule.key, [])}
    incoming = data.get("fields") or {}
    recomputed = {"updated": 0, "days": 0}
    if incoming:
        cur = rule.current_version
        params = dict(cur.params) if cur else {}
        try:
            for name, val in incoming.items():
                spec = specs.get(name)
                if not spec:
                    continue  # champ inconnu → ignoré (sécurité)
                if spec["kind"] == "map":
                    params[name] = {str(k): float(v) for k, v in (val or {}).items()}
                elif spec["kind"] == "time":
                    params[name] = str(val)
                else:
                    params[name] = float(val)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Valeur de paramètre invalide.")
        new_rule_version(rule, params, user=request.user)
        recomputed = reapply_rule_points(rule)  # tous les logs, toutes piscines
    return JsonResponse({"ok": True, **recomputed})


# ─────────────────────── 4. Gestion par Jour ───────────────────────
@staff_required
@require_http_methods(["GET", "POST"])
def api_days(request):
    pool = _pool()
    if not pool:
        return JsonResponse({"days": []})

    if request.method == "GET":
        days, d = [], pool.starts_on
        while d <= pool.ends_on:
            days.append(str(d))
            d += timedelta(days=1)
        out = {"days": days}
        if request.GET.get("date"):
            try:
                day = date.fromisoformat(request.GET["date"])
            except ValueError:
                return HttpResponseBadRequest("Date invalide.")
            mults = day_multipliers(pool, day)
            out["date"] = request.GET["date"]
            out["items"] = [{**c, "multiplier": float(mults.get(c["id"], 1))}
                            for c in _rule_cards()]
        return JsonResponse(out)

    data = _json(request)
    try:
        day = date.fromisoformat(data.get("date", ""))
    except ValueError:
        return HttpResponseBadRequest("Date invalide (AAAA-MM-JJ).")

    if data.get("action") == "randomize":
        randomize_day_multipliers(pool, day, rule_ids=data.get("rule_ids"), reseed=True)
    else:
        rule = Rule.objects.filter(id=data.get("rule_id")).first()
        if not rule:
            return HttpResponseBadRequest("Règle introuvable.")
        try:
            value = _dec(data.get("multiplier"))
        except (InvalidOperation, TypeError):
            return HttpResponseBadRequest("Multiplicateur invalide.")
        DailyEventMultiplier.objects.update_or_create(
            pool=pool, day=day, rule=rule, defaults={"multiplier": value})

    recompute_from(pool, day)  # applique aux scores à partir de ce jour
    return JsonResponse({"ok": True})


@staff_required
@require_http_methods(["POST"])
def api_randomize_all(request):
    """Randomise multiplicateurs + places Bénites/Maudites pour TOUS les jours de
    la piscine active, puis recalcule les scores de bout en bout."""
    pool = _pool()
    if not pool:
        return HttpResponseBadRequest("Aucune piscine active.")
    day, days = pool.starts_on, 0
    while day <= pool.ends_on:
        randomize_day_multipliers(pool, day, reseed=True)
        randomize_daily_hosts(pool, day, reseed=True)
        day += timedelta(days=1)
        days += 1
    recompute_from(pool, pool.starts_on)
    return JsonResponse({"ok": True, "days": days})


# ─────────────────────── 5. Places & Hosts (5 Bénites + 5 Maudites / jour) ───────────────────────
@staff_required
@require_http_methods(["GET", "POST"])
def api_hosts(request):
    pool = _pool()
    if not pool:
        return JsonResponse({"days": [], "shiny": [], "cursed": []})

    if request.method == "POST":
        from .derived import assign_daily_designations
        data = _json(request)
        try:
            day = date.fromisoformat(data.get("date", ""))
        except ValueError:
            return HttpResponseBadRequest("Date invalide.")
        randomize_daily_hosts(pool, day, reseed=True)
        assign_daily_designations(pool, day, reseed=True)  # re-tire aussi les élus du jour
        return JsonResponse({"ok": True})

    days, d = [], pool.starts_on
    while d <= pool.ends_on:
        days.append(str(d))
        d += timedelta(days=1)
    out = {"days": days, "candidates": Workstation.objects.filter(pool=pool).count()}
    date_str = request.GET.get("date") or (days[-1] if days else None)
    if date_str:
        day = date.fromisoformat(date_str)
        dh = list(DailyHost.objects.filter(pool=pool, day=day))
        out["date"] = date_str
        out["shiny"] = sorted(h.hostname for h in dh if h.kind == "shiny")
        out["cursed"] = sorted(h.hostname for h in dh if h.kind == "cursed")
        # Étudiants Maudits/Bénis DU JOUR (tirage quotidien)
        desigs = DailyDesignation.objects.filter(pool=pool, day=day).select_related("user")
        out["blessed"] = sorted(d.user.login for d in desigs if d.status == "blessed")
        out["cursed_users"] = sorted(d.user.login for d in desigs if d.status == "cursed")
    return JsonResponse(out)


# ─────────────────────── 6. Synchronisation API ───────────────────────
def _run_payload(run):
    """Sérialise un SyncRun pour le suivi live du panel."""
    if not run:
        return None
    return {
        "id": run.id, "status": run.status, "progress": run.progress,
        "pool": run.pool.name if run.pool else None,
        "date_from": str(run.date_from) if run.date_from else None,
        "date_to": str(run.date_to) if run.date_to else None,
        "days_done": run.days_done, "days_total": run.days_total,
        "current_day": str(run.current_day) if run.current_day else None,
        "events": run.events_ingested, "log": run.log, "error": run.error,
        "started_at": run.started_at.strftime("%d/%m %H:%M:%S") if run.started_at else None,
        "finished_at": run.finished_at.strftime("%d/%m %H:%M:%S") if run.finished_at else None,
    }


@staff_required
@require_http_methods(["GET", "POST"])
def api_sync(request):
    """GET : dernier run + historique. POST : lance un nouveau run (si aucun en cours)."""
    if request.method == "GET":
        runs = SyncRun.objects.all()[:8]
        return JsonResponse({
            "current": _run_payload(runs[0] if runs else None),
            "history": [{
                "id": r.id, "status": r.status, "pool": r.pool.name if r.pool else None,
                "events": r.events_ingested, "progress": r.progress,
                "at": r.created_at.strftime("%d/%m %H:%M"),
            } for r in runs],
            "running": SyncRun.objects.filter(
                status__in=[SyncRun.Status.PENDING, SyncRun.Status.RUNNING]).exists(),
        })

    # POST : refuse un lancement concurrent
    if SyncRun.objects.filter(status__in=[SyncRun.Status.PENDING,
                                          SyncRun.Status.RUNNING]).exists():
        return HttpResponseBadRequest("Une synchronisation est déjà en cours.")

    data = _json(request)
    d_from = d_to = None
    try:
        if data.get("date_from"):
            d_from = date.fromisoformat(data["date_from"])
        if data.get("date_to"):
            d_to = date.fromisoformat(data["date_to"])
    except ValueError:
        return HttpResponseBadRequest("Date invalide (AAAA-MM-JJ).")
    if d_from and d_to and d_to < d_from:
        return HttpResponseBadRequest("La date de fin est avant la date de début.")

    run = SyncRun.objects.create(
        pool=_pool(), date_from=d_from, date_to=d_to,
        created_by=request.user if request.user.is_authenticated else None,
    )
    from .tasks import run_sync
    run_sync.delay(run.id)
    return JsonResponse({"ok": True, "id": run.id})


@staff_required
def api_sync_status(request):
    """Statut d'un run précis (?id=) ou du dernier — pour le polling live."""
    run_id = request.GET.get("id")
    run = (SyncRun.objects.filter(id=run_id).first() if run_id
           else SyncRun.objects.first())
    return JsonResponse({"run": _run_payload(run)})


@staff_required
@require_http_methods(["POST"])
def api_sync_cancel(request):
    """Annule la synchronisation en cours (arrêt coopératif ; débloque aussi un
    run 'pending' resté coincé faute de worker)."""
    run = SyncRun.objects.filter(
        status__in=[SyncRun.Status.PENDING, SyncRun.Status.RUNNING]).first()
    if not run:
        return HttpResponseBadRequest("Aucune synchronisation en cours.")
    # On clôture immédiatement pour débloquer l'UI, même si le worker est mort
    # (run orphelin). Un worker encore vivant verra cancel_requested et s'arrêtera
    # proprement entre deux jours (les events déjà ingérés sont conservés).
    run.cancel_requested = True
    run.status = SyncRun.Status.CANCELLED
    run.finished_at = timezone.now()
    run.append_log("Annulation demandée — synchronisation arrêtée.")
    run.save(update_fields=["cancel_requested", "status", "finished_at", "log"])
    return JsonResponse({"ok": True, "status": run.status})


@staff_required
@require_http_methods(["GET", "POST"])
def api_autosync(request):
    """
    Interrupteur du polling automatique (tâche Beat `poll-42`, toutes les 10 min).
    GET : état + prochain run estimé. POST {enabled} : active/désactive.
    poll_42 ne cible QUE la piscine active, et seulement pendant sa période.
    """
    from django.utils import timezone as tz
    from django_celery_beat.models import PeriodicTask

    pt = PeriodicTask.objects.filter(name="poll-42").first()
    if request.method == "POST":
        if not pt:
            return HttpResponseBadRequest("Tâche planifiée introuvable (Beat non initialisé).")
        pt.enabled = bool(_json(request).get("enabled"))
        pt.save(update_fields=["enabled"])  # Beat recharge via signal PeriodicTasks.changed
        return JsonResponse({"ok": True, "enabled": pt.enabled})

    if not pt:
        return JsonResponse({"exists": False})
    next_in = None
    try:
        next_in = max(0, int(pt.schedule.remaining_estimate(tz.localtime()).total_seconds()))
    except Exception:  # noqa: BLE001
        next_in = None
    pool = _pool()
    return JsonResponse({
        "exists": True, "enabled": pt.enabled,
        "interval_label": "toutes les 10 min",
        "last_run": tz.localtime(pt.last_run_at).strftime("%d/%m %H:%M:%S") if pt.last_run_at else None,
        "next_in": next_in, "total_runs": pt.total_run_count,
        "pool": pool.name if pool else None,
    })


# ─────────────────────── 7. Peste & Choléra ───────────────────────
@staff_required
@require_http_methods(["GET", "POST"])
def api_plague(request):
    """
    Onglet Peste & Choléra : stats temps réel + unique réglage = points par
    personne (versés à la coalition la plus nombreuse, 1 jour avant l'exam final).
    """
    from .chaos import get_config, plague_endgame, plague_stats
    pool = _pool()
    if not pool:
        return JsonResponse({"stats": None, "points_per_person": None})

    if request.method == "GET":
        cfg = get_config(pool)
        return JsonResponse({"stats": plague_stats(pool),
                             "points_per_person": float(cfg.plague_payout),
                             # dedavid board : proba stockée en [0,1], exposée en %.
                             "secret_board_pct": float(cfg.secret_board_chance) * 100})

    cfg = get_config(pool)
    data = _json(request)

    # Réglage du dedavid board (%) : indépendant du montant Peste & Choléra.
    if data.get("secret_board_pct") is not None:
        try:
            pct = float(data["secret_board_pct"])
        except (TypeError, ValueError):
            return HttpResponseBadRequest("Pourcentage invalide.")
        pct = max(0.0, min(100.0, pct))
        cfg.secret_board_chance = _dec(pct / 100)
        cfg.save(update_fields=["secret_board_chance"])
        return JsonResponse({"ok": True, "secret_board_pct": float(cfg.secret_board_chance) * 100})

    # POST : met à jour les points par personne et rejoue le boost (idempotent).
    try:
        cfg.plague_payout = _dec(data.get("points_per_person"))
    except (InvalidOperation, TypeError):
        return HttpResponseBadRequest("Nombre de points invalide.")
    cfg.save(update_fields=["plague_payout"])
    res = plague_endgame(pool)  # ré-applique le boost avec le nouveau montant
    return JsonResponse({"ok": True, "points_per_person": float(cfg.plague_payout), "endgame": res})
