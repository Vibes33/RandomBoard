"""
Authentification du panel (remplace la dépendance à l'admin Django).

`staff_required` protège toutes les vues du panel :
  - non connecté        → redirection vers la page de login du panel (?next=…)
  - connecté non-staff  → 403
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

LOGIN_URL = "/panel/login/"


def staff_required(view):
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path(), LOGIN_URL)
        if not (user.is_active and user.is_staff):
            raise PermissionDenied
        return view(request, *args, **kwargs)
    return _wrapped
