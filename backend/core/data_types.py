"""
Registre des TYPES DE DONNÉES d'une piscine — pour la gestion granulaire du
panel (compter / reset / refetch chaque type indépendamment).

Chaque type déclare :
  key     : identifiant stable (URL/JS)
  label   : libellé UI
  events  : types d'EventLog rattachés (comptage + annulation au reset)
  models  : modèles annexes (non-event) à vider au reset (noms de modèles)
  flags   : attributs PoolConfig à réarmer au reset (ex: plague_seeded → False)
  source  : "api"    → refetch via une synchro scopée (SyncRun),
            "sheet"  → refetch via le Google Sheet (trolls),
            "random" → « refetch » = re-tirage local (pas d'appel réseau)
  scope   : clés de scope passées à run_full_sync pour le refetch API
"""

DATA_TYPES = [
    {"key": "users", "label": "Étudiants (roster)", "source": "api",
     "events": [], "models": ["AppUser"], "scope": ["users"],
     "warn": "Vider le roster supprime aussi tous leurs events/scores."},
    {"key": "logtime", "label": "Logtime (jackpot minute · 14h · minuit · assiduité)",
     "source": "api", "scope": ["locations"],
     "events": ["logtime_minute", "logtime_high", "midnight_bonus",
                "assiduity_streak", "logtime_low"]},
    {"key": "reconnect", "label": "Reconnexion même PC", "source": "api",
     "events": ["reconnect_same_pc"], "scope": ["locations"]},
    {"key": "cluster", "label": "Cluster (placement)", "source": "api",
     "events": ["cluster_bonus"], "scope": ["locations"]},
    {"key": "hosts", "label": "Places bénites / maudites (effets)", "source": "api",
     "events": ["shiny_host", "cursed_host"], "scope": ["locations", "hosts"]},
    {"key": "exam", "label": "Examens (temps + régression)", "source": "api",
     "events": ["exam_time", "exam_regression"], "scope": ["locations", "exam"]},
    {"key": "feedbacks", "label": "Feedbacks (mots-clés)", "source": "api",
     "events": ["feedback_keywords"], "scope": ["feedbacks"]},
    {"key": "projects", "label": "Projets & évaluations", "source": "api",
     "events": ["bsq_eval", "bsq_duration", "shell_malus", "rush_malus",
                "project_random", "project_perfect", "last_day_corrections"],
     "scope": ["evaluations"]},
    {"key": "flags", "label": "Flags de correction", "source": "api",
     "events": ["correction_flag"], "scope": ["flags"]},
    {"key": "trolls", "label": "Trolls (Google Sheet)", "source": "sheet",
     "events": ["troll_victim"], "scope": ["trolls"]},
    {"key": "plague", "label": "Peste & Choléra", "source": "api",
     "events": ["plague_payout"], "models": ["Infection"],
     "flags": ["plague_seeded"], "scope": ["plague"]},
    {"key": "places", "label": "Tirage des places du jour (hosts)", "source": "random",
     "events": [], "models": ["DailyHost"]},
    {"key": "designations", "label": "Tirage des élus du jour (bénis/maudits)",
     "source": "random", "events": ["daily_blessed", "daily_cursed"],
     "models": ["DailyDesignation"]},
    {"key": "multipliers", "label": "Multiplicateurs journaliers", "source": "random",
     "events": [], "models": ["DailyEventMultiplier"]},
]

BY_KEY = {t["key"]: t for t in DATA_TYPES}

# Union des clés de scope API → sert au refetch « tout » et à la validation.
ALL_SCOPES = {"users", "locations", "hosts", "exam", "feedbacks",
              "evaluations", "flags", "plague", "trolls"}
