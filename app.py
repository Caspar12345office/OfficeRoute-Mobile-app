"""
OfficeRoute — monteur-app (zelfstandige applicatie).

Aparte service die via dezelfde PostgreSQL-database (DATABASE_URL) samenwerkt met de
kantoorsoftware (planning). Lokaal draait hij op SQLite met een mini dev-seed.
"""

import os
import secrets
from flask import Flask
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
app.register_blueprint(bp)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5060, debug=False)
