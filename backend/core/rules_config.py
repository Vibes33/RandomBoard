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
    ("kw_quoi_feur", "gain", 'Feedback contient "quoi feur"',
     {"type": "fixed", "min": 40, "max": 60}),
    ("curl_leaderboard", "gain", "curl sur le leaderboard",
     {"type": "fixed", "min": 3, "max": 8}),
    ("bsq_eval", "gain", "Évaluation projet BSQ",
     {"type": "random_modifier", "base": 100, "rand_min": -30, "rand_max": 30}),
    ("bsq_duration", "loss", "BSQ : durée d'éval hors [30–60 min]",
     {"type": "threshold_window", "value_key": "duration_min", "lo": 30, "hi": 60,
      "in_points": 0, "out_points": -80}),
    ("exam_time", "gain", "Temps passé en examen",
     {"type": "linear_growth", "value_key": "minutes", "min": 0, "max": 240,
      "max_points": 300}),
    # Élus du jour (DailyDesignation) : ±pct % des gains du jour, appliqué à la
    # clôture. Le pct vit ici (éditable panel), le montant est calculé au vol.
    ("daily_blessed", "gain", "Béni du jour : bonus % des gains du jour",
     {"type": "from_context", "value_key": "points", "pct": 30}),
    ("daily_cursed", "loss", "Maudit du jour : malus % des gains du jour",
     {"type": "from_context", "value_key": "points", "pct": 30}),
    ("midnight_bonus", "gain", "Logtime quasi-plein (paliers : 20h / 23h50)",
     {"type": "tiers", "value_key": "minutes", "default": 0,
      "tiers": {"1200": 100, "1430": 300}}),
    ("randominette", "event", "Randominette (pile ou face, moitié basse du classement)",
     {"type": "probability", "proba": 0.5, "points": 150, "else_points": -25}),
    ("kw_quoi_sans_feur", "loss", 'Feedback "quoi" sans "feur" / "coubeh"',
     {"type": "fixed", "min": -50, "max": -30, "rank_penalty": 1}),
    ("reconnect_same_pc", "loss", "Reco sur le MÊME pc dans la journée",
     {"type": "fixed", "min": -20, "max": -10}),
    ("logtime_high", "loss", "Logtime haut (12h–24h)",
     {"type": "linear_growth", "value_key": "minutes", "min": 720, "max": 1440,
      "max_malus": -300}),
    ("aura_first_coalition", "loss", "1er de sa coalition (perte d'aura)",
     {"type": "fixed", "min": -1200, "max": -800}),
    ("last_day_corrections", "event", "Dernier jour : points par correction donnée",
     {"type": "multiplier", "value_key": "correction_points", "factor": 100}),
    # ─── règles avancées (contextuelles) ───
    ("shiny_host", "gain", "Place Bénite (Shiny) — bonus à la connexion",
     {"type": "fixed", "min": 150, "max": 300}),
    ("cursed_host", "loss", "Place Maudite — malus à la connexion",
     {"type": "fixed", "min": -300, "max": -100}),
    ("binome_cursed", "loss", "Malédiction binôme — Maudit (×2 si croisé)",
     {"type": "weighted", "points": -120}),
    ("binome_blessed", "gain", "Malédiction binôme — Béni (×2 si croisé)",
     {"type": "weighted", "points": 120}),
    ("seniority_malus", "loss", "Malus d'ancienneté (croît avec les semaines)",
     {"type": "linear_growth", "value_key": "weeks", "min": 0, "max": 4, "max_malus": -200}),
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
     {"type": "tiers", "value_key": "mark", "default": -150,
      "tiers": {"0": -150, "1": -50, "50": 50}}),
    ("correction_flag", "loss", "Flag de correction (Empty work, Crash…)",
     {"type": "map_lookup", "key_field": "flag", "default": 0,
      "map": {"Empty work": -100, "Crash": -80, "Norme": -60,
              "Can't explain": -80, "Cheat": -200}}),
    ("project_random", "event", "Projet évalué (points random ±, au pif)",
     {"type": "random_modifier", "base": 0, "rand_min": -50, "rand_max": 80}),
    # ─── nouvelles règles (étape 4.1) ───
    ("assiduity_streak", "gain", "Assiduité : jours consécutifs (week-ends neutres, capé à 7 j)",
     {"type": "multiplier", "value_key": "streak", "factor": 10, "cap": 7}),
    ("project_perfect", "gain", "Projet validé pile à 100 (bonus)",
     {"type": "fixed", "points": 150}),
    ("cluster_bonus", "event", "Bonus/malus selon le cluster où l'élève est assis",
     {"type": "map_lookup", "key_field": "cluster", "default": 0,
      "map": {"c1": 15, "c2": 0, "c3": -10}}),
    # ─── chaos absolu (étape 4.2) : montants portés par PoolConfig ───
    ("stacking_penalty", "loss", "Stacking : malus si on ne corrige pas",
     {"type": "from_context", "value_key": "points"}),
    ("stacking_buff", "gain", "Stacking : buff final au plus gros stackeur",
     {"type": "from_context", "value_key": "points"}),
    ("plague_payout", "event", "Peste & Choléra : gain du groupe vainqueur",
     {"type": "from_context", "value_key": "points"}),
]


