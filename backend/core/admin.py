from django.contrib import admin
from django.db.models import Count, Q, Sum
from django.utils.html import format_html, format_html_join
from unfold.admin import ModelAdmin, TabularInline

from .models import (
    Pool, AppUser, Rule, RuleVersion, DailyCoefficient,
    EventLog, DailySnapshot, CurlTracking, HostConfig,
    WeeklyDesignation, AdminAuditLog,
)


@admin.action(description="Annuler (void) les événements sélectionnés")
def void_events(modeladmin, request, queryset):
    n = queryset.update(is_voided=True)
    modeladmin.message_user(request, f"{n} événement(s) annulé(s). Pense à recalculer les snapshots.")


@admin.action(description="Réactiver les événements sélectionnés")
def unvoid_events(modeladmin, request, queryset):
    n = queryset.update(is_voided=False)
    modeladmin.message_user(request, f"{n} événement(s) réactivé(s).")


@admin.action(description="Recalculer les scores à partir de ces jours")
def recompute_from_day(modeladmin, request, queryset):
    from .services import recompute_from
    total = 0
    for dc in queryset.order_by("day"):
        total += len(recompute_from(dc.pool, dc.day))
    modeladmin.message_user(request, f"Snapshots recalculés sur {total} jour(s).")


@admin.action(description="Re-randomiser les multiplicateurs (nouveau tirage)")
def rerandomize_coef(modeladmin, request, queryset):
    from .derived import randomize_daily_coefficient
    n = sum(1 for dc in queryset
            if randomize_daily_coefficient(dc.pool, dc.day, reseed=True) is not None)
    modeladmin.message_user(
        request, f"{n} jour(s) re-randomisé(s). Lance ensuite « Recalculer les scores » pour appliquer.")


def _day_events(obj):
    return EventLog.objects.filter(pool=obj.pool, event_date=obj.day, is_voided=False)


def _hosts_active_on(day):
    """Postes shiny/maudits actifs un jour donné (selon valid_from/valid_to)."""
    return (HostConfig.objects.filter(is_active=True)
            .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=day))
            .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=day)))


class RuleVersionInline(TabularInline):
    model = RuleVersion
    extra = 0
    fields = ("version", "params", "valid_from", "valid_to")
    ordering = ("-version",)


@admin.register(Pool)
class PoolAdmin(ModelAdmin):
    list_display = ("name", "slug", "starts_on", "ends_on", "last_day", "is_active")
    prepopulated_fields = {"slug": ("name",)}
    list_filter = ("is_active",)


@admin.register(AppUser)
class AppUserAdmin(ModelAdmin):
    list_display = ("login", "display_name", "pool", "coalition", "rank_modifier", "is_active")
    list_filter = ("pool", "coalition", "is_active")
    search_fields = ("login", "display_name", "intra_id")


@admin.register(Rule)
class RuleAdmin(ModelAdmin):
    list_display = ("label", "key", "category", "is_active", "current_version")
    list_filter = ("category", "is_active")
    search_fields = ("key", "label")
    inlines = [RuleVersionInline]


@admin.register(RuleVersion)
class RuleVersionAdmin(ModelAdmin):
    list_display = ("rule", "version", "valid_from", "valid_to", "created_by")
    list_filter = ("rule__category", "rule")
    readonly_fields = ("created_at",)


