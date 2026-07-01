"""
Client de l'API 42 (étape 4) — OAuth2 client_credentials avec POOL DE CLÉS.

Plusieurs applications 42 (UID/secret) se relaient :
- chaque clé a son propre token et son propre cooldown ;
- quand une clé prend un 429 (rate-limit), elle est mise en pause (Retry-After)
  et la requête bascule sur la clé suivante ;
- un mini-intervalle par clé (~0.5 s) respecte la limite ~2 req/s par app.

Avec 10 clés tu multiplies par ~10 le débit autorisé (2 req/s & 1200 req/h chacune).

Doc : https://api.intra.42.fr/apidoc
"""
import threading
import time

import requests
from django.conf import settings


class FtAuthError(Exception):
    pass


class FtRateLimit(Exception):
    pass


class _Key:
    """Une application 42 (clé) avec son token et son état de disponibilité."""
    __slots__ = ("uid", "secret", "token", "token_exp", "available_at", "label")

    def __init__(self, uid, secret):
        self.uid = uid
        self.secret = secret
        self.token = None
        self.token_exp = 0.0
        self.available_at = 0.0  # epoch ; 0 = libre
        self.label = (uid[:16] + "…") if len(uid) > 16 else uid


class FtClient:
    MIN_INTERVAL = 0.5   # s entre 2 appels d'une même clé (~2 req/s)
    MAX_WAIT = 30.0      # si toutes les clés sont en pause plus longtemps → on abandonne

    def __init__(self, credentials=None, base=None):
        creds = credentials if credentials is not None else settings.FT_API_CREDENTIALS
        self.keys = [_Key(u, s) for u, s in creds]
        self.base = (base or settings.FT_API_BASE).rstrip("/")
        self._i = 0
        self._lock = threading.Lock()

    @property
    def configured(self):
        return bool(self.keys)

    # ─── sélection de la clé ───
    def _pick_key(self):
        with self._lock:
            now = time.time()
            for _ in range(len(self.keys)):
                k = self.keys[self._i % len(self.keys)]
                self._i += 1
                if k.available_at <= now:
                    return k
            # toutes en pause → on attend la plus proche (borné)
            soonest = min(k.available_at for k in self.keys)
            wait = soonest - now
        if wait > self.MAX_WAIT:
            raise FtRateLimit(
                f"Toutes les {len(self.keys)} clés sont en cooldown (~{int(wait)}s). Réessaie plus tard.")
        time.sleep(max(0.0, wait))
        return self._pick_key()

    # ─── token d'une clé ───
    def _token(self, key):
        if key.token and time.time() < key.token_exp:
            return key.token
        r = requests.post(
            f"{self.base}/oauth/token",
            data={"grant_type": "client_credentials",
                  "client_id": key.uid, "client_secret": key.secret},
            timeout=15,
        )
        if r.status_code != 200:
            key.available_at = time.time() + 300  # clé invalide → pause 5 min
            raise FtAuthError(f"OAuth {r.status_code} pour {key.label} : {r.text[:160]}")
        d = r.json()
        key.token = d["access_token"]
        key.token_exp = time.time() + int(d.get("expires_in", 7200)) - 60
        return key.token

    # ─── requête avec rotation ───
    def get(self, endpoint, params=None, retries=None):
        url = endpoint if endpoint.startswith("http") else f"{self.base}{endpoint}"
        retries = retries if retries is not None else max(3, len(self.keys) * 2)
        last_err = None
        for _ in range(retries):
            key = self._pick_key()
            try:
                token = self._token(key)
            except FtAuthError as e:
                last_err = e
                continue
            r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                             params=params, timeout=20)
            key.available_at = time.time() + self.MIN_INTERVAL  # throttle ~2 req/s
            if r.status_code == 401:
                key.token = None  # token périmé → on refresh au prochain tour
                continue
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", "2"))
                key.available_at = time.time() + retry_after  # cette clé se repose
                last_err = FtRateLimit(f"429 sur {key.label}")
                continue
            r.raise_for_status()
            return r
        if last_err:
            raise last_err
        raise FtRateLimit("Échec après rotation de toutes les clés.")

    def paginate(self, endpoint, params=None, page_size=100, max_pages=200):
        params = dict(params or {})
        params["page[size]"] = page_size
        page = 1
        while page <= max_pages:
            params["page[number]"] = page
            data = self.get(endpoint, params).json()
            if not data:
                break
            yield from data
            if len(data) < page_size:
                break
            page += 1

    # ─── diagnostic ───
    def verify(self):
        """Teste l'auth de chaque clé. Renvoie [(label, ok, détail)]."""
        out = []
        for key in self.keys:
            try:
                self._token(key)
                out.append((key.label, True, "OK"))
            except Exception as e:  # noqa: BLE001
                out.append((key.label, False, str(e)[:80]))
        return out
