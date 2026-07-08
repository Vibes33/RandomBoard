"""
Configuration FIGÉE des règles de points du Chaos Leaderboard 42.

C'est la source de vérité unique : les règles font partie de la configuration
du projet, elles ne s'ajoutent pas à la main. Elles sont créées automatiquement
au `migrate` (data-migration 0006_seed_rules) → un git clone démarre déjà câblé.

Format d'une règle : (key, category, label, params_v1)
  - category ∈ {"gain", "loss", "event"}
  - params_v1 = paramètres de la version 1 (format moteur, champ "type")

Le range du multiplicateur journalier par défaut de chaque règle :
"""
from decimal import Decimal

# Range du multiplicateur journalier appliqué par défaut à une règle
# (éditable ensuite dans l'onglet « Options de Points » du panel).
DEFAULT_MULT_MIN = Decimal("0.5")
DEFAULT_MULT_MAX = Decimal("2.0")

RULES = [
    ("logtime_low", "gain", "Logtime bas (1min–5h)",
     {"type": "linear_decay", "value_key": "minutes", "min": 1, "max": 300,
      "max_points": 200, "min_points": 0}),
    ("logtime_minute", "gain", "Jackpot de la minute (logtime du jour = 1 min pile)",
     {"type": "fixed", "points": 1000}),
    ("kw_quoi_feur", "gain", 'Feedback contient "quoi feur"',
     {"type": "fixed", "min": 40, "max": 60}),
    ("curl_leaderboard", "gain", "curl sur le leaderboard",
     {"type": "fixed", "min": 3, "max": 8}),
    ("bsq_eval", "gain", "Évaluation projet BSQ",
     {"type": "random_modifier", "base": 100, "rand_min": -30, "rand_max": 30}),
    ("bsq_duration", "loss", "BSQ : durée d'éval hors [30–60 min]",
     {"type": "threshold_window", "value_key": "duration_min", "lo": 30, "hi": 60,
      "in_points": 0, "out_points": -80}),
    ("exam_time", "loss", "Temps passé en examen (+ de temps = malus)",
     {"type": "linear_growth", "value_key": "minutes", "min": 0, "max": 240,
      "max_malus": -150}),
    ("exam_regression", "loss", "Score en baisse depuis l'exam précédent (malus)",
     {"type": "fixed", "points": -150}),
    # Élus du jour (DailyDesignation) : ±pct % des gains du jour, appliqué à la
    # clôture. Le pct vit ici (éditable panel), le montant est calculé au vol.
    ("daily_blessed", "gain", "Béni du jour : bonus % des gains du jour",
     {"type": "from_context", "value_key": "points", "pct": 30}),
    ("daily_cursed", "loss", "Maudit du jour : malus % des gains du jour",
     {"type": "from_context", "value_key": "points", "pct": 30}),
    ("midnight_bonus", "gain", "Logtime total du jour 23h50–23h59 (bonus fixe)",
     {"type": "fixed", "points": 300}),
    ("kw_quoi_sans_feur", "loss", 'Feedback "quoi" sans "feur" / "coubeh"',
     {"type": "fixed", "min": -50, "max": -30, "rank_penalty": 1}),
    ("reconnect_same_pc", "loss", "Reco sur le MÊME pc dans la journée",
     {"type": "fixed", "min": -10, "max": -5}),
    ("logtime_high", "loss", "Logtime > 14h (malus fixe du jour)",
     {"type": "fixed", "points": -50}),
    ("aura_first_coalition", "loss", "1er de sa coalition (perte d'aura)",
     {"type": "fixed", "min": -300, "max": -150}),
    ("last_day_corrections", "event", "Dernier jour : points par correction donnée",
     {"type": "multiplier", "value_key": "correction_points", "factor": 100}),
    # ─── règles avancées (contextuelles) ───
    ("shiny_host", "gain", "Place Bénite (Shiny) — bonus à la connexion",
     {"type": "fixed", "min": 150, "max": 300}),
    ("cursed_host", "loss", "Place Maudite — malus FIXE à la connexion",
     {"type": "fixed", "points": -100}),
    ("binome_cursed", "loss", "Malédiction binôme — Maudit (×2 si croisé)",
     {"type": "weighted", "points": -120}),
    ("binome_blessed", "gain", "Malédiction binôme — Béni (×2 si croisé)",
     {"type": "weighted", "points": 120}),
    ("config_weekend", "event", "[config] coefficient du week-end",
     {"type": "fixed", "points": 0, "factor": 0.5}),
    # ─── mots-clés feedbacks (configurable : ajoute un mot = édite la règle) ───
    ("feedback_keywords", "gain", "Mots-clés dans les feedbacks",
     {"type": "keywords",
      "map": {"quoi feur": 50, "apple": 80, "frieren": 80, "dedavid": 100, "rydelepi": 100},
      "quoi_alone_points": -40, "coubeh_points": -40,
      "rank_penalty_on": ["quoi_alone", "coubeh"]}),
    # ─── ranges du multiplicateur journalier, PAR CATÉGORIE ───
    ("config_daily", "event", "[config] ranges du multiplicateur journalier",
     {"type": "fixed", "points": 0,
      "gain": {"min": 1.0, "max": 1.5},
      "loss": {"min": 1.0, "max": 2.0},
      "event": {"min": 0.8, "max": 1.4}}),
    # ─── ajustement manuel par le staff (points fournis à la création) ───
    ("manual_adjust", "event", "Ajustement manuel (staff)",
     {"type": "from_context", "value_key": "points"}),
    # ─── projets & corrections (issus de scale_teams) ───
    ("shell_malus", "loss", "Rendre Shell 00 / Shell 01",
     {"type": "fixed", "min": -80, "max": -40}),
    ("rush_malus", "loss", "Rush (paliers : ≥50 bonus, raté malus, 0 sanction)",
     {"type": "tiers", "value_key": "mark", "default": -100,
      "tiers": {"0": -100, "1": -50, "50": 50}}),
    ("correction_flag", "loss", "Flag de correction (Empty work, Crash…)",
     {"type": "map_lookup", "key_field": "flag", "default": 0,
      "map": {"Empty work": -100, "Crash": -80, "Norme": -60,
              "Can't explain": -80, "Cheat": -200}}),
    ("project_random", "event", "Projet évalué (points random ±, au pif)",
     {"type": "random_modifier", "base": 0, "rand_min": -80, "rand_max": 140}),
    # ─── nouvelles règles (étape 4.1) ───
    ("assiduity_streak", "gain", "Assiduité : jours consécutifs (week-ends neutres, capé à 7 j)",
     {"type": "multiplier", "value_key": "streak", "factor": 5, "cap": 7}),
    ("project_perfect", "loss", "Rendre un projet pile à 100 (malus)",
     {"type": "fixed", "points": -10}),
    ("cluster_bonus", "event", "Bonus/malus selon le cluster où l'élève est assis",
     {"type": "map_lookup", "key_field": "cluster", "default": 0,
      "map": {"c1": 15, "c2": 0, "c3": -10}}),
    # ─── rubber-banding : le podium finance les derniers (zéro-somme) ───
    ("podium_tax", "loss", "Taxe du podium : top 3 du jour, % des gains",
     {"type": "from_context", "value_key": "points", "pct": 5, "top": 3, "bottom": 10}),
    ("comeback_boost", "gain", "Boost comeback : la taxe du podium, redistribuée",
     {"type": "from_context", "value_key": "points"}),
    # ─── chaos absolu (étape 4.2) : montants portés par PoolConfig ───
    ("stacking_penalty", "loss", "Stacking : malus si on ne corrige pas",
     {"type": "from_context", "value_key": "points"}),
    ("stacking_buff", "gain", "Stacking : buff final au plus gros stackeur",
     {"type": "from_context", "value_key": "points"}),
    ("plague_payout", "event", "Peste & Choléra : gain du groupe vainqueur",
     {"type": "from_context", "value_key": "points"}),
    # ─── trolls (Google Sheet Apps Script) : victime → malus niveau × semaine ───
    ("troll_victim", "loss", "Trollé sur son PC (points par niveau × semaine)",
     {"type": "level_week", "level_key": "level", "default": 0,
      "levels": {"1": -10, "2": -20, "3": -30},
      "week_mult": {"1": 1, "2": 1, "3": 1, "4": 1}}),
]


