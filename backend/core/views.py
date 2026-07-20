"""
Vues HTTP du Chaos Leaderboard 42.

- healthz : sonde de vie.
- leaderboard_preview : classement curl-able (texte brut ANSI).
"""
import random

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from .models import CurlTracking, Pool
from .services import standings


def healthz(request):
    return JsonResponse({"status": "ok"})


# Clients « terminal » reconnus à la racine : eux reçoivent le leaderboard texte,
# les navigateurs classiques continuent vers le site HTML (/panel/).
_CLI_AGENTS = ("curl", "wget", "httpie", "python-requests", "fetch")


def root(request, login=None):
    """
    `curl monsite.com` → rendu texte ANSI du leaderboard, directement.
    `curl monsite.com/aandreo` → même leaderboard, pseudo `aandreo` en surbrillance.
    Navigateur (User-Agent Mozilla/…) → page HTML du leaderboard (même contenu).
    Le panel admin n'est PAS servi ici : accès explicite via /panel uniquement.
    Un User-Agent vide est traité comme un client CLI (curl -A "" et consorts).
    """
    ua = request.META.get("HTTP_USER_AGENT", "").lower()
    if not ua or any(tok in ua for tok in _CLI_AGENTS):
        return leaderboard_preview(request, me=login)
    return leaderboard_html(request, me=login)


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


# ─── Rendu terminal du leaderboard (ANSI) ───
_ESC = "\033["

# Profil intra 42 de chaque étudiant (login cliquable dans le terminal).
_PROFILE_URL = "https://profile.intra.42.fr/users/{login}"

# Easter egg : à chaque appel curl, une (petite) chance que tous les pseudos
# deviennent « dedavid » (points/rangs inchangés) et pointent vers son profil.
# La probabilité est réglable au panel (PoolConfig.secret_board_chance) ; cette
# constante n'est plus qu'un fallback si aucune config n'existe.
_SECRET_LOGIN = "dedavid"
_SECRET_CHANCE = 0.01

# Mise en page multi-colonnes : TOUS les participants côte à côte, ~20 lignes
# par colonne, jusqu'à 5 colonnes. La largeur TOTALE est bornée à 80 caractères
# (terminal standard type Ubuntu) : le nombre de colonnes s'adapte pour ne
# JAMAIS wrapper ; ?cols=N force la valeur sur les grands écrans.
_PER_COL = 20
_MAX_COLS = 5
_TARGET_WIDTH = 80

# Tips « toujours utiles » (piochés en plus d'un éventuel tip contextuel).
# RÈGLE : aucune info sur le barème — le système de points reste OPAQUE.
# On ne mentionne jamais un gain/malus, une règle ou une mécanique de score.
_GENERAL_TIPS = [
    "Le classement est un mystère… concentre-toi sur ton code.",
    "Lis les sujets jusqu'au bout avant de te lancer.",
    "Teste ton code avant de rendre : les edge cases piègent tout le monde.",
    "Hydrate-toi et dors un peu — le marathon est long.",
    "Pose des questions",
    "Commit souvent, code proprement, respecte la Norme.",
    "Fais des pauses : un cerveau reposé debug plus vite.",
    "Reste humble et curieux — c'est ça, l'esprit 42.",
]


