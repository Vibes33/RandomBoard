"""
Vues HTTP du Chaos Leaderboard 42.

- healthz : sonde de vie.
- leaderboard_preview : classement curl-able (texte brut ANSI).
"""
import random

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

# Profil intra 42 de chaque étudiant (login cliquable dans le terminal).
_PROFILE_URL = "https://profile.intra.42.fr/users/{login}"

# Easter egg ultra-rare : 1 appel curl sur 100, tous les pseudos deviennent
# « dedavid » (points/rangs inchangés) et pointent vers son profil.
_SECRET_LOGIN = "dedavid"
_SECRET_CHANCE = 0.01


def _hyperlink(url, text, enabled=True):
    """
    Hyperlien terminal (séquence OSC 8) : le TEXTE devient cliquable vers URL.
    Format : ESC ]8;;URL BEL  texte  ESC ]8;; BEL.
    enabled=False (mode --plain) → texte brut, aucune séquence.
    """
    if not enabled:
        return text
    esc, bel = "\033", "\007"
    return f"{esc}]8;;{url}{bel}{text}{esc}]8;;{bel}"


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
    # 1 chance sur 100 : « Leaderboard Secret » — tous les pseudos → dedavid.
    secret = random.random() < _SECRET_CHANCE

    def login_of(r):
        return _SECRET_LOGIN if secret else r["login"]

    COL = 50
    lw = min(max((len(login_of(r)) for r in board), default=6), 11)
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
        # login cliquable → profil intra ; padding calculé sur le texte VISIBLE
        # (les séquences OSC 8 ne comptent pas dans la largeur de colonne).
        name = login_of(r)[:lw]
        linked = _hyperlink(_PROFILE_URL.format(login=login_of(r)), name, enabled=colored)
        pad = " " * (lw - len(name))
        return (f"{rc}{rank:>4}{p['reset']} {p['cream']}{linked}{p['reset']}{pad} "
                f"{pcol}{pts:>{pw}}{p['reset']}")

    def divider():
        return f"   {p['lav']}☽ {'─' * (total_w - 4)} ☾{p['reset']}"

    lines = [
        "",
        f"   {p['title']}{p['bold']}✦ 42 - Leaderboard ✦{p['reset']}"
        f"   {p['muted']}{n} participants{p['reset']}",
        f"   {p['muted']}{pool.name} · {pool.starts_on:%d/%m} → {pool.ends_on:%d/%m/%Y}{p['reset']}",
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
