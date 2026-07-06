"""
OfficeRoute — monteur-app (zelfstandige applicatie).

Aparte service die via dezelfde PostgreSQL-database (DATABASE_URL) samenwerkt met de
kantoorsoftware (planning). Lokaal draait hij op SQLite met een mini dev-seed.
"""

import os
import secrets
from flask import Flask, request
from monteur import bp


def _load_secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    f = os.environ.get("PLANNING_OI_SECRET_FILE", ".secret_key")
    try:
        if os.path.exists(f):
            s = open(f, "r", encoding="utf-8").read().strip()
            if s:
                return s
        nk = secrets.token_hex(32)
        open(f, "w", encoding="utf-8").write(nk)
        return nk
    except Exception:
        return secrets.token_hex(32)


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER") or os.environ.get("DATABASE_URL")),
)
app.register_blueprint(bp)


# --- Performance: gzip-compressie + cache-headers (veilig, alleen stdlib) ---
import gzip as _gzip
from datetime import timedelta as _timedelta

app.config["SEND_FILE_MAX_AGE_DEFAULT"] = _timedelta(days=7)
_GZIP_TYPES = ("text/html", "text/css", "application/javascript", "application/json",
               "image/svg+xml", "text/plain", "application/manifest+json")


@app.after_request
def _perf(resp):
    # Statische bestanden lang cachen (stabiele namen + ?v=-versiestempel).
    try:
        if request.path.startswith("/static/"):
            resp.headers.setdefault("Cache-Control", "public, max-age=604800")
    except Exception:
        pass
    # Gzip tekstuele responses als de client dat ondersteunt (grote HTML/CSS/JSON).
    try:
        ae = request.headers.get("Accept-Encoding", "")
        ct = (resp.content_type or "").split(";")[0].strip()
        if ("gzip" in ae and ct in _GZIP_TYPES and resp.status_code == 200
                and "Content-Encoding" not in resp.headers
                and not resp.direct_passthrough):
            data = resp.get_data()
            if len(data) >= 800:
                gz = _gzip.compress(data, 6)
                resp.set_data(gz)
                resp.headers["Content-Encoding"] = "gzip"
                resp.headers["Content-Length"] = str(len(gz))
                resp.headers.add("Vary", "Accept-Encoding")
    except Exception:
        pass
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5060, debug=False)
