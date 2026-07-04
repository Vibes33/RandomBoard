from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import RedirectView
from core import api
from core.views import healthz, leaderboard_preview

urlpatterns = [
    path("", RedirectView.as_view(url="/panel/", permanent=False)),
    # Auth du panel (remplace l'admin Django)
    path("panel/login/", auth_views.LoginView.as_view(
        template_name="core/login.html", redirect_authenticated_user=True), name="login"),
    path("panel/logout/", auth_views.LogoutView.as_view(next_page="/panel/login/"), name="logout"),
    path("panel/", api.panel, name="panel"),
    path("panel/api/pools", api.api_pools),
    path("panel/api/pools/create", api.api_pool_create),
    path("panel/api/pools/discover", api.api_pools_discover),
    path("panel/api/pools/<int:pool_id>/activate", api.api_pool_activate),
    path("panel/api/pools/<int:pool_id>/delete", api.api_pool_delete),
    path("panel/api/pools/<int:pool_id>/fetch", api.api_pool_fetch),
    path("panel/api/users", api.api_users),
    path("panel/api/users/<int:user_id>/adjust", api.api_user_adjust),
    path("panel/api/logs", api.api_logs),
    path("panel/api/points", api.api_points),
    path("panel/api/days", api.api_days),
    path("panel/api/randomize-all", api.api_randomize_all),
    path("panel/api/hosts", api.api_hosts),
    path("panel/api/sync", api.api_sync),
    path("panel/api/sync/status", api.api_sync_status),
    path("panel/api/sync/cancel", api.api_sync_cancel),
    path("panel/api/plague", api.api_plague),
    path("panel/api/autosync", api.api_autosync),
    path("healthz", healthz),
    # Aperçu curl-able (logique de score complète = étape 3)
    path("leaderboard", leaderboard_preview),
]