# ─────────────────────────────────────────────────────────────────────
# Règles DÉSACTIVÉES : jamais déclenchées / doublons / porte-config obsolètes.
# Marquées is_active=False (historique conservé, invisibles dans le panel).
# ─────────────────────────────────────────────────────────────────────
INACTIVE_RULES = {
    "kw_quoi_feur", "kw_quoi_sans_feur",   # doublons de feedback_keywords
    "curl_leaderboard",                    # inattribuable (pas d'auth sur le curl)
    "config_weekend", "config_daily",      # porte-config de l'ancien modèle
    "logtime_low",                         # RETIRÉ (07/2026) : on garde seulement
    #                                        le jackpot de la minute (logtime_minute)

    # randominette (aléa trop violent) et seniority_malus (perte passive punitive)
    # : RETIRÉES du jeu (07/2026) — plus aucune logique ni UI, historique conservé.
}


# ─────────────────────────────────────────────────────────────────────
# SCHÉMA D'ÉDITION : pour chaque règle active, les paramètres modifiables
# depuis le panel. Source unique qui rend les « points de base » éditables
# pour TOUTES les règles (plus seulement les fixed). kind ∈
#   points | value (seuil) | proba | time (HH:MM) | map (table clé→points)
# ─────────────────────────────────────────────────────────────────────
def _f(name, label, kind="points"):
    return {"name": name, "label": label, "kind": kind}


