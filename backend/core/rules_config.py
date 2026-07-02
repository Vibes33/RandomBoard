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
     {"type": "linear_growth", "value_key": "minutes", "min": 0, "max": 240, "max_points": 300}),
    ("midnight_bonus", "gain", "Connexion entre 23:50 et 23:59",
     {"type": "time_window_bonus", "value_key": "time", "start": "23:50", "end": "23:59",
      "points": 500}),
    ("randominette", "event", "Randominette (pile ou face)",
     {"type": "probability", "proba": 0.5, "points": 150, "else_points": -50}),
    ("kw_quoi_sans_feur", "loss", 'Feedback "quoi" sans "feur" / "coubeh"',
     {"type": "fixed", "min": -50, "max": -30, "rank_penalty": 1}),
    ("reconnect_same_pc", "loss", "Reco sur le MÊME pc dans la journée",
     {"type": "fixed", "min": -35, "max": -15}),
    ("logtime_high", "loss", "Logtime haut (12h–24h)",
     {"type": "linear_growth", "value_key": "minutes", "min": 720, "max": 1440,
      "max_malus": -300}),
    ("aura_first_coalition", "loss", "1er de sa coalition (perte d'aura)",
     {"type": "fixed", "min": -1200, "max": -800}),
    ("last_day_corrections", "event", "Dernier jour : corrections × 1000",
     {"type": "multiplier", "value_key": "correction_points", "factor": 1000}),
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
    ("rush_malus", "loss", "Rush (malus, énorme si note 0)",
     {"type": "threshold_window", "value_key": "mark", "lo": 1, "hi": 125,
      "in_points": -50, "out_points": -300}),
    ("correction_flag", "loss", "Flag de correction (Empty work, Crash…)",
     {"type": "map_lookup", "key_field": "flag", "default": 0,
      "map": {"Empty work": -100, "Crash": -80, "Norme": -60,
              "Can't explain": -80, "Cheat": -200}}),
    ("project_random", "event", "Projet évalué (points random ±, au pif)",
     {"type": "random_modifier", "base": 0, "rand_min": -50, "rand_max": 80}),
]
