"""
Modèles du Chaos Leaderboard 42 — étape 1.

Principes structurants (cf. design) :
  A. L'aléatoire est figé À L'ÉCRITURE (EventLog.raw_points / random_roll),
     jamais recalculé à la lecture.
  B. Tout est append-only : on n'écrase rien, on annule via is_voided
     ou on ajoute un événement d'ajustement.
  C. Les règles sont versionnées dans le temps (RuleVersion.valid_from/valid_to) :
     le calcul d'un jour J utilise la version valide à J.
"""
from django.conf import settings
from django.db import models


# ────────────────────────────────────────────────────────────────────
# Référentiel / identité
# ────────────────────────────────────────────────────────────────────
class Pool(models.Model):
    """Une Piscine (session)."""
    name = models.CharField(max_length=120)
    slug = models.SlugField(unique=True)
    starts_on = models.DateField()
    ends_on = models.DateField()
    last_day = models.DateField(
        help_text="Dernier jour : déclenche le multiplicateur extrême sur les corrections."
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Piscine"
        verbose_name_plural = "Piscines"

    def __str__(self):
        return self.name


class AppUser(models.Model):
    """Miroir local d'un étudiant 42 (alimenté par l'API à l'étape 4)."""
    intra_id = models.BigIntegerField(unique=True, null=True, blank=True)
    login = models.CharField(max_length=64, unique=True)
    display_name = models.CharField(max_length=120, blank=True)
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="users")
    coalition = models.CharField(max_length=64, blank=True)
    rank_modifier = models.IntegerField(
        default=0,
        help_text="Décalage de rang manuel (ex: pénalité 'quoi sans feur' = perd 1 place).",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Étudiant"
        verbose_name_plural = "Étudiants"

    def __str__(self):
        return self.login


# ────────────────────────────────────────────────────────────────────
# Configuration dynamique versionnée (ZÉRO hardcode)
# ────────────────────────────────────────────────────────────────────
class Rule(models.Model):
    """Une règle de points. Sa config vit dans la RuleVersion courante."""

    class Category(models.TextChoices):
        GAIN = "gain", "Gain"
        LOSS = "loss", "Perte"
        EVENT = "event", "Événement / IRL"

    key = models.SlugField(unique=True, help_text="Ex: logtime_low, kw_quoi_feur, bsq_eval")
    category = models.CharField(max_length=16, choices=Category.choices)
    label = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Règle"
        verbose_name_plural = "Règles"

    def __str__(self):
        return f"[{self.category}] {self.label}"

    @property
    def current_version(self):
        return self.versions.filter(valid_to__isnull=True).order_by("-version").first()


class RuleVersion(models.Model):
    """
    Version temporelle d'une règle. On ne MODIFIE jamais une version :
    changer une règle = clôturer la version courante (valid_to) et en créer une nouvelle.
    Toute la logique métier est dans `params` (JSONB).
    """
    rule = models.ForeignKey(Rule, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    params = models.JSONField(
        default=dict,
        help_text='Ex: {"base": 100, "decay": "linear", "proba": 0.3, "multiplier": 1000}',
    )
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True, help_text="NULL = version courante")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Version de règle"
        verbose_name_plural = "Versions de règles"
        unique_together = ("rule", "version")
        ordering = ("rule", "-version")

    def __str__(self):
        active = "courante" if self.valid_to is None else "archivée"
        return f"{self.rule.key} v{self.version} ({active})"


class DailyCoefficient(models.Model):
    """Le multiplicateur journalier appliqué à la somme des points du jour."""
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="coefficients")
    day = models.DateField()
    coefficient = models.DecimalField(max_digits=8, decimal_places=4, default=1)
    is_weekend = models.BooleanField(default=False, help_text="Déclenche le coef pénalisant week-end.")
    locked = models.BooleanField(default=False, help_text="True une fois le jour figé (snapshot fait).")

    class Meta:
        verbose_name = "Coefficient journalier"
        verbose_name_plural = "Coefficients journaliers"
        unique_together = ("pool", "day")
        ordering = ("-day",)

    def __str__(self):
        return f"{self.day} × {self.coefficient}"


