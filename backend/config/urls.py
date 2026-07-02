from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView
from core import api
from core.views import dashboard, healthz, leaderboard_preview

admin.site.site_header = "Chaos Leaderboard 42 — Back-office"
admin.site.site_title = "Chaos 42"
admin.site.index_title = "Administration de la Piscine"

urlpatterns = [
    path("", RedirectView.as_view(url="/panel/", permanent=False)),
    path("dashboard/", dashboard, name="dashboard"),
    path("panel/", api.panel, name="panel"),
    path("panel/api/pools", api.api_pools),
    path("panel/api/pools/<int:pool_id>/activate", api.api_pool_activate),
    path("panel/api/users", api.api_users),
    path("panel/api/users/<int:user_id>/adjust", api.api_user_adjust),
    path("panel/api/logs", api.api_logs),
    path("panel/api/points", api.api_points),
    path("panel/api/days", api.api_days),
    path("panel/api/hosts", api.api_hosts),
    path("panel/api/sync", api.api_sync),
    path("panel/api/sync/status", api.api_sync_status),
    path("panel/api/autosync", api.api_autosync),
    path("admin/", admin.site.urls),
    path("healthz", healthz),
    # Aperçu curl-able (logique de score complète = étape 3)
    path("leaderboard", leaderboard_preview),
]