@admin.register(DailyCoefficient)
class DailyCoefficientAdmin(ModelAdmin):
    """Centre de contrôle par JOUR : coefficient + stats + postes spéciaux."""
    list_display = ("day", "pool", "coef_gain", "coef_loss", "coef_event",
                    "is_weekend", "locked", "events_count", "points_total",
                    "active_students", "special_hosts")
    list_display_links = ("day",)
    list_filter = ("pool", "is_weekend", "locked")
    list_editable = ("coef_gain", "coef_loss", "coef_event", "is_weekend", "locked")
    date_hierarchy = "day"
    ordering = ("-day",)
    actions = [rerandomize_coef, recompute_from_day]
    readonly_fields = ("day_summary",)
    fields = ("pool", "day", "coef_gain", "coef_loss", "coef_event",
              "is_weekend", "locked", "day_summary")

    @admin.display(description="Events")
    def events_count(self, obj):
        return _day_events(obj).count()

    @admin.display(description="Points bruts")
    def points_total(self, obj):
        return round(_day_events(obj).aggregate(s=Sum("raw_points"))["s"] or 0)

    @admin.display(description="Étudiants actifs")
    def active_students(self, obj):
        return _day_events(obj).values("user").distinct().count()

    @admin.display(description="Postes ✦/☠")
    def special_hosts(self, obj):
        hosts = _hosts_active_on(obj.day)
        return format_html("✦ {} &nbsp;·&nbsp; ☠ {}",
                           hosts.filter(kind="shiny").count(),
                           hosts.filter(kind="cursed").count())

    @admin.display(description="Détail du jour")
    def day_summary(self, obj):
        ev = _day_events(obj)
        by_type = ev.values("event_type").annotate(n=Count("id"), pts=Sum("raw_points")).order_by("-n")
        if not by_type:
            return format_html("<i>Aucun événement ce jour-là.</i>")
        rows = format_html_join(
            "", "<tr><td style='padding:4px 16px 4px 0'>{}</td>"
                "<td style='padding:4px 16px 4px 0;text-align:right'>{}</td>"
                "<td style='padding:4px 0;text-align:right;color:{}'>{:+}</td></tr>",
            ((r["event_type"], r["n"],
              "#22c55e" if (r["pts"] or 0) >= 0 else "#ef4444", round(r["pts"] or 0))
             for r in by_type),
        )
        hosts = _hosts_active_on(obj.day)
        host_list = format_html_join(
            ", ", "{} ({})",
            ((h.hostname, h.get_kind_display()) for h in hosts),
        ) or format_html("<i>aucun</i>")
        total = round(ev.aggregate(s=Sum("raw_points"))["s"] or 0)
        return format_html(
            "<table style='border-collapse:collapse'>"
            "<thead><tr><th style='text-align:left'>Type</th>"
            "<th style='text-align:right'>Nb</th>"
            "<th style='text-align:right'>Points</th></tr></thead>"
            "<tbody>{}</tbody>"
            "<tfoot><tr><td><b>Total</b></td><td></td>"
            "<td style='text-align:right'><b>{:+}</b></td></tr></tfoot></table>"
            "<p style='margin-top:12px'><b>Postes spéciaux actifs :</b> {}</p>",
            rows, total, host_list,
        )


@admin.register(EventLog)
class EventLogAdmin(ModelAdmin):
    list_display = ("occurred_at", "user", "event_type", "source", "raw_points", "event_date", "is_voided")
    list_filter = ("source", "event_type", "is_voided", "pool", "event_date")
    search_fields = ("user__login", "event_type", "dedup_key")
    date_hierarchy = "event_date"
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at",)
    actions = [void_events, unvoid_events]
    list_per_page = 50


@admin.register(DailySnapshot)
class DailySnapshotAdmin(ModelAdmin):
    list_display = ("day", "user", "rank", "day_final_points", "cumulative_total")
    list_filter = ("pool", "day")
    search_fields = ("user__login",)
    date_hierarchy = "day"
    ordering = ("-day", "rank")


@admin.register(CurlTracking)
class CurlTrackingAdmin(ModelAdmin):
    list_display = ("requested_at", "ip", "user", "endpoint")
    list_filter = ("endpoint", "day")
    search_fields = ("ip", "user__login")
    date_hierarchy = "requested_at"


@admin.register(HostConfig)
class HostConfigAdmin(ModelAdmin):
    list_display = ("hostname", "kind", "points", "is_active", "valid_from", "valid_to")
    list_filter = ("kind", "is_active")
    search_fields = ("hostname",)

    @admin.display(description="Points")
    def points(self, obj):
        pts = (obj.params or {}).get("points")
        if pts is None:
            return "—"
        color = "#22c55e" if pts >= 0 else "#ef4444"
        return format_html("<b style='color:{}'>{:+}</b>", color, pts)


@admin.register(WeeklyDesignation)
class WeeklyDesignationAdmin(ModelAdmin):
    list_display = ("user", "status", "factor", "week_start", "pool")
    list_filter = ("status", "pool", "week_start")
    search_fields = ("user__login",)


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(ModelAdmin):
    list_display = ("at", "staff", "action", "target")
    list_filter = ("action",)
    search_fields = ("action", "target")
    readonly_fields = ("at",)