def _pool_tips(pool, today, n=2):
    """
    Tips CONTEXTUELS selon le moment de la piscine (jour de semaine, début/fin,
    proximité du week-end/exam) + quelques généraux. Renvoie `n` tips distincts,
    en priorisant les contextuels. Choix aléatoire → varie à chaque curl.
    """
    wd = today.weekday()  # 0=lun … 4=ven, 5=sam, 6=dim
    day_num = (today - pool.starts_on).days + 1
    days_left = (pool.ends_on - today).days
    ctx = []
    if wd in (3, 4):  # jeudi / vendredi : le rush du week-end arrive
        ctx.append("Le rush du week-end approche — formez vos équipes dès maintenant !")
    if wd == 4:  # vendredi : jour d'exam en piscine
        ctx.append("N'oubliez pas de vous register à l'examen !")
    if wd in (5, 6):  # week-end : rush en cours
        ctx.append("C'est le rush ! Codez en groupe et soignez vos rendus.")
    if day_num <= 3:
        ctx.append("Début de piscine : soignez vos shells et prenez le rythme.")
    if 0 <= days_left <= 2:
        ctx.append("Dernière ligne droite — l'examen final décide de tout.")
    # les contextuels sont prioritaires (pondérés ×2) puis on complète au général
    pool_tips = ctx * 2 + _GENERAL_TIPS
    picks, seen = [], set() 
    while pool_tips and len(picks) < n:
        t = random.choice(pool_tips)
        if t not in seen:
            picks.append(t)
            seen.add(t)
        pool_tips = [x for x in pool_tips if x != t] if t in seen else pool_tips
    return picks


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


_DISEASE_EMOJI = {"peste": "🐀", "cholera": "💧"}
# Docteur (immunisé, soigne par correction) — prioritaire sur tout badge maladie.
_DOCTOR_EMOJI = "🩺"


