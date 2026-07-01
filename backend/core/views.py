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

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone

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
    }


def _render_board(pool, board, colored=True):
    """Classement façon grimoire (Witch Hat Atelier) : rang + pseudo, sans score."""
    p = _ansi(colored)

    def divider():
        return f"   {p['lav']}☽ {'─' * 32} ☾{p['reset']}"

    lines = [
        "",
        f"   {p['title']}{p['bold']}✦ 42 - Leaderboard ✦{p['reset']}",
        f"   {p['muted']}Piscine 2026 - promo Juillet{p['reset']}",
        divider(),
        "",
    ]

    if not board:
        lines.append(f"     {p['dim']}(le grimoire est encore vierge…){p['reset']}")
    for r in board:
        rank = r["rank"]
        star = {1: "✦", 2: "✧", 3: "✧"}.get(rank, "⋆")
        rc = {1: p["gold"], 2: p["silver"], 3: p["bronze"]}.get(rank, p["muted"])
        lines.append(
            f"     {rc}{star} {rank:>2}{p['reset']}   {p['cream']}{r['login']}{p['reset']}"
        )

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

    # Score réel = cumul figé (snapshots) + jour courant en live
    board = standings(pool, include_today=True, limit=50)
    colored = "plain" not in request.GET  # curl ...?plain pour couper les couleurs
    body = _render_board(pool, board, colored=colored)
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


@staff_member_required
def dashboard(request):
    """Back-office visuel : KPI + graphes, alimentés par la base en temps réel."""
    today = timezone.localdate()
    pool = Pool.objects.filter(is_active=True).order_by("-starts_on").first()

    events = EventLog.objects.filter(is_voided=False)
    if pool:
        events = events.filter(pool=pool)

    # ─── KPI ───
    total_students = AppUser.objects.filter(pool=pool).count() if pool else 0
    events_today = events.filter(event_date=today).count()
    events_total = events.count()
    raw_today = float(events.filter(event_date=today).aggregate(s=Sum("raw_points"))["s"] or 0)
    raw_total = float(events.aggregate(s=Sum("raw_points"))["s"] or 0)
    curls_today = CurlTracking.objects.filter(day=today).count()
    active_rules = Rule.objects.filter(is_active=True).count()

    active_today = events.filter(event_date=today).values("user").distinct().count()
    active_pct = round(100 * active_today / total_students) if total_students else 0

    # ─── Classement réel (cumul figé + jour courant) ───
    leaders = [
        {"login": r["login"], "pts": r["total"]}
        for r in standings(pool, include_today=True, limit=8)
    ] if pool else []

    # ─── Events par type ───
    by_type = list(
        events.values("event_type").annotate(c=Count("id")).order_by("-c")[:6]
    )
    by_type = [{"label": r["event_type"], "value": r["c"]} for r in by_type]

    # ─── Règles par catégorie ───
    cat_map = {r["category"]: r["c"] for r in Rule.objects.values("category").annotate(c=Count("id"))}
    categories = [
        {"label": "Gains", "value": cat_map.get("gain", 0), "color": "#8fe03a"},
        {"label": "Pertes", "value": cat_map.get("loss", 0), "color": "#ff5a5a"},
        {"label": "Events", "value": cat_map.get("event", 0), "color": "#4aa3ff"},
    ]

    # ─── Séries temporelles (14 derniers jours) ───
    start = today - timedelta(days=13)

    def daily_series(qs, date_field):
        m = {r[date_field]: r["c"] for r in qs.values(date_field).annotate(c=Count("id"))}
        return [
            {"label": (start + timedelta(days=i)).strftime("%d/%m"),
             "value": m.get(start + timedelta(days=i), 0)}
            for i in range(14)
        ]

    events_per_day = daily_series(events.filter(event_date__gte=start), "event_date")
    curls_per_day = daily_series(CurlTracking.objects.filter(day__gte=start), "day")

    bundle = {
        "leaders": leaders,
        "by_type": by_type,
        "categories": categories,
        "events_per_day": events_per_day,
        "curls_per_day": curls_per_day,
        "gauge": {"label": "Étudiants actifs", "value": active_pct,
                  "sub": f"{active_today}/{total_students}"},
    }

    context = {
        "pool": pool,
        "kpis": [
            {"label": "Étudiants", "value": total_students, "unit": "", "accent": "blue"},
            {"label": "Events aujourd'hui", "value": events_today, "unit": "", "accent": "green"},
            {"label": "Points bruts (jour)", "value": round(raw_today), "unit": "pts", "accent": "green"},
            {"label": "curl /leaderboard", "value": curls_today, "unit": "appels", "accent": "orange"},
            {"label": "Règles actives", "value": active_rules, "unit": "", "accent": "blue"},
        ],
        "events_total": events_total,
        "raw_total": round(raw_total),
        "bundle_json": json.dumps(bundle),
    }
    return render(request, "core/dashboard.html", context)
