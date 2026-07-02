"""
Vues HTTP du Chaos Leaderboard 42.

- healthz : sonde de vie.
- leaderboard_preview : aperçu curl-able (texte brut).
- dashboard : back-office visuel staff-only (KPI + graphes, stats live).
  La VRAIE logique de score (snapshots) arrive à l'étape 3 ; ici on présente
  un aperçu honnête basé sur les points BRUTS (hors coefficient journalier).
"""
import json
from datetime import timedelta

from django.db.models import Count, Max, Min, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from .auth import staff_required
from .models import AppUser, CurlTracking, EventLog, Pool, Rule
from .services import standings


def healthz(request):
    return JsonResponse({"status": "ok"})


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


# ─── Rendu terminal du leaderboard (ANSI) ───
_ESC = "\033["


def _ansi(colored):
    """Fabrique de codes couleur ANSI (thème Witch Hat Atelier) ; vide si --plain."""
    def c(code):
        return f"{_ESC}38;5;{code}m" if colored else ""
    return {
        "reset": f"{_ESC}0m" if colored else "", "bold": f"{_ESC}1m" if colored else "",
        "dim": f"{_ESC}2m" if colored else "",
        "indigo": c(99), "lav": c(147), "title": c(189), "cream": c(230),
        "gold": c(222), "silver": c(252), "bronze": c(180), "muted": c(103),
        "green": c(150), "red": c(210),
    }


def _render_board(pool, board, colored=True):
    """
    Classement complet (Witch Hat Atelier), TOUS les participants en colonnes de 50
    (donc jusqu'à 3 colonnes), avec les points à côté du pseudo.
    """
    p = _ansi(colored)
    COL = 50
    lw = min(max((len(r["login"]) for r in board), default=6), 11)
    pw = max((len(str(round(r["total"]))) for r in board), default=5)
    pw = max(pw, 5)
    n = len(board)
    ncols = max(1, (n + COL - 1) // COL)
    cellw = 4 + 1 + lw + 1 + pw  # rang(4) + login + points
    total_w = ncols * cellw + (ncols - 1) * 3

    def cell(r):
        if r is None:
            return " " * cellw
        rank = r["rank"]
        rc = {1: p["gold"], 2: p["silver"], 3: p["bronze"]}.get(rank, p["muted"])
        pts = round(r["total"])
        pcol = p["green"] if pts >= 0 else p["red"]
        return (f"{rc}{rank:>4}{p['reset']} {p['cream']}{r['login'][:lw]:<{lw}}{p['reset']} "
                f"{pcol}{pts:>{pw}}{p['reset']}")

    def divider():
        return f"   {p['lav']}☽ {'─' * (total_w - 4)} ☾{p['reset']}"

    lines = [
        "",
        f"   {p['title']}{p['bold']}✦ 42 - Leaderboard ✦{p['reset']}"
        f"   {p['muted']}{n} participants{p['reset']}",
        f"   {p['muted']}Piscine 2026 - promo Juillet{p['reset']}",
        divider(),
        "",
    ]

    if not board:
        lines.append(f"     {p['dim']}(le grimoire est encore vierge…){p['reset']}")
    else:
        rows = COL if ncols > 1 else n
        for i in range(rows):
            parts = [cell(board[c * COL + i] if c * COL + i < n else None) for c in range(ncols)]
            lines.append("   " + "   ".join(parts).rstrip())

    lines += [
        "",
        divider(),
        f"   {p['muted']}crafted by {p['lav']}{p['bold']}Dedavid{p['reset']}"
        f"{p['muted']} & {p['lav']}{p['bold']}Rydelepi{p['reset']}",
        f"   {p['indigo']}⋆ ˚ ｡ ✦ ⋆ ˚ ｡ ✦ ⋆ ˚ ｡ ✦{p['reset']}",
        "",
    ]
    return "\n".join(lines) + "\n"


def leaderboard_preview(request):
    CurlTracking.objects.create(
        ip=_client_ip(request),
        endpoint="/leaderboard",
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:255],
        day=timezone.localdate(),
    )

    pool = Pool.objects.filter(is_active=True).order_by("-starts_on").first()
    if not pool:
        return HttpResponse("Aucune Piscine active.\n", content_type="text/plain")

    # Score réel = cumul figé (snapshots) + jour courant en live — TOUS les participants
    board = standings(pool, include_today=True)
    colored = "plain" not in request.GET  # curl ...?plain pour couper les couleurs
    body = _render_board(pool, board, colored=colored)
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