# ─────────────────────────────────────────────────────────────────────
# Règles DÉSACTIVÉES : jamais déclenchées / doublons / porte-config obsolètes.
# Marquées is_active=False (historique conservé, invisibles dans le panel).
# ─────────────────────────────────────────────────────────────────────
INACTIVE_RULES = {
    "kw_quoi_feur", "kw_quoi_sans_feur",   # doublons de feedback_keywords
    "curl_leaderboard",                    # inattribuable (pas d'auth sur le curl)
    "config_weekend", "config_daily",      # porte-config de l'ancien modèle
    # randominette / exam_time / last_day_corrections : RÉACTIVÉES (audit 07/2026)
    # et câblées — randominette moitié basse, exam via overlap locations,
    # last_day via corrections données le dernier jour.
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
    "logtime_low": [_f("max_points", "Points si logtime minimal"),
                    _f("min_points", "Points si logtime maximal"),
                    _f("min", "Minutes — borne basse", "value"),
                    _f("max", "Minutes — borne haute", "value")],
    "bsq_eval": [_f("base", "Points de base"),
                 _f("rand_min", "Aléa min"), _f("rand_max", "Aléa max")],
    "bsq_duration": [_f("lo", "Durée min (minutes)", "value"),
                     _f("hi", "Durée max (minutes)", "value"),
                     _f("in_points", "Points si dans la fenêtre"),
                     _f("out_points", "Points si hors fenêtre")],
    "midnight_bonus": [_f("tiers", "Minutes de log → bonus", "map"),
                       _f("default", "Points par défaut")],
    "logtime_high": [_f("min", "Minutes — borne basse", "value"),
                     _f("max", "Minutes — borne haute", "value"),
                     _f("max_malus", "Malus maximal")],
    "reconnect_same_pc": [_f("min", "Malus min"), _f("max", "Malus max")],
    "aura_first_coalition": [_f("min", "Malus min"), _f("max", "Malus max")],
    "shiny_host": [_f("min", "Bonus min"), _f("max", "Bonus max")],
    "cursed_host": [_f("min", "Malus min"), _f("max", "Malus max")],
    "binome_cursed": [_f("points", "Points (Maudit)")],
    "binome_blessed": [_f("points", "Points (Béni)")],
    "seniority_malus": [_f("min", "Semaines — borne basse", "value"),
                        _f("max", "Semaines — borne haute", "value"),
                        _f("max_malus", "Malus maximal")],
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
    "project_perfect": [_f("points", "Bonus (note = 100)")],
    "cluster_bonus": [_f("map", "Cluster → points", "map"),
                      _f("default", "Points par défaut")],
    "randominette": [_f("proba", "Probabilité de gagner", "proba"),
                     _f("points", "Points si gagné"),
                     _f("else_points", "Points si perdu")],
    "exam_time": [_f("min", "Minutes — borne basse", "value"),
                  _f("max", "Minutes — borne haute", "value"),
                  _f("max_points", "Points maximum")],
    "last_day_corrections": [_f("factor", "Points par correction donnée")],
    "daily_blessed": [_f("pct", "% des gains du jour", "value")],
    "daily_cursed": [_f("pct", "% des gains du jour", "value")],
    "manual_adjust": [],  # points fournis à la main au moment de l'ajustement
    # Montants gérés par PoolConfig (onglet Chaos), pas de champ ici.
    "stacking_penalty": [],
    "stacking_buff": [],
    "plague_payout": [],
}