def _render_board(pool, board, colored=True, links=True, me=None, secret_chance=None,
                  force_cols=None, diseases=None, doctors=None):
    """
    Classement curl-able, TOUS les participants en grille multi-colonnes (jusqu'à
    5 colonnes côte à côte, ~20 lignes chacune) pour rester compact en largeur
    (~80–100 colonnes). Le Top 100 tient sur un écran ; au-delà, la grille
    s'allonge verticalement (5 colonnes plus longues) mais tout le monde apparaît.

    links=True (défaut) : pseudos cliquables via OSC 8 → profil intra. Fonctionne
    nativement en SSH (c'est le terminal LOCAL qui rend le lien). Coupable via
    ?nolinks pour les rares terminaux qui ne gèrent pas la séquence.
    me : login à mettre en SURBRILLANCE (curl ?me=login) — gras + vidéo inversée.
    secret_chance : proba du dedavid board (défaut = _SECRET_CHANCE).
    Les largeurs sont calculées sur le texte VISIBLE : les hyperliens OSC 8 et les
    codes couleur SGR (caractères « invisibles ») ne faussent pas l'alignement.
    """
    p = _ansi(colored)
    me = (me or "").lower() or None
    chance = _SECRET_CHANCE if secret_chance is None else float(secret_chance)
    # « dedavid board » (proba réglable) — tous les pseudos → dedavid.
    secret = random.random() < chance
    # DA du dedavid board : palette dédiée émeraude + bleu océan (256 couleurs)
    # pour qu'il se distingue au premier coup d'œil du leaderboard normal.
    if secret and colored:
        emerald, ocean = f"{_ESC}38;5;78m", f"{_ESC}38;5;38m"
        p = {**p, "title": emerald, "cream": emerald, "gold": emerald,
             "silver": emerald, "bronze": emerald, "indigo": emerald,
             "lav": ocean, "muted": ocean, "green": ocean, "red": ocean}

    def login_of(r):
        return _SECRET_LOGIN if secret else r["login"]

    total = n = len(board)
    shown = board  # on affiche TOUT le classement
    # Rang du login mis en surbrillance (hors mode secret où tout est « dedavid »).
    my_row = None if secret else next(
        (r for r in shown if r["login"].lower() == me), None) if me else None

    # Badges Docteur 🩺 / Peste 🐀 / Choléra 💧 par user (hors mode secret). Un slot
    # de 3 cellules d'affichage est réservé pour tous (emoji large = 2 cellules + 1
    # espace, ou 3 espaces pour un sain) → l'alignement de la grille reste bon.
    doctors = doctors or set()
    show_badges = bool(diseases or doctors) and not secret

    def badge(r):
        if not show_badges:
            return ""
        # docteur d'abord : il est immunisé, il ne porte jamais de maladie
        emo = (_DOCTOR_EMOJI if r["login"] in doctors
               else _DISEASE_EMOJI.get(diseases.get(r["login"]) if diseases else None))
        return f"{emo} " if emo else "   "  # 2 (emoji) + 1 espace, sinon 3 espaces
    bw = 3 if show_badges else 0

    rw = max(2, len(str(total)))                                    # largeur du rang
    lw = min(max((len(login_of(r)) for r in shown), default=6), 8)  # largeur login
    pw = max(4, max((len(str(round(r["total"]))) for r in shown), default=4))
    cellw = rw + 1 + bw + lw + 1 + pw
    sep = indent = "  "

    # Nombre de colonnes : le PLUS possible pour minimiser la hauteur, MAIS la
    # ligne complète (indent + cellules + séparateurs) doit tenir dans un
    # terminal standard de 80 colonnes — sinon ça wrappe et tout est illisible.
    # ?cols=N (1..6) force la valeur pour les grands écrans.
    budget = _TARGET_WIDTH - len(indent)
    fit = max(1, (budget + len(sep)) // (cellw + len(sep)))
    ncols = max(1, min(_MAX_COLS, (n + _PER_COL - 1) // _PER_COL, fit))
    if force_cols:
        ncols = max(1, min(6, force_cols))
    rows = max(1, (n + ncols - 1) // ncols)
    total_w = ncols * cellw + (ncols - 1) * len(sep)

    def cell(r):
        if r is None:
            return " " * cellw
        rank = r["rank"]
        pts = round(r["total"])
        # login cliquable → profil intra ; padding calculé sur le texte VISIBLE
        # (les séquences OSC 8 / couleurs ne comptent pas dans la largeur).
        name = login_of(r)[:lw]
        linked = _hyperlink(_PROFILE_URL.format(login=login_of(r)), name, enabled=links)
        pad = " " * (lw - len(name))
        # Surbrillance du login demandé (?me=) : gras + vidéo inversée sur toute la
        # cellule, SANS reset intermédiaire (sinon l'inversion « saute »). Le lien
        # OSC 8 ne contient pas de SGR, donc l'inversion tient dessous.
        bdg = badge(r)
        if colored and me and login_of(r).lower() == me:
            hl = f"{_ESC}1m{_ESC}7m"
            return f"{hl}{rank:>{rw}} {bdg}{linked}{pad} {pts:>{pw}}{p['reset']}"
        rc = {1: p["gold"], 2: p["silver"], 3: p["bronze"]}.get(rank, p["muted"])
        pcol = p["green"] if pts >= 0 else p["red"]
        return (f"{rc}{rank:>{rw}}{p['reset']} {bdg}{p['cream']}{linked}{p['reset']}{pad} "
                f"{pcol}{pts:>{pw}}{p['reset']}")

    def divider():
        return f"{indent}{p['lav']}☽ {'─' * max(0, total_w - 4)} ☾{p['reset']}"

    scope = f"{total} participants"
    title_txt = "✦ dedavid board ✦" if secret else "✦ 42 - Leaderboard ✦"
    lines = [
        "",
        f"{indent}{p['title']}{p['bold']}{title_txt}{p['reset']}"
        f"   {p['muted']}{scope}{p['reset']}",
        f"{indent}{p['muted']}{pool.name} · {pool.starts_on:%d/%m} → {pool.ends_on:%d/%m/%Y}{p['reset']}",
    ]
    # (aucune légende des badges : la mécanique reste OPAQUE — les emoji
    # 🩺/🐀/💧 s'affichent sans jamais être expliqués au joueur)
    # Note « c'est toi » quand ?me= est fourni (rang + points, ou introuvable).
    if me and not secret:
        if my_row:
            lines.append(f"{indent}{p['gold']}{p['bold']}➤ toi : #{my_row['rank']} "
                         f"· {round(my_row['total'])} pts{p['reset']}")
        else:
            lines.append(f"{indent}{p['dim']}(« {me} » introuvable au classement){p['reset']}")
    lines.append(divider())

    if not shown:
        lines.append(f"{indent}{p['dim']}(le grimoire est encore vierge…){p['reset']}")
    else:
        # Remplissage colonne par colonne : rangs 1..rows en col 1, etc.
        for i in range(rows):
            parts = [cell(shown[c * rows + i] if c * rows + i < n else None)
                     for c in range(ncols)]
            lines.append(indent + sep.join(parts).rstrip())

    lines.append(divider())
    lines.append(
        f"{indent}{p['muted']}crafted by {p['lav']}{p['bold']}Dedavid{p['reset']}"
        f"{p['muted']} & {p['lav']}{p['bold']}Rydelepi{p['reset']}")
    # Tips contextuels de la piscine, juste sous les créateurs.
    for tip in _pool_tips(pool, timezone.localdate()):
        lines.append(f"{indent}{p['gold']}💡 {p['muted']}{tip}{p['reset']}")
    lines.append("")
    return "\n".join(lines) + "\n"


def leaderboard_preview(request, me=None):
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
    links = "nolinks" not in request.GET  # pseudos cliquables (OSC 8) par défaut
    # login en surbrillance : /aandreo (path) prioritaire, sinon ?me=aandreo
    me = me or request.GET.get("me")
    from .chaos import get_config
    chance = float(get_config(pool).secret_board_chance)
    try:
        force_cols = int(request.GET.get("cols", "")) or None  # ?cols=5 : écran large
    except ValueError:
        force_cols = None
    body = _render_board(pool, board, colored=colored, links=links, me=me,
                         secret_chance=chance, force_cols=force_cols,
                         diseases=_disease_map(pool), doctors=_doctor_set(pool))
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


def _disease_map(pool):
    """{login: 'peste'|'cholera'} des étudiants infectés (badges du leaderboard)."""
    from .models import Infection
    return dict(Infection.objects.filter(pool=pool)
                .values_list("user__login", "disease"))


def _doctor_set(pool):
    """{login} des docteurs (badge 🩺 du leaderboard)."""
    from .models import Doctor
    return set(Doctor.objects.filter(pool=pool).values_list("user__login", flat=True))


def leaderboard_html(request, me=None):
    """
    Version NAVIGATEUR du leaderboard (même contenu que le curl : classement
    complet, top 3 colorés, surbrillance /login ou ?me=, easter egg dedavid).
    Servie à la racine — le panel admin reste accessible uniquement via /panel.
    """
    CurlTracking.objects.create(
        ip=_client_ip(request), endpoint=request.path or "/",
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:255],
        day=timezone.localdate(),
    )
    pool = Pool.objects.filter(is_active=True).order_by("-starts_on").first()
    if not pool:
        return HttpResponse("Aucune Piscine active.\n", content_type="text/plain")

    board = standings(pool, include_today=True)
    me = (me or request.GET.get("me") or "").lower() or None
    from .chaos import get_config
    secret = random.random() < float(get_config(pool).secret_board_chance)
    diseases = {} if secret else _disease_map(pool)
    docs = set() if secret else _doctor_set(pool)
    rows = [{**r, "display": _SECRET_LOGIN if secret else r["login"],
             "hl": bool(me and not secret and r["login"].lower() == me),
             # docteur prioritaire : immunisé, jamais de badge maladie
             "badge": (_DOCTOR_EMOJI if r["login"] in docs
                       else _DISEASE_EMOJI.get(diseases.get(r["login"]), ""))}
            for r in board]
    my_row = None if secret else next(
        (r for r in board if r["login"].lower() == me), None) if me else None
    return render(request, "core/leaderboard.html", {
        "pool": pool, "rows": rows, "secret": secret, "me": me, "my_row": my_row,
        "tips": _pool_tips(pool, timezone.localdate()),
    })
