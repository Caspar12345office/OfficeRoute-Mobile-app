"""
OfficeRoute — monteur-app (zelfstandige service).

Aparte repo/Render-service die via dezelfde PostgreSQL-database samenwerkt met de
kantoorsoftware (planning). Bevat alleen de monteur-functionaliteit. De database wordt
beheerd door de kantoorsoftware; deze app leest/schrijft de gedeelde tabellen.

DB: PostgreSQL als DATABASE_URL is gezet (productie), anders lokaal SQLite (ontwikkeling).
"""

from flask import (Blueprint, render_template, request, redirect, url_for, session,
                   flash, jsonify, Response, abort, send_from_directory)
from werkzeug.security import check_password_hash, generate_password_hash
import os, json, time, sqlite3, secrets
import re as _re
from datetime import datetime, timedelta

# Endpoint-namespace 'planning' aangehouden zodat de gedeelde templates (url_for('planning.*')) werken.
bp = Blueprint("planning", __name__, url_prefix="", template_folder="templates")

BRAND = "OfficeRoute"
HOME_BASE = "Breda"
ALERT_THRESHOLD = 20
ROLE_LABELS = {"beheerder": "Beheerder", "manager": "Manager", "planner": "Planner",
               "administratie": "Administratie", "monteur": "Monteur"}
PERMISSION_KEYS = ["view_planning", "edit_planning", "monteur_app", "complete_deliveries"]

DB_PATH = os.environ.get("PLANNING_OI_DB_PATH", "monteur.db")
UPLOAD_DIR = os.environ.get("PLANNING_OI_UPLOADS", "oi_uploads")
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
except Exception:
    pass

# --------------------------------------------------------------------------- #
#  Database-laag (gelijk aan de kantoorsoftware) — SQLite of PostgreSQL
# --------------------------------------------------------------------------- #
_PG_URL = os.environ.get("DATABASE_URL", "")
if _PG_URL.startswith("postgres://"):
    _PG_URL = _PG_URL.replace("postgres://", "postgresql://", 1)
IS_PG = bool(_PG_URL)
_NO_ID_TABLES = {"monteur_location", "route_closed", "integrations", "settings"}


def _xlate(sql):
    is_ignore = "INSERT OR IGNORE" in sql.upper()
    s = _re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql, flags=_re.I)
    s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    s = s.replace("qty || 'x ' || name", "qty::text || 'x ' || name")
    s = s.replace("?", "%s")
    s = _re.sub(r'\bLIKE\b', 'ILIKE', s)
    if is_ignore and "ON CONFLICT" not in s.upper():
        s += " ON CONFLICT DO NOTHING"
    up = s.lstrip().upper()
    m = _re.match(r'INSERT\s+INTO\s+([a-z_]+)', s.lstrip(), flags=_re.I)
    target = (m.group(1).lower() if m else "")
    append_returning = (up.startswith("INSERT") and "RETURNING" not in up
                        and "ON CONFLICT" not in up and target not in _NO_ID_TABLES)
    if append_returning:
        s += " RETURNING id"
    return s, append_returning


class _Row:
    __slots__ = ("_c", "_v")
    def __init__(self, cols, vals):
        self._c = cols; self._v = vals
    def __getitem__(self, k):
        return self._v[k] if isinstance(k, int) else self._v[self._c.index(k)]
    def keys(self):
        return self._c
    def get(self, k, d=None):
        try:
            return self[k]
        except Exception:
            return d


def _pg_rowfactory(cur):
    cols = [d.name for d in cur.description] if cur.description else []
    def make(values):
        return _Row(cols, list(values))
    return make


class _PgCur:
    def __init__(self, conn):
        self._conn = conn
        self._cur = conn._raw.cursor(row_factory=_pg_rowfactory)
        self.lastrowid = None
        self._scalar = None
    def execute(self, sql, params=()):
        if "last_insert_rowid()" in sql.lower():
            self._scalar = self._conn._lastid
            return self
        s, ret = _xlate(sql)
        self._cur.execute(s, tuple(params) if params else None)
        if ret:
            try:
                row = self._cur.fetchone()
                self.lastrowid = row[0]; self._conn._lastid = row[0]
            except Exception:
                pass
        return self
    def executescript(self, script):
        for stmt in script.split(";"):
            if stmt.strip():
                self.execute(stmt)
        return self
    def fetchone(self):
        if self._scalar is not None:
            v = self._scalar; self._scalar = None
            return [v]
        return self._cur.fetchone()
    def fetchall(self):
        return self._cur.fetchall()
    def __iter__(self):
        return iter(self._cur)


