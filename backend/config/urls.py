from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView
from core.views import dashboard, healthz, leaderboard_preview

admin.site.site_header = "Chaos Leaderboard 42 — Back-office"
admin.site.site_title = "Chaos 42"
admin.site.index_title = "Administration de la Piscine"

urlpatterns = [
    path("", RedirectView.as_view(url="/dashboard/", permanent=False)),
    path("dashboard/", dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("healthz", healthz),
    # Aperçu curl-able (logique de score complète = étape 3)
    path("leaderboard", leaderboard_preview),
]