# ────────────────────────────────────────────────────────────────────
# Le cœur : log d'événements chronologique (append-only)
# ────────────────────────────────────────────────────────────────────
class EventLog(models.Model):
    class Source(models.TextChoices):
        API_42 = "api_42", "API 42"
        WEB = "web", "Web (curl)"
        MANUAL = "manual", "Manuel (staff)"
        SYSTEM = "system", "Système"

    user = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name="events")
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=64, help_text="logtime, exam, feedback_keyword, curl...")
    source = models.CharField(max_length=16, choices=Source.choices)

    occurred_at = models.DateTimeField(help_text="Horodatage métier (begin_at 42, etc.)")
    event_date = models.DateField(db_index=True, help_text="Jour métier — clé d'agrégation des snapshots.")

    rule = models.ForeignKey(Rule, on_delete=models.SET_NULL, null=True, blank=True)
    rule_version = models.PositiveIntegerField(null=True, blank=True, help_text="Version EXACTE appliquée.")

    raw_payload = models.JSONField(default=dict, blank=True, help_text="Données brutes API conservées.")
    raw_points = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text="Points AVANT coef journalier. Random DÉJÀ figé ici.",
    )
    random_roll = models.JSONField(null=True, blank=True, help_text="Tirage figé (reproductibilité).")

    dedup_key = models.CharField(
        max_length=200, null=True, blank=True, unique=True,
        help_text="Clé d'idempotence pour le polling API (évite les doublons).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    is_voided = models.BooleanField(default=False, help_text="Annulation logique (jamais de DELETE).")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Événement (log)"
        verbose_name_plural = "Événements (log)"
        ordering = ("-occurred_at",)
        indexes = [
            models.Index(fields=["user", "event_date"]),
            models.Index(fields=["pool", "event_date"]),
        ]

    def __str__(self):
        return f"{self.user.login} · {self.event_type} · {self.raw_points} ({self.event_date})"


# ────────────────────────────────────────────────────────────────────
# L'optimisation : snapshots quotidiens
# ────────────────────────────────────────────────────────────────────
class DailySnapshot(models.Model):
    """
    Score figé d'un user pour un jour. cumulative_total est une somme courante :
    cumulative(J) = cumulative(J-1) + day_final_points  → jamais de recalcul depuis 0.
    """
    user = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name="snapshots")
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="snapshots")
    day = models.DateField(db_index=True)

    day_raw_points = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    day_coefficient = models.DecimalField(max_digits=8, decimal_places=4, default=1)
    day_final_points = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    cumulative_total = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    rank = models.IntegerField(null=True, blank=True)
    rules_fingerprint = models.CharField(
        max_length=64, blank=True,
        help_text="Hash des versions de règles utilisées (détection des jours 'dirty').",
    )
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Snapshot quotidien"
        verbose_name_plural = "Snapshots quotidiens"
        unique_together = ("user", "day")
        ordering = ("-day", "rank")

    def __str__(self):
        return f"{self.user.login} · {self.day} · cumul {self.cumulative_total}"


# ────────────────────────────────────────────────────────────────────
# Tracking web + configs spéciales
# ────────────────────────────────────────────────────────────────────
class CurlTracking(models.Model):
    """Chaque appel à GET /leaderboard. Sert aux gains (curl modéré) ET pertes (spam)."""
    user = models.ForeignKey(AppUser, on_delete=models.SET_NULL, null=True, blank=True)
    ip = models.GenericIPAddressField()
    endpoint = models.CharField(max_length=120)
    user_agent = models.CharField(max_length=255, blank=True)
    day = models.DateField(db_index=True)
    requested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tracking curl"
        verbose_name_plural = "Tracking curl"
        ordering = ("-requested_at",)
        indexes = [models.Index(fields=["ip", "day"])]

    def __str__(self):
        return f"{self.ip} · {self.endpoint} · {self.requested_at:%Y-%m-%d %H:%M}"


class HostConfig(models.Model):
    """Postes de travail spéciaux (shiny = bonus, cursed = malus)."""

    class Kind(models.TextChoices):
        SHINY = "shiny", "Shiny (bonus)"
        CURSED = "cursed", "Maudit (malus)"

    hostname = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    params = models.JSONField(default=dict, blank=True, help_text='Ex: {"points": 250}')
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Poste spécial"
        verbose_name_plural = "Postes spéciaux"

    def __str__(self):
        return f"{self.hostname} ({self.kind})"


class WeeklyDesignation(models.Model):
    """La malédiction des binômes : maudits/bénis désignés chaque semaine."""

    class Status(models.TextChoices):
        CURSED = "cursed", "Maudit"
        BLESSED = "blessed", "Béni"

    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="designations")
    user = models.ForeignKey(AppUser, on_delete=models.CASCADE, related_name="designations")
    week_start = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices)
    factor = models.DecimalField(max_digits=8, decimal_places=4, default=1)

    class Meta:
        verbose_name = "Désignation hebdo"
        verbose_name_plural = "Désignations hebdo"
        unique_together = ("user", "week_start")

    def __str__(self):
        return f"{self.user.login} · {self.status} · semaine {self.week_start}"


class AdminAuditLog(models.Model):
    """Traçabilité de toute action staff (changement de règle, ajustement de score...)."""
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=120)
    target = models.CharField(max_length=200, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Audit admin"
        verbose_name_plural = "Audit admin"
        ordering = ("-at",)

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} · {self.action}"
