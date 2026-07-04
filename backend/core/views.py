"""
Vues HTTP du Chaos Leaderboard 42.

- healthz : sonde de vie.
- leaderboard_preview : classement curl-able (texte brut ANSI).
"""
from django.http import HttpResponse, JsonResponse
from django.utils import timezone

from .models import CurlTracking, Pool
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