class _PgConn:
    def __init__(self):
        import psycopg
        self._raw = psycopg.connect(_PG_URL, autocommit=True)
        self._lastid = None
    def cursor(self):
        return _PgCur(self)
    def execute(self, sql, params=()):
        return _PgCur(self).execute(sql, params)
    def executescript(self, script):
        return _PgCur(self).executescript(script)
    def commit(self):
        pass
    def close(self):
        try:
            self._raw.close()
        except Exception:
            pass


def db():
    if IS_PG:
        return _PgConn()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA busy_timeout=5000")
    except Exception:
        pass
    return conn


# --------------------------------------------------------------------------- #
#  Schema garanderen (geen seed — de kantoorsoftware beheert de data).
#  Lokaal (SQLite) een mini dev-seed zodat je de app kunt testen.
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE,
  password TEXT, role TEXT, permissions TEXT, phone TEXT, monteur_id INTEGER, active INTEGER DEFAULT 1,
  created_at TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS monteurs(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT, email TEXT,
  speed INTEGER DEFAULT 3, color TEXT, bus_id INTEGER, home_address TEXT, home_lat REAL, home_lng REAL,
  standard INTEGER DEFAULT 1, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT, phone TEXT,
  address TEXT, postal TEXT, city TEXT, invoice_address TEXT, notes TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, order_number TEXT, client_id INTEGER,
  source TEXT, is_draft INTEGER DEFAULT 0, status TEXT, delivery_address TEXT, city TEXT, postal TEXT,
  invoice_address TEXT, phone TEXT, email TEXT, desired_date TEXT, notes TEXT, instructions TEXT,
  amount REAL DEFAULT 0, volume REAL DEFAULT 0, weight REAL DEFAULT 0, montage_min INTEGER DEFAULT 30,
  service_type TEXT DEFAULT 'montage', pakbon TEXT, fulfilled INTEGER DEFAULT 0, fulfilled_at TEXT,
  shopify_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, name TEXT, qty INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS planning(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER UNIQUE, monteur_id INTEGER,
  bus_id INTEGER, date TEXT, slot_start TEXT, slot_end TEXT, sequence INTEGER DEFAULT 0, confirmed INTEGER DEFAULT 0,
  mailed INTEGER DEFAULT 0, arrival_mailed INTEGER DEFAULT 0, delay_mailed INTEGER DEFAULT 0, status TEXT DEFAULT 'gepland');
CREATE TABLE IF NOT EXISTS monteur_location(monteur_id INTEGER PRIMARY KEY, lat REAL, lng REAL, updated_at TEXT, live INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, monteur_id INTEGER,
  receiver TEXT, signature TEXT, outcome TEXT, sub_outcome TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS route_closed(monteur_id INTEGER, date TEXT, ts TEXT, PRIMARY KEY(monteur_id, date));
CREATE TABLE IF NOT EXISTS leave_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, user_name TEXT,
  is_monteur INTEGER, monteur_id INTEGER, category TEXT, leave_type TEXT, date_from TEXT, date_to TEXT,
  time_from TEXT, time_to TEXT, reason TEXT, status TEXT DEFAULT 'open', decided_by TEXT, decision_reason TEXT,
  decided_at TEXT, decided_seen INTEGER DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS integrations(ikey TEXT, field TEXT, value TEXT, PRIMARY KEY(ikey, field));
CREATE TABLE IF NOT EXISTS settings(skey TEXT PRIMARY KEY, value TEXT);
"""


def init_db():
    conn = db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    except Exception:
        pass
    # mini dev-seed alleen lokaal (SQLite) zodat de app testbaar is
    if not IS_PG:
        try:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                today = datetime.now().date().isoformat()
                conn.execute("INSERT INTO monteurs(name,phone,color,home_address,home_lat,home_lng) VALUES(?,?,?,?,?,?)",
                             ("Tom", "06-21110011", "#0f3d3e", "Ginnekenweg 200, Breda", 51.57, 4.78))
                conn.execute("""INSERT INTO users(name,email,password,role,permissions,monteur_id,active,created_at)
                                VALUES(?,?,?,?,?,?,1,?)""",
                             ("Tom", "tom@office-interior.nl", generate_password_hash("PlanningOI2025!"),
                              "monteur", json.dumps(["monteur_app"]), 1, today))
                conn.execute("INSERT INTO clients(name,city,created_at) VALUES(?,?,?)", ("Gemeente Tilburg", "Tilburg", today))
                conn.execute("""INSERT INTO orders(order_number,client_id,source,status,delivery_address,city,postal,
                                montage_min,service_type,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                             ("36339", 1, "manual", "gepland", "Stadhuisplein 130, 5038 TC Tilburg", "Tilburg", "5038 TC", 50, "levering", today))
                conn.execute("INSERT INTO order_items(order_id,name,qty) VALUES(1,?,1)", ("Kastenwand",))
                conn.execute("""INSERT INTO planning(order_id,monteur_id,date,sequence,status) VALUES(1,1,?,0,'gepland')""", (today,))
                conn.commit()
        except Exception:
            pass
    conn.close()


# --------------------------------------------------------------------------- #
#  Auth & helpers
# --------------------------------------------------------------------------- #
def current_user():
    uid = session.get("p_user_id")
    if not uid:
        return None
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    conn.close()
    return u


def user_perms(u):
    if not u:
        return set()
    if u["role"] == "beheerder":
        return set(PERMISSION_KEYS)
    try:
        return set(json.loads(u["permissions"] or "[]"))
    except Exception:
        return set()


def has_perm(p):
    return p in user_perms(current_user())


def integ_status(ikey):
    conn = db()
    rows = {r["field"]: r["value"] for r in
            conn.execute("SELECT field,value FROM integrations WHERE ikey=?", (ikey,)).fetchall()}
    conn.close()
    return "verbonden" if any((v or "").strip() for v in rows.values()) else "niet_gekoppeld"


def route_alerts(monteur_id, has_stops):
    out = []
    if has_stops and monteur_id == 1:
        out.append({"icon": "🚗", "desc": "File A2 richting Den Bosch", "min": 25})
        out.append({"icon": "🚧", "desc": "Wegwerkzaamheden N65 (Tilburg)", "min": 10})
    return [a for a in out if a["min"] >= ALERT_THRESHOLD]


def my_unseen_decision(u):
    if not u:
        return None
    conn = db()
    r = conn.execute("""SELECT * FROM leave_requests WHERE user_id=? AND status!='open' AND decided_seen=0
                        ORDER BY decided_at DESC LIMIT 1""", (u["id"],)).fetchone()
    conn.close()
    return dict(r) if r else None


def _today_iso():
    return datetime.now().date().isoformat()


@bp.app_context_processor
def _inject():
    u = current_user()
    return {"p_user": u, "p_has_perm": has_perm, "p_perms": user_perms(u),
            "ROLE_LABELS": ROLE_LABELS, "HOME_BASE": HOME_BASE, "BRAND": BRAND,
            "p_leave_decision": my_unseen_decision(u) if u else None}


def login_required(perm=None):
    u = current_user()
    if not u:
        return redirect(url_for("planning.login", next=request.path))
    if perm and perm not in user_perms(u):
        abort(403)
    return None


# --------------------------------------------------------------------------- #
#  Auth-routes (met 2FA)
# --------------------------------------------------------------------------- #
@bp.route("/")
def home():
    return redirect(url_for("planning.monteur_app") if current_user() else url_for("planning.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""; show_2fa = False; demo_code = None; twofa_email = None
    if request.method == "POST":
        if request.form.get("twofa_code") is not None:
            tf = session.get("twofa") or {}
            code = (request.form.get("twofa_code") or "").strip()
            if not tf:
                error = "Sessie verlopen. Log opnieuw in."
            elif time.time() > tf.get("exp", 0):
                session.pop("twofa", None); error = "Code verlopen."
            elif code == tf.get("code"):
                session["p_user_id"] = tf["uid"]; session.pop("twofa", None)
                return redirect(url_for("planning.monteur_app"))
            else:
                error = "Onjuiste code."; show_2fa = True; demo_code = tf.get("code"); twofa_email = tf.get("email")
        else:
            email = (request.form.get("email") or "").strip().lower()
            pw = request.form.get("password") or ""
            conn = db()
            u = conn.execute("SELECT * FROM users WHERE lower(email)=? AND active=1", (email,)).fetchone()
            conn.close()
            if u and check_password_hash(u["password"], pw):
                code = "%06d" % secrets.randbelow(1000000)
                session["twofa"] = {"uid": u["id"], "code": code, "exp": time.time() + 300, "email": u["email"]}
                show_2fa = True; demo_code = code; twofa_email = u["email"]
            else:
                error = "Onjuiste inloggegevens."
    return render_template("planning/login.html", error=error, show_2fa=show_2fa,
                           demo_code=demo_code, twofa_email=twofa_email)


@bp.route("/logout")
def logout():
    session.pop("p_user_id", None); session.pop("twofa", None)
    return redirect(url_for("planning.login"))


# --------------------------------------------------------------------------- #
#  Monteur-app
# --------------------------------------------------------------------------- #
@bp.route("/monteur")
def monteur_app():
    guard = login_required("monteur_app")
    if guard:
        return guard
    u = current_user()
    conn = db()
    mid = u["monteur_id"]
    today = _today_iso()
    jobs, monteur = [], None
    if mid:
        monteur = conn.execute("SELECT * FROM monteurs WHERE id=?", (mid,)).fetchone()
        jobs = conn.execute("""SELECT p.*, o.id AS oid, o.order_number, o.delivery_address, o.phone, o.instructions,
                               o.montage_min, o.service_type, o.pakbon, c.name AS client,
                               (SELECT GROUP_CONCAT(qty || 'x ' || name, ', ') FROM order_items WHERE order_id=o.id) AS items
                               FROM planning p JOIN orders o ON o.id=p.order_id
                               LEFT JOIN clients c ON c.id=o.client_id
                               WHERE p.monteur_id=? AND p.date=? ORDER BY p.sequence""", (mid, today)).fetchall()
    closed = bool(mid and conn.execute("SELECT 1 FROM route_closed WHERE monteur_id=? AND date=?",
                                       (mid, today)).fetchone())
    conn.close()
    alerts = route_alerts(mid, bool(jobs)) if mid else []
    all_done = bool(jobs) and all(j["status"] == "afgerond" for j in jobs)
    return render_template("planning/monteur_app.html", monteur=monteur, jobs=jobs, alerts=alerts,
                           closed=closed, all_done=all_done,
                           maps_ready=(integ_status("google_maps") == "verbonden"))


@bp.route("/monteur/complete/<int:pid>", methods=["POST"])
def monteur_complete(pid):
    if not has_perm("monteur_app") and not has_perm("complete_deliveries"):
        abort(403)
    receiver = (request.form.get("receiver") or "").strip()
    signature = request.form.get("signature") or ""
    outcome = request.form.get("outcome") or "succesvol"
    sub = request.form.get("sub_outcome") or ""
    if outcome == "succesvol" and (not receiver or not signature):
        return jsonify(ok=False, error="Ontvanger en handtekening zijn verplicht."), 400
    conn = db()
    p = conn.execute("SELECT * FROM planning WHERE id=?", (pid,)).fetchone()
    if p:
        conn.execute("UPDATE planning SET status='afgerond' WHERE id=?", (pid,))
        conn.execute("UPDATE orders SET status='afgerond', fulfilled=1, fulfilled_at=? WHERE id=?",
                     (datetime.now().isoformat(timespec="minutes"), p["order_id"]))
        conn.execute("""INSERT INTO deliveries(order_id,monteur_id,receiver,signature,outcome,sub_outcome,ts)
                        VALUES(?,?,?,?,?,?,?)""",
                     (p["order_id"], p["monteur_id"], receiver, signature, outcome, sub,
                      datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/monteur/start/<int:pid>", methods=["POST"])
def monteur_start(pid):
    if not has_perm("monteur_app"):
        return jsonify(ok=False), 403
    u = current_user()
    conn = db()
    p = conn.execute("SELECT * FROM planning WHERE id=? AND monteur_id=?", (pid, u["monteur_id"])).fetchone()
    if p:
        conn.execute("UPDATE planning SET status='onderweg' WHERE id=?", (pid,))
        conn.execute("UPDATE orders SET status='onderweg' WHERE id=?", (p["order_id"],))
        conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/monteur/close-route", methods=["POST"])
def close_route():
    u = current_user()
    if not u or not u["monteur_id"]:
        return jsonify(ok=False), 403
    conn = db()
    conn.execute("""INSERT INTO route_closed(monteur_id,date,ts) VALUES(?,?,?)
                    ON CONFLICT(monteur_id,date) DO UPDATE SET ts=excluded.ts""",
                 (u["monteur_id"], _today_iso(), datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/location", methods=["POST"])
def api_location():
    u = current_user()
    if not u or not u["monteur_id"]:
        return jsonify(ok=False), 403
    data = request.get_json(force=True)
    conn = db()
    conn.execute("""INSERT INTO monteur_location(monteur_id,lat,lng,updated_at,live) VALUES(?,?,?,?,?)
                    ON CONFLICT(monteur_id) DO UPDATE SET lat=excluded.lat,lng=excluded.lng,
                    updated_at=excluded.updated_at,live=excluded.live""",
                 (u["monteur_id"], float(data["lat"]), float(data["lng"]),
                  datetime.now().isoformat(timespec="minutes"), 1 if data.get("live", True) else 0))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/leave-request", methods=["POST"])
def api_leave_request():
    u = current_user()
    if not u:
        return jsonify(ok=False), 403
    f = request.form
    cat = f.get("category", "verlof")
    conn = db()
    conn.execute("""INSERT INTO leave_requests(user_id,user_name,is_monteur,monteur_id,category,leave_type,
                    date_from,date_to,time_from,time_to,reason,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
                 (u["id"], u["name"], 1 if u["role"] == "monteur" else 0, u["monteur_id"],
                  cat, (f.get("leave_type") if cat == "verlof" else "afspraak"),
                  f.get("date_from"), f.get("date_to") or f.get("date_from"),
                  f.get("time_from"), f.get("time_to"), f.get("reason", ""),
                  datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/leave-seen", methods=["POST"])
def api_leave_seen():
    u = current_user()
    if not u:
        return jsonify(ok=False), 403
    conn = db()
    conn.execute("UPDATE leave_requests SET decided_seen=1 WHERE user_id=? AND status!='open'", (u["id"],))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/pakbon/<int:oid>")
def pakbon(oid):
    if not current_user():
        abort(403)
    conn = db()
    o = conn.execute("SELECT pakbon FROM orders WHERE id=?", (oid,)).fetchone()
    conn.close()
    if not o or not o["pakbon"]:
        abort(404)
    return send_from_directory(UPLOAD_DIR, o["pakbon"])


# --------------------------------------------------------------------------- #
#  PWA
# --------------------------------------------------------------------------- #
@bp.route("/manifest.webmanifest")
def manifest():
    icon = url_for("static", filename="oi-icon.svg")
    data = {"name": "OfficeRoute — Monteur", "short_name": "OfficeRoute",
            "start_url": "/monteur", "scope": "/", "display": "standalone",
            "background_color": "#0f3d3e", "theme_color": "#0f3d3e",
            "icons": [{"src": icon, "sizes": "192x192", "type": "image/svg+xml", "purpose": "any"},
                      {"src": icon, "sizes": "512x512", "type": "image/svg+xml", "purpose": "any maskable"}]}
    return Response(json.dumps(data), mimetype="application/manifest+json")


@bp.route("/__diag")
def _diag():
    """Tijdelijke diagnose: met welke database praat deze service en bestaat Tom daar?
    Toont GEEN wachtwoorden. Verwijderen zodra het inloggen werkt."""
    info = {"is_pg": IS_PG}
    host = ""
    try:
        if _PG_URL:
            m = _re.search(r'@([^/]+)/([^?]+)', _PG_URL)
            if m:
                host = m.group(1) + "/" + m.group(2)
    except Exception:
        pass
    info["db"] = host or ("sqlite:" + DB_PATH)
    try:
        conn = db()
        info["users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        info["monteurs"] = conn.execute("SELECT COUNT(*) FROM monteurs").fetchone()[0]
        info["planning"] = conn.execute("SELECT COUNT(*) FROM planning").fetchone()[0]
        r = conn.execute("SELECT id,name,role,active,monteur_id FROM users WHERE lower(email)=?",
                         ("tom@office-interior.nl",)).fetchone()
        info["tom"] = ({"id": r["id"], "name": r["name"], "role": r["role"],
                        "active": r["active"], "monteur_id": r["monteur_id"]} if r else None)
        info["emails"] = [row["email"] for row in
                          conn.execute("SELECT email FROM users ORDER BY id LIMIT 12").fetchall()]
        conn.close()
    except Exception as e:
        info["error"] = str(e)
    return jsonify(info)


@bp.route("/sw.js")
def service_worker():
    js = ("const C='officeroute-app-v1';"
          "self.addEventListener('install',e=>self.skipWaiting());"
          "self.addEventListener('activate',e=>self.clients.claim());"
          "self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET')return;"
          "e.respondWith(fetch(r).then(res=>{const cp=res.clone();caches.open(C).then(c=>c.put(r,cp));return res;})"
          ".catch(()=>caches.match(r)));});")
    return Response(js, mimetype="application/javascript", headers={"Service-Worker-Allowed": "/"})


init_db()