RULE_FIELDS = {
    "bsq_eval": [_f("base", "Points de base"),
                 _f("rand_min", "Aléa min"), _f("rand_max", "Aléa max")],
    "bsq_duration": [_f("lo", "Durée min (minutes)", "value"),
                     _f("hi", "Durée max (minutes)", "value"),
                     _f("in_points", "Points si dans la fenêtre"),
                     _f("out_points", "Points si hors fenêtre")],
    "logtime_minute": [_f("points", "Jackpot (logtime du jour = 1 min)")],
    "midnight_bonus": [_f("points", "Bonus fixe (logtime total 23h50–23h59)")],
    "logtime_high": [_f("points", "Malus fixe (>14h)")],
    "reconnect_same_pc": [_f("min", "Malus min"), _f("max", "Malus max")],
    "aura_first_coalition": [_f("min", "Malus min"), _f("max", "Malus max")],
    "shiny_host": [_f("min", "Bonus min"), _f("max", "Bonus max")],
    "cursed_host": [_f("points", "Malus fixe (place maudite)")],
    "binome_cursed": [_f("points", "Points (Maudit)")],
    "binome_blessed": [_f("points", "Points (Béni)")],
    "feedback_keywords": [_f("map", "Mots-clés → points", "map"),
                          _f("quoi_alone_points", "« quoi » seul"),
                          _f("coubeh_points", "« coubeh »")],
    "shell_malus": [_f("min", "Malus min"), _f("max", "Malus max")],
    "rush_malus": [_f("tiers", "Note (seuil) → points", "map"),
                   _f("default", "Points par défaut")],
    "correction_flag": [_f("map", "Flag → points", "map"),
                        _f("default", "Points par défaut")],
    "project_random": [_f("base", "Points de base"),
                       _f("rand_min", "Aléa min"), _f("rand_max", "Aléa max")],
    "assiduity_streak": [_f("factor", "Points par jour consécutif"),
                         _f("cap", "Plafond (jours comptés)", "value")],
    "project_perfect": [_f("points", "Malus (note = 100)")],
    "cluster_bonus": [_f("map", "Cluster → points", "map"),
                      _f("default", "Points par défaut")],
    "exam_time": [_f("min", "Minutes — borne basse", "value"),
                  _f("max", "Minutes — borne haute", "value"),
                  _f("max_malus", "Malus maximum (à 240 min+)")],
    "exam_regression": [_f("points", "Malus si score en baisse vs exam précédent")],
    "troll_victim": [_f("levels", "Niveau de troll → points", "map"),
                     _f("week_mult", "Semaine (1-4) → multiplicateur", "map"),
                     _f("default", "Points si niveau inconnu")],
    "last_day_corrections": [_f("factor", "Points par correction donnée")],
    "daily_blessed": [_f("pct", "% des gains du jour", "value")],
    "daily_cursed": [_f("pct", "% des gains du jour", "value")],
    "podium_tax": [_f("pct", "% des gains du jour taxé", "value"),
                   _f("top", "Nb de taxés (tête)", "value"),
                   _f("bottom", "Nb de bénéficiaires (queue)", "value")],
    "comeback_boost": [],  # montant = taxe collectée, réparti automatiquement
    "manual_adjust": [],  # points fournis à la main au moment de l'ajustement
    # Montants gérés par PoolConfig (onglet Chaos), pas de champ ici.
    "stacking_penalty": [],
    "stacking_buff": [],
    "plague_payout": [],
}
