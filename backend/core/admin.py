from django.contrib import admin

from .models import (
    Pool, AppUser, Rule, RuleVersion, DailyCoefficient,
    EventLog, DailySnapshot, CurlTracking, HostConfig,
    WeeklyDesignation, AdminAuditLog,
)


class RuleVersionInline(admin.TabularInline):
    model = RuleVersion
    extra = 0
    fields = ("version", "params", "valid_from", "valid_to")
    ordering = ("-version",)


@admin.register(Pool)
class PoolAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "starts_on", "ends_on", "last_day", "is_active")
    prepopulated_fields = {"slug": ("name",)}
    list_filter = ("is_active",)


@admin.register(AppUser)
class AppUserAdmin(admin.ModelAdmin):
    list_display = ("login", "display_name", "pool", "coalition", "rank_modifier", "is_active")
    list_filter = ("pool", "coalition", "is_active")
    search_fields = ("login", "display_name", "intra_id")


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("label", "key", "category", "is_active", "current_version")
    list_filter = ("category", "is_active")
    search_fields = ("key", "label")
    inlines = [RuleVersionInline]


@admin.register(RuleVersion)
class RuleVersionAdmin(admin.ModelAdmin):
    list_display = ("rule", "version", "valid_from", "valid_to", "created_by")
    list_filter = ("rule__category", "rule")
    readonly_fields = ("created_at",)


@admin.register(DailyCoefficient)
class DailyCoefficientAdmin(admin.ModelAdmin):
    list_display = ("day", "pool", "coefficient", "is_weekend", "locked")
    list_filter = ("pool", "is_weekend", "locked")
    list_editable = ("coefficient", "is_weekend")


@admin.register(EventLog)
class EventLogAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "user", "event_type", "source", "raw_points", "event_date", "is_voided")
    list_filter = ("source", "event_type", "is_voided", "pool", "event_date")
    search_fields = ("user__login", "event_type", "dedup_key")
    date_hierarchy = "event_date"
    autocomplete_fields = ("user",)
    readonly_fields = ("created_at",)


@admin.register(DailySnapshot)
class DailySnapshotAdmin(admin.ModelAdmin):
    list_display = ("day", "user", "rank", "day_final_points", "cumulative_total")
    list_filter = ("pool", "day")
    search_fields = ("user__login",)
    date_hierarchy = "day"
    ordering = ("-day", "rank")


@admin.register(CurlTracking)
class CurlTrackingAdmin(admin.ModelAdmin):
    list_display = ("requested_at", "ip", "user", "endpoint")
    list_filter = ("endpoint", "day")
    search_fields = ("ip", "user__login")
    date_hierarchy = "requested_at"


@admin.register(HostConfig)
class HostConfigAdmin(admin.ModelAdmin):
    list_display = ("hostname", "kind", "is_active", "valid_from", "valid_to")
    list_filter = ("kind", "is_active")
    search_fields = ("hostname",)


@admin.register(WeeklyDesignation)
class WeeklyDesignationAdmin(admin.ModelAdmin):
    list_display = ("user", "status", "factor", "week_start", "pool")
    list_filter = ("status", "pool", "week_start")
    search_fields = ("user__login",)


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    list_display = ("at", "staff", "action", "target")
    list_filter = ("action",)
    search_fields = ("action", "target")
    readonly_fields = ("at",)
