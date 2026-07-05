"""
Vues HTTP du Chaos Leaderboard 42.

- healthz : sonde de vie.
- leaderboard_preview : classement curl-able (texte brut ANSI).
"""
import random

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

from .models import CurlTracking, Pool
from .services import standings


def healthz(request):
    return JsonResponse({"status": "ok"})


# Clients « terminal » reconnus à la racine : eux reçoivent le leaderboard texte,
# les navigateurs classiques continuent vers le site HTML (/panel/).
_CLI_AGENTS = ("curl", "wget", "httpie", "python-requests", "fetch")


def root(request):
    """
    `curl monsite.com` → rendu texte ANSI du leaderboard, directement.
    Navigateur (User-Agent Mozilla/…) → site web classique. Un User-Agent vide
    est traité comme un client CLI (curl -A "" et consorts).
    """
    ua = request.META.get("HTTP_USER_AGENT", "").lower()
    if not ua or any(tok in ua for tok in _CLI_AGENTS):
        return leaderboard_preview(request)
    return redirect("/panel/")


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

# Mise en page multi-colonnes : TOUS les participants côte à côte, ~20 lignes
# par colonne, jusqu'à 5 colonnes (largeur bornée à ~95 car.). Au-delà de 100
# participants la grille grandit en hauteur (5 colonnes plus longues).
_PER_COL = 20
_MAX_COLS = 5


def _hyperlink(url, text, enabled=True):
    """
    Hyperlien terminal (séquence OSC 8) : le TEXTE devient cliquable vers URL.
    Format : ESC ]8;;URL ST  texte  ESC ]8;; ST   (ST = ESC \\, terminateur
    standard — le BEL faisait glitcher certains terminaux).

    OPT-IN (?links) : Terminal.app (macOS) ne supporte pas OSC 8 et son parseur
    avale même le texte du lien → pseudos invisibles. On n'émet donc AUCUNE
    séquence par défaut ; ?links l'active pour iTerm2/Kitty/WezTerm/GNOME…
    """
    if not enabled:
        return text
    esc, st = "\033", "\033\\"
    return f"{esc}]8;;{url}{st}{text}{esc}]8;;{st}"


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


def _render_board(pool, board, colored=True, links=False):
    """
    Classement curl-able, TOUS les participants en grille multi-colonnes (jusqu'à
    5 colonnes côte à côte, ~20 lignes chacune) pour rester compact en largeur
    (~80–100 colonnes). Le Top 100 tient sur un écran ; au-delà, la grille
    s'allonge verticalement (5 colonnes plus longues) mais tout le monde apparaît.

    links=True (?links) : pseudos cliquables via OSC 8 — opt-in car Terminal.app
    ne supporte pas la séquence et rend les pseudos invisibles (cf. _hyperlink).
    Les largeurs sont calculées sur le texte VISIBLE : les hyperliens OSC 8 et les
    codes couleur SGR (caractères « invisibles ») ne faussent pas l'alignement.
    """
    p = _ansi(colored)
    # 1 chance sur 100 : « Leaderboard Secret » — tous les pseudos → dedavid.
    secret = random.random() < _SECRET_CHANCE

    def login_of(r):
        return _SECRET_LOGIN if secret else r["login"]

    total = n = len(board)
    shown = board  # on affiche TOUT le classement

    # On met le PLUS de colonnes possible (≤ _MAX_COLS) pour minimiser la hauteur.
    ncols = max(1, min(_MAX_COLS, (n + _PER_COL - 1) // _PER_COL))
    rows = max(1, (n + ncols - 1) // ncols)

    rw = max(2, len(str(total)))                                    # largeur du rang
    lw = min(max((len(login_of(r)) for r in shown), default=6), 8)  # largeur login
    pw = max(4, max((len(str(round(r["total"]))) for r in shown), default=4))
    cellw = rw + 1 + lw + 1 + pw
    sep = indent = "  "
    total_w = ncols * cellw + (ncols - 1) * len(sep)

    def cell(r):
        if r is None:
            return " " * cellw
        rank = r["rank"]
        rc = {1: p["gold"], 2: p["silver"], 3: p["bronze"]}.get(rank, p["muted"])
        pts = round(r["total"])
        pcol = p["green"] if pts >= 0 else p["red"]
        # login cliquable → profil intra ; padding calculé sur le texte VISIBLE
        # (les séquences OSC 8 / couleurs ne comptent pas dans la largeur).
        name = login_of(r)[:lw]
        linked = _hyperlink(_PROFILE_URL.format(login=login_of(r)), name, enabled=links)
        pad = " " * (lw - len(name))
        return (f"{rc}{rank:>{rw}}{p['reset']} {p['cream']}{linked}{p['reset']}{pad} "
                f"{pcol}{pts:>{pw}}{p['reset']}")

    def divider():
        return f"{indent}{p['lav']}☽ {'─' * max(0, total_w - 4)} ☾{p['reset']}"

    scope = f"{total} participants"
    lines = [
        "",
        f"{indent}{p['title']}{p['bold']}✦ 42 - Leaderboard ✦{p['reset']}"
        f"   {p['muted']}{scope}{p['reset']}",
        f"{indent}{p['muted']}{pool.name} · {pool.starts_on:%d/%m} → {pool.ends_on:%d/%m/%Y}{p['reset']}",
        divider(),
    ]

    if not shown:
        lines.append(f"{indent}{p['dim']}(le grimoire est encore vierge…){p['reset']}")
    else:
        # Remplissage colonne par colonne : rangs 1..rows en col 1, etc.
        for i in range(rows):
            parts = [cell(shown[c * rows + i] if c * rows + i < n else None)
                     for c in range(ncols)]
            lines.append(indent + sep.join(parts).rstrip())

    lines.append(divider())
    hint = "" if links else f"   {p['dim']}(?links : pseudos cliquables — iTerm2/Kitty/WezTerm){p['reset']}"
    lines += [
        f"{indent}{p['muted']}crafted by {p['lav']}{p['bold']}Dedavid{p['reset']}"
        f"{p['muted']} & {p['lav']}{p['bold']}Rydelepi{p['reset']}{hint}",
        "",
    ]
    return "\n".join(lines) + "\n"


def leaderboard_preview(request):
    CurlTracking.objects.create(
        ip=_client_ip(request),
        endpoint=request.path or "/leaderboard",
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:255],
        day=timezone.localdate(),
    )

    pool = Pool.objects.filter(is_active=True).order_by("-starts_on").first()
    if not pool:
        return HttpResponse("Aucune Piscine active.\n", content_type="text/plain")

    # Score réel = cumul figé (snapshots) + jour courant en live — TOUS les participants
    board = standings(pool, include_today=True)
    colored = "plain" not in request.GET  # curl ...?plain pour couper les couleurs
    links = colored and "links" in request.GET  # ?links : pseudos cliquables (OSC 8)
    body = _render_board(pool, board, colored=colored, links=links)
    return HttpResponse(body, content_type="text/plain; charset=utf-8")