@staff_required
def dashboard(request):
    """Back-office visuel : KPI + graphes, alimentés par la base en temps réel."""
    today = timezone.localdate()
    pool = Pool.objects.filter(is_active=True).order_by("-starts_on").first()

    events = EventLog.objects.filter(is_voided=False)
    if pool:
        events = events.filter(pool=pool)

    # ─── KPI (relatifs à la piscine, pas à "aujourd'hui") ───
    total_students = AppUser.objects.filter(pool=pool).count() if pool else 0
    events_total = events.count()
    raw_total = float(events.aggregate(s=Sum("raw_points"))["s"] or 0)
    active_rules = Rule.objects.filter(is_active=True).count()

    bounds = events.aggregate(mn=Min("event_date"), mx=Max("event_date"))
    first_day, last_day = bounds["mn"], bounds["mx"]
    days_active = events.values("event_date").distinct().count()
    active_users = events.values("user").distinct().count()
    active_pct = round(100 * active_users / total_students) if total_students else 0

    # ─── Classement (top 8 + lanterne rouge) ───
    board = standings(pool, include_today=True) if pool else []
    leaders = [{"login": r["login"], "pts": r["total"]} for r in board[:8]]
    losers = [{"login": r["login"], "pts": r["total"]} for r in board[-3:][::-1]]

    # ─── Events par type ───
    by_type = [
        {"label": r["event_type"], "value": r["c"]}
        for r in events.values("event_type").annotate(c=Count("id")).order_by("-c")[:7]
    ]

    # ─── Points par catégorie de règle (gains vs pertes) ───
    rule_cat = {rl.key: rl.category for rl in Rule.objects.all()}
    cat_pts = {"gain": 0.0, "loss": 0.0, "event": 0.0}
    for r in events.values("event_type").annotate(s=Sum("raw_points")):
        cat = rule_cat.get(r["event_type"])
        pts = float(r["s"] or 0)
        if cat in cat_pts:
            cat_pts[cat] += pts
        elif pts >= 0:
            cat_pts["gain"] += pts
        else:
            cat_pts["loss"] += pts
    categories = [
        {"label": "Gains", "value": round(cat_pts["gain"]), "color": "#8fe03a"},
        {"label": "Pertes", "value": round(abs(cat_pts["loss"])), "color": "#ff5a5a"},
        {"label": "Events", "value": round(abs(cat_pts["event"])), "color": "#4aa3ff"},
    ]

    # ─── Séries temporelles sur la PLAGE de la piscine ───
    ev_map = {r["event_date"]: r["c"] for r in events.values("event_date").annotate(c=Count("id"))}
    pt_map = {r["event_date"]: float(r["s"] or 0)
              for r in events.values("event_date").annotate(s=Sum("raw_points"))}
    events_per_day, points_per_day = [], []
    if first_day and last_day:
        span = min((last_day - first_day).days + 1, 40)  # borne de sécurité
        for i in range(span):
            d = first_day + timedelta(days=i)
            events_per_day.append({"label": d.strftime("%d/%m"), "value": ev_map.get(d, 0)})
            points_per_day.append({"label": d.strftime("%d/%m"), "value": round(pt_map.get(d, 0))})

    bundle = {
        "leaders": leaders,
        "losers": losers,
        "by_type": by_type,
        "categories": categories,
        "events_per_day": events_per_day,
        "points_per_day": points_per_day,
        "gauge": {"label": "Étudiants actifs", "value": active_pct,
                  "sub": f"{active_users}/{total_students}"},
    }

    period = (f"{first_day:%d/%m} → {last_day:%d/%m}" if first_day else "—")
    context = {
        "pool": pool,
        "period": period,
        "kpis": [
            {"label": "Étudiants", "value": total_students, "unit": "", "accent": "blue"},
            {"label": "Events", "value": events_total, "unit": "", "accent": "green"},
            {"label": "Points bruts cumulés", "value": round(raw_total), "unit": "pts", "accent": "green"},
            {"label": "Jours actifs", "value": days_active, "unit": "j", "accent": "orange"},
            {"label": "Règles actives", "value": active_rules, "unit": "", "accent": "blue"},
        ],
        "events_total": events_total,
        "raw_total": round(raw_total),
        "bundle_json": json.dumps(bundle),
    }
    return render(request, "core/dashboard.html", context)
