"""
OfficeRoute - monteur-app (zelfstandige service).

Aparte repo/Render-service die via dezelfde PostgreSQL-database samenwerkt met de
kantoorsoftware (planning). Bevat alleen de monteur-functionaliteit. De database wordt
beheerd door de kantoorsoftware; deze app leest/schrijft de gedeelde tabellen.

DB: PostgreSQL als DATABASE_URL is gezet (productie), anders lokaal SQLite (ontwikkeling).
"""

from flask import (Blueprint, render_template, request, redirect, url_for, session,
                   flash, jsonify, Response, abort, send_from_directory, g)
from werkzeug.security import check_password_hash, generate_password_hash
import os, json, time, sqlite3, secrets, smtplib, threading, math, base64, hmac, hashlib, urllib.parse

# Licht/snel wachtwoord-hashen (Werkzeug-default 'scrypt' is traag op kleine servers).
_PW_METHOD = "pbkdf2:sha256:30000"


def _hash_pw(pw):
    return generate_password_hash(pw, method=_PW_METHOD)
import re as _re
import urllib.request, urllib.error
from email.message import EmailMessage
from datetime import datetime, timedelta

# Endpoint-namespace 'planning' aangehouden zodat de gedeelde templates (url_for('planning.*')) werken.
bp = Blueprint("planning", __name__, url_prefix="", template_folder="templates")

BRAND = "OfficeRoute"
HOME_BASE = "Breda"
# Versie verandert automatisch bij elke Render-deploy (commit-SHA) → verplicht bijwerken.
APP_VERSION = (os.environ.get("RENDER_GIT_COMMIT") or "dev")[:12]
ALERT_THRESHOLD = 20
ROLE_LABELS = {"beheerder": "Beheerder", "manager": "Manager", "planner": "Planner",
               "administratie": "Administratie", "monteur": "Monteur"}
PERMISSION_KEYS = ["view_planning", "edit_planning", "monteur_app", "complete_deliveries"]

# Wagenpark - kentekens van onze vloot (buskeuze toont "Bus N" + kenteken, geen merk/chauffeur)
# label leeg = voertuig wordt op zijn kenteken getoond; alleen de bakwagen
# heeft een eigen naam ("Bakwagen") met de kentekengegevens erbij.
FLEET = [
    {"id": "bus1", "label": "", "plate": "V-16-FGH"},
    {"id": "bus2", "label": "", "plate": "VLT-21-N"},
    {"id": "bus3", "label": "", "plate": "VVB-14-T"},
    {"id": "bus4", "label": "", "plate": "VSN-02-X"},
    {"id": "bus5", "label": "", "plate": "VTZ-73-G"},
    {"id": "bus6", "label": "", "plate": "VVL-09-B"},
    {"id": "bus7", "label": "", "plate": "V-95-DVF"},
    {"id": "bus8", "label": "", "plate": "VTZ-77-G"},
    {"id": "bakwagen", "label": "Bakwagen", "plate": "VLD-03-F"},
]
FLEET_BY_ID = {v["id"]: v for v in FLEET}

# Kantoorcollega's voor het Contact-tabblad (1-klik bellen via eigen provider)
OFFICE_CONTACTS = [
    {"name": "Yelith", "phone": "+31 6 82048377"},
    {"name": "Thom", "phone": "+31 6 83257859"},
    {"name": "Chris", "phone": "+31 6 44544713"},
    {"name": "Jorik", "phone": "+31 6 10700901"},
]

# Bus-issues gaan naar kantoor (Jorik & Stijn)
BUS_ISSUE_RECIPIENTS = ["jorik@office-interior.nl", "stijn@office-interior.nl"]


# Regio-bepaling op basis van de plaats van de stops
REGION_BY_CITY = {
    "breda": "Noord-Brabant", "tilburg": "Noord-Brabant", "eindhoven": "Noord-Brabant",
    "den bosch": "Noord-Brabant", "'s-hertogenbosch": "Noord-Brabant", "helmond": "Noord-Brabant",
    "rotterdam": "Zuid-Holland", "den haag": "Zuid-Holland", "delft": "Zuid-Holland",
    "leiden": "Zuid-Holland", "dordrecht": "Zuid-Holland", "gouda": "Zuid-Holland",
    "amsterdam": "Noord-Holland", "haarlem": "Noord-Holland", "alkmaar": "Noord-Holland",
    "utrecht": "Utrecht", "amersfoort": "Utrecht", "nijmegen": "Gelderland", "arnhem": "Gelderland",
    "antwerpen": "België", "gent": "België", "brussel": "België",
}


def _region_for(jobs):
    counts = {}
    for j in jobs:
        city = (j["city"] or "").strip().lower()
        reg = REGION_BY_CITY.get(city)
        if reg:
            counts[reg] = counts.get(reg, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)

DB_PATH = os.environ.get("PLANNING_OI_DB_PATH", "monteur.db")
UPLOAD_DIR = os.environ.get("PLANNING_OI_UPLOADS", "oi_uploads")
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
except Exception:
    pass

# --------------------------------------------------------------------------- #
#  Database-laag (gelijk aan de kantoorsoftware) - SQLite of PostgreSQL
# --------------------------------------------------------------------------- #
_PG_URL = os.environ.get("DATABASE_URL", "")
if _PG_URL.startswith("postgres://"):
    _PG_URL = _PG_URL.replace("postgres://", "postgresql://", 1)
IS_PG = bool(_PG_URL)

# Connection pool voor PostgreSQL (valt veilig terug op directe verbindingen als
# psycopg_pool ontbreekt).
_PG_POOL = None
_PG_POOL_TRIED = False


def _get_pg_pool():
    global _PG_POOL, _PG_POOL_TRIED
    if _PG_POOL is not None or _PG_POOL_TRIED:
        return _PG_POOL
    _PG_POOL_TRIED = True
    p = None
    try:
        from psycopg_pool import ConnectionPool
        # open=False + valideren met korte timeout: check NU of de pool echt verbindt.
        # Zo niet -> pool weggooien en directe verbindingen gebruiken. Voorkomt dat elke
        # request de volle pool-timeout afwacht op een kapotte pool (verse Render-build).
        p = ConnectionPool(_PG_URL, min_size=1, max_size=5,
                           kwargs={"autocommit": True}, timeout=5, open=False,
                           check=ConnectionPool.check_connection, max_idle=300, max_lifetime=1800)
        p.open()
        _c = p.getconn(timeout=3)
        p.putconn(_c)
        _PG_POOL = p
    except Exception:
        try:
            if p is not None:
                p.close()
        except Exception:
            pass
        _PG_POOL = None
    return _PG_POOL


_NO_ID_TABLES = {"monteur_location", "route_closed", "integrations", "settings", "monteur_day_gps"}

# GPS-throttle + home-coordinaten-cache (per proces): voorkomt onnodige DB-writes en
# een home-query bij elke live positie-push.
_LOC_LAST = {}
_HOME_CACHE = {}
_LOC_MIN_INTERVAL = 12


def _sub_placeholders(sql):
    """Vervang '?'-parameters door '%s', maar laat vraagtekens BINNEN
    string-literals ('…') met rust (anders telt psycopg te veel placeholders)."""
    out = []
    in_str = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            if in_str and i + 1 < n and sql[i + 1] == "'":
                out.append("'"); i += 2; continue
            in_str = not in_str
            i += 1; continue
        out.append("%s" if (ch == "?" and not in_str) else ch)
        i += 1
    return "".join(out)


def _xlate(sql):
    is_ignore = "INSERT OR IGNORE" in sql.upper()
    s = _re.sub(r'INSERT\s+OR\s+IGNORE\s+INTO', 'INSERT INTO', sql, flags=_re.I)
    s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    s = s.replace("qty || 'x ' || name", "qty::text || 'x ' || name")
    s = _re.sub(r'GROUP_CONCAT\s*\(', 'string_agg(', s, flags=_re.I)  # SQLite -> PostgreSQL
    s = _sub_placeholders(s)
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


_PG_POOL_FAILS = 0


class _PgConn:
    def __init__(self):
        global _PG_POOL, _PG_POOL_FAILS
        self._pool = _get_pg_pool()
        if self._pool is not None:
            try:
                self._raw = self._pool.getconn()
                _PG_POOL_FAILS = 0
            except Exception:
                # Pool even vol: deze keer een directe verbinding. Eerder werd de pool
                # dan meteen voor iedereen gesloten; nu pas na drie missers op rij (dan
                # is hij echt kapot en willen we niet steeds de wachttijd betalen).
                _PG_POOL_FAILS += 1
                if _PG_POOL_FAILS >= 3:
                    try:
                        self._pool.close()
                    except Exception:
                        pass
                    _PG_POOL = None
                self._pool = None
        if self._pool is None:
            import psycopg
            self._raw = psycopg.connect(_PG_URL, autocommit=True)
        self._lastid = None
        self._closed = False
        # Onthouden binnen het verzoek: loopt een pagina vast voordat hij close()
        # aanroept, dan geeft _release_request_conns de verbinding toch terug.
        # Zonder dat liep de pool na een paar fouten leeg (en volgden er meer 500's).
        try:
            from flask import has_request_context, g as _g
            if has_request_context():
                _g.setdefault("_oi_conns", []).append(self)
        except Exception:
            pass
    def cursor(self):
        return _PgCur(self)
    def execute(self, sql, params=()):
        return _PgCur(self).execute(sql, params)
    def executescript(self, script):
        return _PgCur(self).executescript(script)
    def commit(self):
        pass
    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if self._pool is not None:
                self._pool.putconn(self._raw)
            else:
                self._raw.close()
        except Exception:
            pass


def _release_request_conns(exc=None):
    """Na elk verzoek: verbindingen die niet netjes zijn gesloten alsnog teruggeven."""
    try:
        from flask import g as _g
        for c in (_g.pop("_oi_conns", None) or []):
            try:
                c.close()
            except Exception:
                pass
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
#  Schema garanderen (geen seed - de kantoorsoftware beheert de data).
#  Lokaal (SQLite) een mini dev-seed zodat je de app kunt testen.
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE,
  password TEXT, role TEXT, permissions TEXT, phone TEXT, monteur_id INTEGER, active INTEGER DEFAULT 1,
  created_at TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS monteurs(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, phone TEXT, email TEXT,
  speed INTEGER DEFAULT 3, color TEXT, bus_id INTEGER, home_address TEXT, home_lat REAL, home_lng REAL,
  standard INTEGER DEFAULT 1, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS busses(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, plate TEXT, driver TEXT,
  max_volume REAL, max_weight REAL, max_stops INTEGER, apk_date TEXT, maintenance TEXT);
CREATE TABLE IF NOT EXISTS clients(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT, phone TEXT,
  address TEXT, postal TEXT, city TEXT, invoice_address TEXT, notes TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, order_number TEXT, client_id INTEGER,
  source TEXT, is_draft INTEGER DEFAULT 0, status TEXT, delivery_address TEXT, city TEXT, postal TEXT,
  invoice_address TEXT, phone TEXT, email TEXT, desired_date TEXT, notes TEXT, instructions TEXT, customer_note TEXT,
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
CREATE TABLE IF NOT EXISTS bus_choices(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, user_email TEXT,
  user_name TEXT, bus_id TEXT, bus_label TEXT, plate TEXT, date TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS bus_issues(id INTEGER PRIMARY KEY AUTOINCREMENT, monteur_id INTEGER, monteur_name TEXT,
  reporter_email TEXT, bus_label TEXT, plate TEXT, message TEXT, status TEXT DEFAULT 'open',
  created_at TEXT, resolved_by TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS email_log(id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER, direction TEXT,
  subject TEXT, body TEXT, ts TEXT, has_attachment INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS work_hours(id INTEGER PRIMARY KEY AUTOINCREMENT, monteur_id INTEGER, user_id INTEGER,
  user_email TEXT, user_name TEXT, work_date TEXT, start_time TEXT, end_time TEXT,
  worked_min INTEGER DEFAULT 0, overtime_min INTEGER DEFAULT 0, note TEXT, submitted_at TEXT,
  UNIQUE(monteur_id, work_date));
CREATE TABLE IF NOT EXISTS monteur_day_gps(monteur_id INTEGER, date TEXT, home_since TEXT,
  PRIMARY KEY(monteur_id, date));
CREATE TABLE IF NOT EXISTS monteur_notices(id INTEGER PRIMARY KEY AUTOINCREMENT, monteur_id INTEGER, date TEXT,
  kind TEXT, text TEXT, detail TEXT, created_by TEXT, created_at TEXT, seen INTEGER DEFAULT 0);
"""

UREN_TERUG_DAGEN = 31     # zo ver terug mag een monteur vergeten uren nog zelf doorgeven


def _uren_min_date(today):
    return (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=UREN_TERUG_DAGEN)).date().isoformat()


def init_db():
    conn = db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE orders ADD COLUMN customer_note TEXT")
        conn.commit()
    except Exception:
        pass
    # Kolommen voor "planning die leert" (gemeten montageduur) + login-blokkade.
    # De gedeelde tabel heeft ze al via de kantoorsoftware; hier veiligheidshalve ook.
    for _stmt in ("ALTER TABLE planning ADD COLUMN onderweg_at TEXT",
                  "ALTER TABLE planning ADD COLUMN aangekomen_at TEXT",
                  "ALTER TABLE planning ADD COLUMN afgerond_at TEXT",
                  "ALTER TABLE users ADD COLUMN login_fails INTEGER DEFAULT 0",
                  "ALTER TABLE users ADD COLUMN locked INTEGER DEFAULT 0",
                  "ALTER TABLE users ADD COLUMN totp_secret TEXT",
                  # De kantoorsoftware zet hem, maar de volg-link in de 'ik kom eraan'-mail
                  # heeft hem nodig; zo werkt de monteur-app ook op een verse database.
                  "ALTER TABLE orders ADD COLUMN track_token TEXT"):
        try:
            conn.execute(_stmt); conn.commit()
        except Exception:
            pass
    # mini dev-seed alleen lokaal (SQLite) zodat de app testbaar is
    if not IS_PG:
        try:
            if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                today = datetime.now().date().isoformat()
                for b in [("Bus 1 - Mercedes Sprinter", "VND-12-A", "Rick"),
                          ("Bus 2 - VW Crafter", "8-XGT-99", "Sven"),
                          ("Bus 3 - Ford Transit", "GV-880-K", "Youssef")]:
                    conn.execute("INSERT INTO busses(name,plate,driver) VALUES(?,?,?)", b)
                conn.execute("INSERT INTO monteurs(name,phone,color,home_address,home_lat,home_lng) VALUES(?,?,?,?,?,?)",
                             ("Tom", "06-21110011", "#0f3d3e", "Ginnekenweg 200, Breda", 51.57, 4.78))
                conn.execute("""INSERT INTO users(name,email,password,role,permissions,monteur_id,active,created_at)
                                VALUES(?,?,?,?,?,?,1,?)""",
                             ("Tom", "tom@office-interior.nl", _hash_pw("PlanningOI2025!"),
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
    # Cache per request (Flask g): scheelt herhaalde users-queries per pagina/API-call.
    if getattr(g, "_cur_user_set", False):
        return g._cur_user
    uid = session.get("p_user_id")
    u = None
    if uid:
        conn = db()
        u = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
        conn.close()
    g._cur_user = u
    g._cur_user_set = True
    return u


# Gelijk houden met is_leave_approver in de kantoor-app: beheerders, het recht
# 'Verlofaanvragen goedkeuren', en als vangnet de vaste adressen van vroeger.
_LEGACY_APPROVERS = {"caspar@office-interior.nl", "aleks@office-interior.nl",
                     "stijn@office-interior.nl", "jorik@office-interior.nl"}


def _my_leave_overview(conn, uid):
    """Verlofoverzicht voor in de app, kort gehouden: bovenaan wat nu telt (in
    behandeling, en goedgekeurd verlof dat nog komt; op datum), daaronder dichtgeklapt
    de eerdere aanvragen (voorbij of afgewezen, hooguit de laatste 10)."""
    rows = conn.execute("""SELECT id, category, leave_type, date_from, date_to, time_from, time_to, status,
                                  decision_reason, decided_by FROM leave_requests
                           WHERE user_id=? ORDER BY created_at DESC LIMIT 60""", (uid,)).fetchall()
    today = _today_iso()
    now_ids = {r["id"] for r in rows if r["status"] == "open" or (
        r["status"] == "goedgekeurd" and ((r["date_to"] or r["date_from"] or "") >= today))}
    now = sorted([r for r in rows if r["id"] in now_ids], key=lambda r: r["date_from"] or "")
    past = [r for r in rows if r["id"] not in now_ids][:10]
    return {"now": now, "past": past}


def _leave_approver_names(conn):
    names = []
    for r in conn.execute("""SELECT name, email, role, permissions FROM users WHERE active=1
                             AND role NOT IN ('monteur','picker') ORDER BY name""").fetchall():
        try:
            perms = set(json.loads(r["permissions"] or "[]"))
        except Exception:
            perms = set()
        if r["role"] == "beheerder" or "approve_leave" in perms or (r["email"] or "").lower() in _LEGACY_APPROVERS:
            names.append(r["name"])
    if not names:
        return "kantoor"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " en " + names[-1]


# --------------------------------------------------------------------------- #
#  Single sign-on vanuit de OfficeHub: de hub-knop stuurt een kort geldig token
#  mee (?sso=...). Klopt het (zelfde SSO_SECRET als de hub), dan ben je meteen
#  ingelogd, zonder wachtwoord en 2FA. Gelijk houden met de kantoor-app.
#  Zonder SSO_SECRET doet dit niets en zie je gewoon het inlogscherm.
# --------------------------------------------------------------------------- #
SSO_SECRET = os.environ.get("SSO_SECRET") or ""


def _sso_verify(tok):
    if not SSO_SECRET:
        return None
    try:
        uid, exp, sig = tok.split(".")
    except Exception:
        return None
    good = hmac.new(SSO_SECRET.encode(), (uid + "." + exp).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    try:
        if int(exp) < int(time.time()):
            return None
    except Exception:
        return None
    return uid


@bp.before_app_request
def _sso_login():
    tok = request.args.get("sso")
    if not tok:
        return None
    uid = _sso_verify(tok)
    if uid:
        try:
            conn = db()
            row = conn.execute("SELECT id FROM users WHERE id=? AND active=1", (int(uid),)).fetchone()
            conn.close()
        except Exception:
            row = None
        if row:
            session.permanent = True
            session["p_user_id"] = int(uid)
            session.pop("twofa", None)
    args = request.args.to_dict(flat=True)
    args.pop("sso", None)
    q = urllib.parse.urlencode(args)
    return redirect(request.path + (("?" + q) if q else ""))


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
    """Hier stond een demo-file (A2, N65) die altijd bij monteur 1 verscheen. Echte
    file-informatie zit in Google Maps en Waze zelf; de kantoor-app toont de echte
    vertraging op de route. Dus hier geen verzonnen meldingen meer."""
    return []


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


def _nl_hour():
    """Huidig uur in Nederlandse tijd (Render draait in UTC). Valt terug op servertijd."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Amsterdam")).hour
    except Exception:
        return datetime.now().hour


HOME_RADIUS_M = 250   # binnen deze straal van huis = "thuis"


def _dist_m(lat1, lng1, lat2, lng2):
    """Afstand in meters tussen twee coördinaten (haversine)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _parse_hm(s):
    """'HH:MM' -> minuten sinds middernacht, of None."""
    try:
        h, m = (s or "").split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        pass
    return None


def _calc_hours(start, end):
    """Gewerkte en overuren-minuten uit start/eind. Pauzes worden NIET afgetrokken.
    Alles boven 8 uur (480 min) telt als overuren. Retourneert (worked, overtime) of None."""
    a, b = _parse_hm(start), _parse_hm(end)
    if a is None or b is None or b <= a:
        return None
    worked = b - a
    overtime = max(0, worked - 480)
    return worked, overtime


@bp.app_context_processor
def _inject():
    u = current_user()
    return {"p_user": u, "p_has_perm": has_perm, "p_perms": user_perms(u),
            "ROLE_LABELS": ROLE_LABELS, "HOME_BASE": HOME_BASE, "BRAND": BRAND,
            "p_app_version": APP_VERSION,
            "p_leave_decision": my_unseen_decision(u) if u else None}


def login_required(perm=None):
    u = current_user()
    if not u:
        return redirect(url_for("planning.login", next=request.path))
    if perm and perm not in user_perms(u):
        abort(403)
    return None


def _email_cfg():
    conn = db()
    cfg = {r["field"]: r["value"] for r in
           conn.execute("SELECT field,value FROM integrations WHERE ikey=?", ("email",)).fetchall()}
    conn.close()
    return cfg


def _email_configured():
    c = _email_cfg()
    from_email = (c.get("from_email") or c.get("smtp_user") or "").strip()
    has_resend = bool((c.get("resend_api_key") or "").strip() and from_email)
    has_smtp = bool((c.get("smtp_host") or "").strip() and (c.get("smtp_user") or "").strip()
                    and (c.get("smtp_pass") or "").strip())
    return has_resend or has_smtp


def _mail_live():
    """True als mail echt verstuurd wordt (ingesteld + testmodus uit) - zonder netwerkcall."""
    return _email_configured() and (_email_cfg().get("send_live") or "0") == "1"


def _api_send(to, subject, text, html=None):
    """Verstuur via Resend HTTPS-API (werkt op Render); anders SMTP (lokaal). Respecteert testmodus.
    Leest de gedeelde 'email'-integratie (beheerd in de kantoor-app)."""
    recips = [r for r in (to if isinstance(to, list) else [to]) if r]
    if not recips:
        return False
    c = _email_cfg()
    if (c.get("send_live") or "0") != "1":
        return False   # testmodus
    from_email = (c.get("from_email") or c.get("smtp_user") or "").strip()
    if not from_email:
        return False
    frm = "%s <%s>" % ((c.get("from_name") or "Office-Interior").strip(), from_email)
    reply_to = (c.get("reply_to") or "").strip()
    key = (c.get("resend_api_key") or "").strip()
    if key:
        try:
            body = {"from": frm, "to": recips, "subject": subject, "text": text, "html": html or text}
            if reply_to:
                body["reply_to"] = reply_to
            payload = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                "https://api.resend.com/emails", data=payload,
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json",
                         "Accept": "application/json",
                         # De User-Agent is NIET optioneel: zonder eigen user-agent
                         # vertrekt dit als "Python-urllib/3.x" en blokkeert de
                         # Cloudflare-laag voor api.resend.com het verzoek met
                         # HTTP 403 "error code: 1010" (browser signature banned).
                         "User-Agent": "OfficeRoute-Monteur/1.0 (+https://office-interior.nl)"})
            urllib.request.urlopen(req, timeout=10).read()
            return True
        except Exception:
            return False
    host = (c.get("smtp_host") or "").strip()
    user = (c.get("smtp_user") or "").strip()
    pwd = (c.get("smtp_pass") or "").strip()
    if not (host and user and pwd):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = ", ".join(recips)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(host, int(c.get("smtp_port") or 587), timeout=10) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return True
    except Exception:
        return False


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _twofa_email_html(name, code, subtitle="Monteur-app"):
    html = """<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#e7ebe7;padding:24px 0;margin:0;">
<tr><td align="center">
<table role="presentation" width="460" cellpadding="0" cellspacing="0" style="max-width:460px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e6ebe4;font-family:Arial,Helvetica,sans-serif;">
<tr><td style="background:#0f3d3e;padding:22px 24px;">
<span style="color:#ffffff;font-size:18px;font-weight:bold;letter-spacing:2px;">OfficeRoute</span>
<span style="color:#cda35a;font-size:12px;"> &middot; __SUB__</span></td></tr>
<tr><td style="padding:26px 24px 6px;">
<p style="margin:0 0 4px;font-size:16px;color:#16302d;">Hoi __NAME__,</p>
<p style="margin:0 0 20px;font-size:15px;color:#5f6b64;line-height:1.6;">Hier is je inlogcode voor OfficeRoute. Vul 'm in op het inlogscherm om verder te gaan.</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef3ec;border:1px solid #cda35a;border-radius:12px;margin-bottom:16px;">
<tr><td align="center" style="padding:18px;">
<div style="font-size:12px;color:#5f6b64;margin-bottom:8px;">Je verificatiecode</div>
<div style="font-size:38px;font-weight:bold;color:#0f3d3e;letter-spacing:8px;">__CODE__</div></td></tr></table>
<p style="margin:0 0 18px;font-size:13px;color:#5f6b64;">Deze code is 5 minuten geldig.</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f7efe0;border:1px solid #e3c98f;border-radius:12px;">
<tr><td style="padding:14px 15px;font-size:13px;color:#6b4e15;line-height:1.55;">
<b>Niet zelf ingelogd?</b> Heb je g&eacute;&eacute;n inlogpoging gedaan, neem dan <b>direct contact op met de beheerder</b>. Deel deze code met niemand.</td></tr></table>
</td></tr>
<tr><td style="padding:18px 24px 22px;border-top:1px solid #e6ebe4;">
<p style="margin:0;font-size:11px;color:#97a39d;line-height:1.6;">Deze e-mail is automatisch verstuurd door OfficeRoute &middot; Office-Interior.<br>Antwoorden op dit bericht worden niet gelezen.</p></td></tr>
</table></td></tr></table>"""
    return html.replace("__SUB__", _esc(subtitle)).replace("__NAME__", _esc(name) or "collega").replace("__CODE__", _esc(code))


def _send_2fa_email(to_email, code, name=""):
    """Mail de 6-cijferige inlogcode (HTML + tekst). True bij succes, anders False (val terug op scherm)."""
    text = ("Hoi %s,\n\nJe verificatiecode voor OfficeRoute is: %s\n\n"
            "De code is 5 minuten geldig.\n\n"
            "Niet zelf ingelogd? Neem dan direct contact op met de beheerder en deel "
            "deze code met niemand.\n" % (name or "collega", code))
    return _api_send(to_email, "Je OfficeRoute-inlogcode: %s" % code, text, _twofa_email_html(name, code))


# --------------------------------------------------------------------------- #
#  Auth-routes (met 2FA)
# --------------------------------------------------------------------------- #
@bp.route("/")
def home():
    return redirect(url_for("planning.monteur_app") if current_user() else url_for("planning.login"))


def _totp_secret_new():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_at(secret, ts):
    try:
        key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    except Exception:
        return None
    ctr = int(ts // 30)
    h = hmac.new(key, ctr.to_bytes(8, "big"), hashlib.sha1).digest()
    o = h[-1] & 0x0f
    return "%06d" % ((int.from_bytes(h[o:o + 4], "big") & 0x7fffffff) % 1000000)


def _totp_verify(secret, code):
    code = (code or "").strip().replace(" ", "")
    if not (secret and code.isdigit() and len(code) == 6):
        return False
    now = time.time()
    # +/- 2 stappen (150 s) tolerantie: vergevingsgezind voor kleine klokverschillen.
    return any(_totp_at(secret, now + w * 30) == code for w in (-2, -1, 0, 1, 2))


def _totp_uri(secret, email):
    label = urllib.parse.quote("OfficeRoute:" + (email or ""))
    return "otpauth://totp/%s?secret=%s&issuer=OfficeRoute&digits=6&period=30" % (label, secret)

def _totp_qr_svg(uri):
    """Inline SVG-QR van de otpauth-URI, zodat je kunt scannen i.p.v. overtypen."""
    if not uri:
        return ""
    try:
        import io
        import segno
        buff = io.BytesIO()
        segno.make(uri, error="m").save(buff, kind="svg", scale=4, border=2,
                                        dark="#0f3d3e", xmldecl=False, svgns=True)
        return buff.getvalue().decode("ascii")
    except Exception:
        return ""


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""; show_2fa = False; enrolling = False; totp_secret = None; otp_uri = None; twofa_email = None
    if request.method == "POST":
        if request.form.get("twofa_code") is not None:
            tf = session.get("twofa") or {}
            code = (request.form.get("twofa_code") or "").strip()
            if not tf:
                error = "Sessie verlopen. Log opnieuw in."
            elif time.time() > tf.get("exp", 0):
                session.pop("twofa", None); error = "Sessie verlopen."
            elif _totp_verify(tf.get("secret"), code):
                if tf.get("enroll"):
                    try:
                        cu = db(); cu.execute("UPDATE users SET totp_secret=? WHERE id=?", (tf["secret"], tf["uid"])); cu.commit(); cu.close()
                    except Exception:
                        pass
                session["p_user_id"] = tf["uid"]; session.pop("twofa", None)
                return redirect(url_for("planning.monteur_app"))
            else:
                error = "Onjuiste code. Controleer je authenticator-app."
            if error and tf:
                show_2fa = True; twofa_email = tf.get("email")
                if tf.get("enroll"):
                    enrolling = True; totp_secret = tf.get("secret"); otp_uri = _totp_uri(tf.get("secret"), tf.get("email"))
        else:
            email = (request.form.get("email") or "").strip().lower()
            pw = request.form.get("password") or ""
            conn = db()
            u = conn.execute("SELECT * FROM users WHERE lower(email)=? AND active=1", (email,)).fetchone()
            conn.close()
            _keys = set(u.keys()) if u else set()
            _locked = int((u["locked"] if "locked" in _keys else 0) or 0) if u else 0
            if u and _locked == 1:
                error = ("Dit account is geblokkeerd na te veel mislukte inlogpogingen. "
                         "Vraag een beheerder om het account te ontgrendelen.")
            elif u and check_password_hash(u["password"], pw):
                # Juist wachtwoord: teller mislukte pogingen weer op nul.
                try:
                    cu = db(); cu.execute("UPDATE users SET login_fails=0 WHERE id=?", (u["id"],)); cu.commit(); cu.close()
                except Exception:
                    pass
                # Zware hash (scrypt/hoge telling) eenmalig omzetten naar de lichte methode.
                if not (u["password"] or "").startswith(_PW_METHOD):
                    try:
                        cu = db()
                        cu.execute("UPDATE users SET password=? WHERE id=?", (_hash_pw(pw), u["id"]))
                        cu.commit(); cu.close()
                    except Exception:
                        pass
                enrolled = bool(((u["totp_secret"] if "totp_secret" in _keys else None) or "").strip())
                secret = u["totp_secret"] if enrolled else _totp_secret_new()
                show_2fa = True; twofa_email = u["email"]
                if not enrolled:
                    enrolling = True; totp_secret = secret; otp_uri = _totp_uri(secret, u["email"])
                session["twofa"] = {"uid": u["id"], "secret": secret, "enroll": (not enrolled),
                                    "exp": time.time() + 600, "email": u["email"]}
            elif u:
                # Bestaand account, fout wachtwoord: teller ophogen en na 5 pogingen blokkeren.
                try:
                    fails = int((u["login_fails"] if "login_fails" in _keys else 0) or 0) + 1
                    cu = db()
                    if fails >= 5:
                        cu.execute("UPDATE users SET login_fails=?, locked=1 WHERE id=?", (fails, u["id"]))
                        error = ("Te veel mislukte pogingen. Dit account is nu geblokkeerd. "
                                 "Vraag een beheerder om het te ontgrendelen.")
                    else:
                        cu.execute("UPDATE users SET login_fails=? WHERE id=?", (fails, u["id"]))
                        error = ("Onjuiste inloggegevens. Nog %d poging(en) voordat het account "
                                 "wordt geblokkeerd." % (5 - fails))
                    cu.commit(); cu.close()
                except Exception:
                    error = "Onjuiste inloggegevens."
            else:
                error = "Onjuiste inloggegevens."
    return render_template("planning/login.html", error=error, show_2fa=show_2fa,
                           enrolling=enrolling, totp_secret=totp_secret, otp_uri=otp_uri,
                           qr_svg=_totp_qr_svg(otp_uri),
                           twofa_email=twofa_email)


@bp.route("/logout")
def logout():
    session.pop("p_user_id", None); session.pop("twofa", None)
    session.pop("bus_id", None); session.pop("bus_done", None); session.pop("view_as", None)
    return redirect(url_for("planning.login"))


# --------------------------------------------------------------------------- #
#  Monteur-app
# --------------------------------------------------------------------------- #
def _purge_old_customer_notes(conn):
    """Privacy: klantopmerkingen wissen 12 uur na de levering (fulfilled_at)."""
    try:
        cutoff = (datetime.now() - timedelta(hours=12)).isoformat(timespec="minutes")
        conn.execute("UPDATE orders SET customer_note=NULL WHERE customer_note IS NOT NULL "
                     "AND fulfilled=1 AND fulfilled_at IS NOT NULL AND fulfilled_at < ?", (cutoff,))
        conn.commit()
    except Exception:
        pass


def _current_bus():
    return FLEET_BY_ID.get(session.get("bus_id"))


def _record_bus_choice(u, item):
    """Leg vast (per login) in welke bus de monteur vandaag rijdt."""
    if not u:
        return
    try:
        conn = db()
        # Eén keuze per monteur per dag bewaren (voorkomt ongelimiteerde groei).
        conn.execute("DELETE FROM bus_choices WHERE user_id=? AND date=?", (u["id"], _today_iso()))
        conn.execute("""INSERT INTO bus_choices(user_id,user_email,user_name,bus_id,bus_label,plate,date,ts)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (u["id"], u["email"], u["name"], item["id"], item["label"], item["plate"],
                      _today_iso(), datetime.now().isoformat(timespec="seconds")))
        conn.commit(); conn.close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Meekijken als monteur: een beheerder (zonder eigen monteurskaart) kiest een
#  monteur en ziet diens app zoals hij hem ziet. Alleen kijken: elke POST wordt
#  aan de serverkant geweigerd, zodat er nooit per ongeluk iets namens de
#  monteur gebeurt (levering afronden, klant mailen, uren indienen).
# --------------------------------------------------------------------------- #
_VIEW_AS_ALLOWED = {"planning.meekijken", "planning.logout", "planning.login", "planning.api_leave_seen"}


def _can_view_as(u):
    return bool(u and u["role"] == "beheerder" and not u["monteur_id"])


def _view_as_id():
    if not _can_view_as(current_user()):
        return None
    try:
        return int(session.get("view_as") or 0) or None
    except (TypeError, ValueError):
        return None


@bp.before_app_request
def _view_as_readonly():
    if request.method != "POST" or request.endpoint in _VIEW_AS_ALLOWED or not session.get("view_as"):
        return None
    if _can_view_as(current_user()):
        return jsonify(ok=False, error="Je kijkt mee als monteur. Dit kan alleen de monteur zelf."), 403
    return None


@bp.route("/meekijken", methods=["GET", "POST"])
def meekijken():
    guard = login_required()
    if guard:
        return guard
    if not _can_view_as(current_user()):
        abort(403)
    if request.method == "POST":
        try:
            session["view_as"] = int(request.form.get("monteur_id") or 0) or None
        except ValueError:
            session["view_as"] = None
        if session.get("view_as"):
            return redirect(url_for("planning.monteur_app"))
    today = _today_iso()
    conn = db()
    monteurs = conn.execute("""SELECT m.id, m.name, m.color,
                                      (SELECT COUNT(*) FROM planning p WHERE p.monteur_id=m.id AND p.date=?) AS stops,
                                      (SELECT COUNT(*) FROM planning p WHERE p.monteur_id=m.id AND p.date=?
                                         AND p.status='afgerond') AS done
                               FROM monteurs m WHERE m.active=1 ORDER BY m.name""", (today, today)).fetchall()
    conn.close()
    monteurs = sorted(monteurs, key=lambda m: (0 if m["stops"] else 1, m["name"] or ""))   # met route bovenaan
    return render_template("planning/meekijken.html", monteurs=monteurs, current=session.get("view_as"))


@bp.route("/kies-bus", methods=["GET", "POST"])
def kies_bus():
    guard = login_required("monteur_app")
    if guard:
        return guard
    if request.method == "POST":
        if request.form.get("skip"):
            session["bus_done"] = True
            session.pop("bus_id", None)
            return redirect(url_for("planning.monteur_app"))
        item = FLEET_BY_ID.get(request.form.get("bus_id"))
        if item:
            session["bus_id"] = item["id"]
            session["bus_done"] = True
            _record_bus_choice(current_user(), item)
            return redirect(url_for("planning.monteur_app"))
    return render_template("planning/kies_bus.html", fleet=FLEET, current=session.get("bus_id"))


@bp.route("/monteur")
def monteur_app():
    guard = login_required("monteur_app")
    if guard:
        return guard
    u = current_user()
    viewing = None
    if _can_view_as(u):
        # Beheerder zonder eigen route: eerst kiezen bij wie je meekijkt.
        view_mid = _view_as_id()
        if not view_mid:
            return redirect(url_for("planning.meekijken"))
    elif not session.get("bus_done"):
        # Eerst een bus kiezen (of bewust overslaan) voordat het overzicht verschijnt
        return redirect(url_for("planning.kies_bus"))
    conn = db()
    _purge_old_customer_notes(conn)
    mid = u["monteur_id"]
    leave_uid, bus = u["id"], _current_bus()
    if _can_view_as(u):
        mid = view_mid
        vu = conn.execute("SELECT * FROM users WHERE monteur_id=? AND active=1 ORDER BY id LIMIT 1",
                          (mid,)).fetchone()
        leave_uid = vu["id"] if vu else 0
        bc = conn.execute("SELECT bus_label, plate FROM bus_choices WHERE user_id=? AND date=? ORDER BY id DESC LIMIT 1",
                          (leave_uid, _today_iso())).fetchone() if vu else None
        bus = {"label": bc["bus_label"], "plate": bc["plate"]} if bc else None
    today = _today_iso()
    jobs, monteur = [], None
    if mid:
        monteur = conn.execute("SELECT * FROM monteurs WHERE id=?", (mid,)).fetchone()
        jobs = conn.execute("""SELECT p.*, o.id AS oid, o.order_number, o.delivery_address, o.city, o.phone, o.instructions,
                               o.montage_min, o.service_type, o.pakbon, o.customer_note, c.name AS client,
                               (SELECT GROUP_CONCAT(qty || 'x ' || name, ', ') FROM order_items WHERE order_id=o.id) AS items
                               FROM planning p JOIN orders o ON o.id=p.order_id
                               LEFT JOIN clients c ON c.id=o.client_id
                               WHERE p.monteur_id=? AND p.date=? ORDER BY p.sequence""", (mid, today)).fetchall()
    closed = bool(mid and conn.execute("SELECT 1 FROM route_closed WHERE monteur_id=? AND date=?",
                                       (mid, today)).fetchone())
    # Urenregistratie: entry van vandaag + maandoverzicht (live)
    hours_today, hours_month, month_total, month_ot = None, [], 0, 0
    if mid:
        hours_today = conn.execute("SELECT * FROM work_hours WHERE monteur_id=? AND work_date=?",
                                   (mid, today)).fetchone()
        hours_month = conn.execute("SELECT * FROM work_hours WHERE monteur_id=? AND work_date LIKE ? "
                                   "ORDER BY work_date DESC", (mid, today[:7] + "%")).fetchall()
        month_total = sum((r["worked_min"] or 0) for r in hours_month)
        month_ot = sum((r["overtime_min"] or 0) for r in hours_month)
    # Uren van de afgelopen maand, zodat de monteur een vergeten dag kan invullen of aanpassen.
    uren_min = _uren_min_date(today)
    hours_recent = {}
    if mid:
        for r in conn.execute("""SELECT work_date, start_time, end_time, note, submitted_at FROM work_hours
                                 WHERE monteur_id=? AND work_date>=?""", (mid, uren_min)).fetchall():
            hours_recent[r["work_date"]] = {"start": r["start_time"] or "", "end": r["end_time"] or "",
                                            "note": r["note"] or "", "at": r["submitted_at"] or ""}
    # Laatste routewijziging door kantoor van vandaag (melding bovenin + in Meldingen).
    last_notice = None
    if mid:
        try:
            last_notice = conn.execute("""SELECT * FROM monteur_notices WHERE monteur_id=? AND date=? AND kind='route'
                                          ORDER BY id DESC LIMIT 1""", (mid, today)).fetchone()
        except Exception:
            last_notice = None
    # Eigen verlofaanvragen met hun status, zodat de monteur ziet wat hij heeft aangevraagd.
    my_leave = _my_leave_overview(conn, leave_uid)
    leave_approvers = _leave_approver_names(conn)
    conn.close()
    # Herinnering: na 20:00 en vandaag nog geen uren ingediend
    remind_hours = bool(mid and hours_today is None and _nl_hour() >= 20 and not _can_view_as(u))
    viewing = monteur if _can_view_as(u) else None
    alerts = route_alerts(mid, bool(jobs)) if mid else []
    all_done = bool(jobs) and all(j["status"] == "afgerond" for j in jobs)
    client_notes = [{"client": j["client"], "note": j["customer_note"]}
                    for j in jobs if (j["customer_note"] or "").strip()]
    return render_template("planning/monteur_app.html", monteur=monteur, jobs=jobs, alerts=alerts,
                           closed=closed, all_done=all_done, region=_region_for(jobs),
                           bus=bus, viewing=viewing, contacts=OFFICE_CONTACTS, client_notes=client_notes,
                           maps_ready=(integ_status("google_maps") == "verbonden"),
                           hours_today=hours_today, hours_month=hours_month,
                           month_total=month_total, month_ot=month_ot, remind_hours=remind_hours,
                           today_iso=today, my_leave=my_leave, leave_approvers=leave_approvers,
                           uren_min=uren_min, hours_recent=hours_recent, last_notice=last_notice)


@bp.route("/api/notices")
def api_notices():
    """Kleine check (elke minuut vanuit de app): is er een nieuwe routewijziging van kantoor?"""
    u = current_user()
    if not u or not u["monteur_id"]:
        return jsonify(ok=False), 403
    conn = db()
    try:
        r = conn.execute("""SELECT id FROM monteur_notices WHERE monteur_id=? AND date=? AND seen=0
                            ORDER BY id DESC LIMIT 1""", (u["monteur_id"], _today_iso())).fetchone()
    except Exception:
        r = None
    conn.close()
    return jsonify(ok=True, id=(r["id"] if r else 0))


@bp.route("/api/notices-seen", methods=["POST"])
def api_notices_seen():
    u = current_user()
    if not u or not u["monteur_id"]:
        return jsonify(ok=False), 403
    conn = db()
    try:
        conn.execute("UPDATE monteur_notices SET seen=1 WHERE monteur_id=? AND seen=0", (u["monteur_id"],))
        conn.commit()
    except Exception:
        pass
    conn.close()
    return jsonify(ok=True)


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
    u = current_user()
    conn = db()
    p = conn.execute("SELECT * FROM planning WHERE id=?", (pid,)).fetchone()
    if p and u and u["monteur_id"] and p["monteur_id"] != u["monteur_id"]:
        conn.close()
        return jsonify(ok=False, error="Dit is niet jouw order."), 403
    if p:
        conn.execute("UPDATE planning SET status='afgerond', afgerond_at=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), pid))
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
        conn.execute("UPDATE planning SET status='onderweg', onderweg_at=COALESCE(onderweg_at,?) WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), pid))
        conn.execute("UPDATE orders SET status='onderweg' WHERE id=?", (p["order_id"],))
        conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/monteur/arrive/<int:pid>", methods=["POST"])
def monteur_arrive(pid):
    """Monteur meldt dat hij bij de klant is = startpunt van de gemeten montageduur."""
    if not has_perm("monteur_app"):
        return jsonify(ok=False), 403
    u = current_user()
    conn = db()
    p = conn.execute("SELECT * FROM planning WHERE id=? AND monteur_id=?", (pid, u["monteur_id"])).fetchone()
    if p:
        conn.execute("UPDATE planning SET status='onderweg', aangekomen_at=COALESCE(aangekomen_at,?) WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), pid))
        conn.commit()
    conn.close()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
#  Verwachte aankomst voor de mail "Onze monteur is er bijna": vanaf de positie van
#  nu (telefoon, of de bus via Radius) over de weg naar het adres van de klant, met
#  verkeer (Google Routes). Zelfde Google-sleutel en adres-cache als de kantoor-app
#  (gedeelde database). Zonder sleutel of positie: een schatting.
# --------------------------------------------------------------------------- #
GOOGLE_ROUTES_URL = os.environ.get("GOOGLE_ROUTES_URL",
                                   "https://routes.googleapis.com/directions/v2:computeRoutes")
_NL_POSTCODE = _re.compile(r"\b\d{4}\s?[A-Za-z]{2}\b")


def _geo_key(q):
    # Gelijk houden met _geo_key in de kantoor-app (gedeelde geo_cache).
    return _re.sub(r"[^a-z0-9]+", " ", (q or "").lower()).strip()


def _addr_query(address, postal=None, city=None):
    # Gelijk houden met _addr_query in de kantoor-app.
    a = (address or "").strip()
    parts = [a] if a else []
    flat = a.lower().replace(" ", "")
    if postal and postal.strip() and postal.replace(" ", "").lower() not in flat:
        parts.append(postal.strip())
    if city and city.strip() and city.strip().lower() not in a.lower():
        parts.append(city.strip())
    return ", ".join(parts)


def _dest_point(address, postal, city):
    q = _addr_query(address, postal, city)
    conn = db()
    try:
        r = conn.execute("SELECT lat,lng FROM geo_cache WHERE addr_key=?", (_geo_key(q),)).fetchone()
    except Exception:
        r = None
    finally:
        conn.close()
    if r and r["lat"] is not None:
        return (r["lat"], r["lng"])
    if not _NL_POSTCODE.search(q):
        return None            # buiten Nederland zonder cache: geen gok
    try:
        url = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free?" + urllib.parse.urlencode(
            {"q": q, "rows": 1, "fl": "centroide_ll"})
        req = urllib.request.Request(url, headers={"User-Agent": "OfficeRoute/1.0"})
        doc = json.loads(urllib.request.urlopen(req, timeout=6).read().decode("utf-8"))["response"]["docs"][0]
        m = _re.match(r"POINT\(([-\d.]+) ([-\d.]+)\)", doc.get("centroide_ll") or "")
        return (float(m.group(2)), float(m.group(1))) if m else None
    except Exception:
        return None


def _google_key():
    k = (os.environ.get("GOOGLE_MAPS_API_KEY") or "").strip()
    if k:
        return k
    conn = db()
    try:
        r = conn.execute("SELECT value FROM integrations WHERE ikey='google_maps' AND field='api_key'").fetchone()
    except Exception:
        r = None
    finally:
        conn.close()
    return ((r["value"] if r else "") or "").strip()


def _drive_minutes(a, b):
    """Rijtijd in minuten van a naar b: Google met verkeer, anders een schatting."""
    key = _google_key()
    if key:
        try:
            def wp(p):
                return {"location": {"latLng": {"latitude": p[0], "longitude": p[1]}}}
            body = {"origin": wp(a), "destination": wp(b), "travelMode": "DRIVE",
                    "routingPreference": "TRAFFIC_AWARE", "languageCode": "nl-NL", "regionCode": "nl"}
            req = urllib.request.Request(GOOGLE_ROUTES_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                                                  "X-Goog-FieldMask": "routes.duration", "User-Agent": "OfficeRoute/1.0"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8") or "{}")
            return float(str(data["routes"][0]["duration"]).rstrip("s")) / 60.0
        except Exception:
            pass
    km = _dist_m(a[0], a[1], b[0], b[1]) / 1000.0 * 1.3
    if km < 0.3:
        return 0.0
    return km / (30 if km < 8 else (50 if km < 25 else 75)) * 60 + 3


def _round5(dt):
    """Op 5 minuten afgerond, voor een rustige 'rond 10:35' in de mail."""
    dt = dt.replace(second=0, microsecond=0)
    return dt + timedelta(minutes=(5 - dt.minute % 5) % 5 if dt.minute % 5 >= 3 else -(dt.minute % 5))


@bp.route("/monteur/announce/<int:pid>", methods=["POST"])
def monteur_announce(pid):
    """'Ik kom eraan' - informeer de klant (e-mail best-effort) en log het zodat kantoor het ziet."""
    u = current_user()
    if not u or not has_perm("monteur_app"):
        return jsonify(ok=False), 403
    conn = db()
    p = conn.execute("""SELECT p.id, o.client_id, o.email, o.order_number, o.track_token, c.name AS client,
                               o.delivery_address, o.postal, o.city
                        FROM planning p JOIN orders o ON o.id=p.order_id
                        LEFT JOIN clients c ON c.id=o.client_id
                        WHERE p.id=? AND p.monteur_id=?""", (pid, u["monteur_id"])).fetchone()
    if not p:
        conn.close()
        return jsonify(ok=False), 404
    # Positie: wat de telefoon nu meestuurt, anders de laatste bekende (telefoon of bus).
    data = request.get_json(silent=True) or {}
    try:
        pos = (float(data["lat"]), float(data["lng"]))
    except (KeyError, TypeError, ValueError):
        pos = None
    if not pos and u["monteur_id"]:
        r = conn.execute("SELECT lat, lng, updated_at FROM monteur_location WHERE monteur_id=?",
                         (u["monteur_id"],)).fetchone()
        if r and r["lat"] is not None and (r["updated_at"] or "") >= (
                datetime.now() - timedelta(minutes=15)).isoformat(timespec="minutes"):
            pos = (r["lat"], r["lng"])
    dest = _dest_point(p["delivery_address"], p["postal"], p["city"])
    mins = _drive_minutes(pos, dest) if (pos and dest) else 15
    eta = _round5(datetime.now() + timedelta(minutes=mins)).strftime("%H:%M")
    # De volgpagina werkt op het niet-raadbare track_token, niet op het ordernummer.
    # Geen token (oudere order)? Dan liever geen knop dan een dode link.
    token = (p["track_token"] or "").strip()
    track_url = ("%s/track/%s" % (os.environ.get("KANTOOR_URL", "https://planning-o-i-fra.onrender.com"), token)
                 if token else None)
    subject = "Onze monteur is er bijna"
    cells = [("user", "Monteur", u["name"]),
             ("clock", "Verwachte aankomst", "rond %s" % eta),
             ("hash", "Ordernummer", "#%s" % p["order_number"])]
    body = _mail_text(p["client"], _mailtxt("mailtxt_near_b"), cells, link=track_url)
    conn.execute("UPDATE planning SET arrival_mailed=1 WHERE id=?", (pid,))
    conn.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment)
                    VALUES(?,?,?,?,?,0)""",
                 (p["client_id"], "out", subject, body, datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    # Bij deze mail is de monteur al onderweg, dus de volgkaart is open. De
    # tweede knop blijft handig: de klant kan nog iets doorgeven.
    html = _mail_html(_mailtxt("mailtxt_near_h"), p["client"], _mailtxt("mailtxt_near_b"),
                      cells,
                      buttons=([("Volg live op de kaart", track_url),
                                ("Bericht doorgeven", track_url + "#bericht")]
                               if track_url else None))
    emailed = _smtp_send([p["email"]], subject, body, html)
    return jsonify(ok=True, emailed=emailed)


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


@bp.route("/monteur/uren", methods=["POST"])
def monteur_uren():
    """Uren indienen of aanpassen, voor vandaag of een eerdere dag (tot een maand terug):
    een monteur die het vergat, kan het zo alsnog doen. Kantoor ziet in het urenregister
    dat het achteraf is doorgegeven (submitted_at ligt na de werkdag)."""
    u = current_user()
    if not u or not u["monteur_id"]:
        return jsonify(ok=False, error="Geen monteurprofiel."), 403
    today = _today_iso()
    day = (request.form.get("date") or "").strip() or today
    if not _re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", day):
        return jsonify(ok=False, error="Kies een geldige datum."), 400
    if day > today:
        return jsonify(ok=False, error="Uren voor een dag in de toekomst kun je nog niet doorgeven."), 400
    if day < _uren_min_date(today):
        return jsonify(ok=False, error="Uren van meer dan een maand geleden geef je door via kantoor."), 400
    start = (request.form.get("start") or "").strip()
    end = (request.form.get("end") or "").strip()
    note = (request.form.get("note") or "").strip()[:40]
    calc = _calc_hours(start, end)
    if not calc:
        return jsonify(ok=False, error="Vul een geldige start- en eindtijd in (eind na start)."), 400
    worked, overtime = calc
    conn = db()
    conn.execute("""INSERT INTO work_hours(monteur_id,user_id,user_email,user_name,work_date,start_time,end_time,
                    worked_min,overtime_min,note,submitted_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(monteur_id,work_date) DO UPDATE SET start_time=excluded.start_time,
                    end_time=excluded.end_time,worked_min=excluded.worked_min,overtime_min=excluded.overtime_min,
                    note=excluded.note,submitted_at=excluded.submitted_at""",
                 (u["monteur_id"], u["id"], u["email"], u["name"], day, start, end,
                  worked, overtime, note, datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    return jsonify(ok=True)


@bp.route("/api/location", methods=["POST"])
def api_location():
    u = current_user()
    if not u or not u["monteur_id"] or not has_perm("monteur_app"):
        return jsonify(ok=False), 403
    data = request.get_json(silent=True) or {}
    try:
        lat, lng = float(data["lat"]), float(data["lng"])
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, error="Ongeldige locatie."), 400
    live = 1 if data.get("live", True) else 0
    mid = u["monteur_id"]
    # Server-side throttle: sla live-pushes over die <_LOC_MIN_INTERVAL s na de vorige komen.
    # (De 'uit'-melding live=0 gaat altijd door.)
    now_ts = time.time()
    if live and (now_ts - _LOC_LAST.get(mid, 0) < _LOC_MIN_INTERVAL):
        return jsonify(ok=True, skipped=True)
    _LOC_LAST[mid] = now_ts
    conn = db()
    conn.execute("""INSERT INTO monteur_location(monteur_id,lat,lng,updated_at,live) VALUES(?,?,?,?,?)
                    ON CONFLICT(monteur_id) DO UPDATE SET lat=excluded.lat,lng=excluded.lng,
                    updated_at=excluded.updated_at,live=excluded.live""",
                 (mid, lat, lng, datetime.now().isoformat(timespec="minutes"), live))
    # Thuiskomst vastleggen (voor de urencontrole op kantoor): eerste keer binnen de
    # thuisstraal = thuis-sinds; zolang hij nog onderweg is, blijft dit leeg.
    if live:
        try:
            home = _HOME_CACHE.get(mid)
            if home is None:
                m = conn.execute("SELECT home_lat,home_lng FROM monteurs WHERE id=?", (mid,)).fetchone()
                home = (float(m["home_lat"]), float(m["home_lng"])) if (m and m["home_lat"] and m["home_lng"]) else (0.0, 0.0)
                _HOME_CACHE[mid] = home
            if home != (0.0, 0.0):
                now = datetime.now().isoformat(timespec="minutes")
                today = _today_iso()
                if _dist_m(lat, lng, home[0], home[1]) <= HOME_RADIUS_M:
                    conn.execute("""INSERT INTO monteur_day_gps(monteur_id,date,home_since) VALUES(?,?,?)
                                    ON CONFLICT(monteur_id,date) DO UPDATE SET
                                    home_since=COALESCE(monteur_day_gps.home_since, excluded.home_since)""",
                                 (u["monteur_id"], today, now))
                else:
                    conn.execute("""INSERT INTO monteur_day_gps(monteur_id,date,home_since) VALUES(?,?,NULL)
                                    ON CONFLICT(monteur_id,date) DO UPDATE SET home_since=NULL""",
                                 (u["monteur_id"], today))
        except Exception:
            pass
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
    if cat not in ("verlof", "afspraak"):
        cat = "verlof"
    d_from = (f.get("date_from") or "").strip()
    d_to = (f.get("date_to") or "").strip() or d_from
    if not _re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", d_from):
        return jsonify(ok=False, error="Kies eerst een datum.")
    if not _re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", d_to) or d_to < d_from:
        return jsonify(ok=False, error="De einddatum ligt voor de begindatum.")
    conn = db()
    conn.execute("""INSERT INTO leave_requests(user_id,user_name,is_monteur,monteur_id,category,leave_type,
                    date_from,date_to,time_from,time_to,reason,status,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open', ?)""",
                 (u["id"], u["name"], 1 if u["role"] == "monteur" else 0, u["monteur_id"],
                  cat, (f.get("leave_type") if cat == "verlof" else "afspraak"),
                  d_from, (d_to if cat == "verlof" else d_from),
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


MAIL_TEXT_DEFAULTS = {
    "mailtxt_near_h": "Onze monteur is er bijna",
    "mailtxt_near_b": "Onze monteur is er bijna. U kunt hem live volgen via de knop hieronder.",
}


def _mailtxt(key):
    """Bewerkbare mailtekst uit de gedeelde settings (beheerd in de kantoor-app), anders standaard."""
    try:
        conn = db()
        r = conn.execute("SELECT value FROM settings WHERE skey=?", (key,)).fetchone()
        conn.close()
        v = r["value"] if r else None
    except Exception:
        v = None
    return v if (v is not None and v.strip()) else MAIL_TEXT_DEFAULTS.get(key, "")


def _paras(greet, bodytext):
    return [greet] + [p for p in (bodytext or "").split("\n\n") if p.strip()]


def _brand_email(heading, paragraphs, info=None, button=None, note=None):
    """OUDE opmaak, NIET meer in gebruik in deze app.

    De klantmail "onze monteur is er bijna" gebruikt sinds het gelijktrekken van
    de vier klantmails _mail_html() hieronder. Deze functie blijft staan als
    terugval en om te kunnen vergelijken; verwijderen kan als niets hem mist.
    """
    paras = ""
    for p in (paragraphs or []):
        if p:
            paras += ('<p style="margin:0 0 14px;font-size:15px;color:#3a4a45;line-height:1.65;">'
                      + _esc(p).replace("\n", "<br>") + '</p>')
    info_html = ""
    if info:
        rows = ""
        for label, value in info:
            rows += ('<tr><td style="padding:7px 2px;font-size:13px;color:#6b7a74;">' + _esc(label) + '</td>'
                     '<td style="padding:7px 2px;font-size:14px;color:#16302d;font-weight:bold;text-align:right;">'
                     + _esc(value) + '</td></tr>')
        info_html = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     'style="background:#eef3ec;border:1px solid #cfe0d7;border-radius:12px;margin:4px 0 16px;">'
                     '<tr><td style="padding:8px 16px;"><table role="presentation" width="100%" cellpadding="0" '
                     'cellspacing="0">' + rows + '</table></td></tr></table>')
    note_html = ""
    if note:
        note_html = ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                     'style="background:#f7efe0;border:1px solid #e3c98f;border-radius:12px;margin:0 0 16px;">'
                     '<tr><td style="padding:12px 14px;font-size:13px;color:#6b4e15;line-height:1.55;">'
                     + _esc(note) + '</td></tr></table>')
    btn_html = ""
    if button:
        btn_html = ('<table role="presentation" cellpadding="0" cellspacing="0" style="margin:2px 0 16px;">'
                    '<tr><td style="background:#0f3d3e;border-radius:10px;">'
                    '<a href="' + _esc(button[1]) + '" style="display:inline-block;padding:12px 22px;color:#ffffff;'
                    'font-size:14px;font-weight:bold;text-decoration:none;">' + _esc(button[0]) + '</a></td></tr></table>')
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#f1ede5;padding:24px 0;margin:0;"><tr><td align="center">'
            '<table role="presentation" width="520" cellpadding="0" cellspacing="0" style="max-width:520px;width:100%;'
            'background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e6ebe4;font-family:Arial,Helvetica,sans-serif;">'
            '<tr><td style="background:#0f3d3e;padding:18px 28px;">'
            '<span style="color:#ffffff;font-size:18px;font-weight:bold;letter-spacing:2px;">OFFICE-INTERIOR</span></td></tr>'
            '<tr><td style="height:3px;background:#cda35a;"></td></tr>'
            '<tr><td style="padding:26px 28px 4px;">'
            '<h1 style="margin:0 0 14px;font-size:20px;color:#0f3d3e;font-weight:bold;">' + _esc(heading) + '</h1>'
            + paras + info_html + note_html + btn_html +
            '</td></tr><tr><td style="padding:6px 28px 22px;">'
            '<p style="margin:10px 0 0;padding-top:14px;border-top:1px solid #eef0ec;font-size:12px;color:#8a948f;'
            'line-height:1.6;">Vragen? Mail planning@office-interior.com of bel 085-0481444.</p></td></tr>'
            '</table></td></tr></table>')


# --------------------------------------------------------------------------- #
#  Sjabloon voor de klantmail "Onze monteur is er bijna"
#
#  Kopie van het sjabloon in de kantoor-app (planning_oi.py), zodat alle vier de
#  automatische klantmails er hetzelfde uitzien. Elke app heeft zijn eigen
#  verzendcode, dus deze kopie volgt hetzelfde patroon als _brand_email hierboven.
#  Wijzig je het ontwerp in de kantoor-app, pas het hier dan ook aan.
#
#  De AFBEELDINGEN komen bewust van de kantoor-app (KANTOOR_URL), zodat er maar
#  één set iconen bestaat en deze app geen eigen kopie hoeft bij te houden.
#
#  Vullen met .replace() op {{...}}, niet met % of .format(): de CSS zit vol
#  accolades en procenttekens.
# --------------------------------------------------------------------------- #
MAIL_COLORS = {
    # buitenvlak lichtgrijs (was beige #f6f1e8); gelijk met de kantoor-app
    "outer": "#efefef", "card": "#ffffff",
    "teal": "#053f44", "text": "#31464e", "label": "#6c7b80",
    # knoppen in de huiskleur groen; koppen en waarden blijven teal
    "btn": "#2f9631",
    "rule": "#e1e5e3", "foot_rule": "#e8ebe9",
}
MAIL_ICONS = {"calendar": "icon-calendar.png", "clock": "icon-clock.png",
              "hash": "icon-hash.png", "user": "icon-user.png"}
MAIL_CONTACT_EMAIL = "planning@office-interior.com"

MAIL_TEMPLATE = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>{{heading}}</title>
<style>
html, body { margin:0 !important; padding:0 !important; width:100% !important; }
table, td { border-collapse:collapse !important; }
img { border:0; outline:none; text-decoration:none; -ms-interpolation-mode:bicubic; display:block; }
a { color:{{teal}}; text-decoration:none; }
@media screen and (max-width: 560px) {
  /* De kaart heeft bewust GEEN rand: met een rand plus border-radius loopt de
     achtergrond niet mee met de bocht en zie je een harde lijn in de hoek. */
  .card { border-radius:0 !important; }
  .body-pad { padding:30px 20px 24px 20px !important; }
  .headline { font-size:28px !important; }
  .copy { font-size:16px !important; }
  .meta-cell { display:block !important; width:100% !important; padding:0 0 18px 0 !important; }
  .meta-sep { display:none !important; }
  .btn-cell { display:block !important; width:100% !important; padding:0 0 12px 0 !important; }
  /* anders breekt planning@office-interior.com op 375px over twee regels */
  .foot-mail { font-size:14px !important; }
}
</style>
</head>
<body style="margin:0; padding:0; background:{{outer}};">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%; background:{{outer}};">
<tr><td align="center" style="padding:28px 12px;">
<table role="presentation" width="640" cellspacing="0" cellpadding="0" border="0" class="card" bgcolor="{{card}}" style="width:100%; max-width:640px; background:{{card}}; border-radius:22px;">
<tr><td class="body-pad" style="padding:44px 42px 32px 42px;">

<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
<tr><td align="center" style="padding:8px 0 46px 0;">
<img src="{{logo_url}}" width="360" alt="OFFICE-INTERIOR" style="width:100%; max-width:360px; height:auto;">
</td></tr></table>

<h1 class="headline" style="margin:0; font-family:Arial,Helvetica,sans-serif; font-size:34px; line-height:1.15; color:{{teal}}; font-weight:700; text-align:center;">{{heading}}</h1>

<div class="copy" style="font-family:Arial,Helvetica,sans-serif; font-size:17px; line-height:1.55; color:{{text}}; text-align:center; padding-top:34px;">{{greeting}}</div>

{{intro_blocks}}
{{meta_row}}
{{button_block}}

</td></tr>

<tr><td align="center" style="border-top:1px solid {{foot_rule}}; padding:24px 42px 30px 42px;">
<table role="presentation" cellspacing="0" cellpadding="0" border="0" align="center"><tr>
<td width="62" style="width:62px; vertical-align:middle;">
<img src="{{icon_mail}}" width="56" height="56" alt="" style="width:56px; height:56px;">
</td>
<td style="vertical-align:middle; text-align:left;">
<div style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:{{label}};">Vragen over de levering?</div>
<div class="foot-mail" style="font-family:Arial,Helvetica,sans-serif; font-size:16px; color:{{teal}}; font-weight:700; margin-top:3px;">
<a href="mailto:{{contact_email}}" style="color:{{teal}}; text-decoration:none;">{{contact_email}}</a></div>
</td></tr></table>
</td></tr>

</table>
</td></tr></table>
</body>
</html>"""


def _mail_asset(path):
    """Absolute URL naar een bestand in de static van de KANTOOR-app."""
    base = os.environ.get("KANTOOR_URL", "https://planning-o-i-fra.onrender.com")
    return "%s/static/%s" % (base.rstrip("/"), path.lstrip("/"))


def _mail_meta_row(cells):
    """Rij gegevens met iconen, gescheiden door verticale lijntjes (1 tot 3)."""
    if not cells:
        return ""
    c = MAIL_COLORS
    width = "%.4f" % (100.0 / len(cells))
    out = ('<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
           'border="0" style="margin-top:38px;"><tr>')
    for i, (icon, label, value) in enumerate(cells):
        if i:
            out += ('<td class="meta-sep" width="1" style="width:1px; border-left:1px solid '
                    + c["rule"] + ';">&nbsp;</td>')
        out += ('<td class="meta-cell" align="center" style="width:' + width + '%;'
                ' padding:0 10px; text-align:center; vertical-align:top;">'
                '<img src="' + _mail_asset("mail/" + MAIL_ICONS[icon]) + '" width="72"'
                ' height="72" alt=""'
                ' style="width:72px; height:72px; margin:0 auto 10px auto;">'
                '<div style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:'
                + c["label"] + ';">' + _esc(label) + '</div>'
                '<div style="font-family:Arial,Helvetica,sans-serif; font-size:18px;'
                ' line-height:1.3; color:' + c["teal"] + '; font-weight:700; margin-top:4px;">'
                + _esc(value) + '</div></td>')
    return out + '</tr></table>'


def _mail_button_block(buttons):
    """Eén of twee gecentreerde knoppen naast elkaar.

    buttons = (tekst, url) of een lijst daarvan. Geef '&' mee, niet '&amp;':
    hier wordt ge-escaped. Eerste knop gevuld, tweede wit met teal rand.
    """
    if not buttons:
        return ""
    if isinstance(buttons, tuple):
        buttons = [buttons]
    items = [b for b in buttons if b and b[1]]
    if not items:
        return ""
    c = MAIL_COLORS
    cells = ""
    for i, (text, url) in enumerate(items):
        fill = c["btn"] if i == 0 else "#ffffff"
        ink = "#ffffff" if i == 0 else c["btn"]
        pad = "0 0 0 10px" if i else "0"
        # ÉÉN element bepaalt de vorm: de link draagt de achtergrond, de rand en
        # de ronding. Zet je die ook op de tabelcel, dan zie je bij de gevulde
        # knop de vierkante celrand om de ronde link heen.
        cells += ('<td class="btn-cell" align="center" valign="middle"'
                  ' style="padding:' + pad + ';">'
                  '<a href="' + _esc(url) + '" style="display:inline-block;'
                  ' padding:13px 26px; background:' + fill + '; color:' + ink + ';'
                  ' border:2px solid ' + c["btn"] + '; border-radius:12px;'
                  ' font-family:Arial,Helvetica,sans-serif; font-size:16px;'
                  ' font-weight:700; text-decoration:none; white-space:nowrap;">'
                  + _esc(text) + '</a></td>')
    return ('<table role="presentation" cellspacing="0" cellpadding="0" border="0"'
            ' align="center" style="margin:34px auto 6px;"><tr>' + cells + '</tr></table>')


def _mail_html(heading, client, intro, cells, buttons=None):
    """Bouw de klantmail uit het gedeelde sjabloon.

    buttons = (tekst, url) of een lijst daarvan (maximaal twee naast elkaar).
    """
    blocks = ""
    for para in [p for p in (intro or "").split("\n\n") if p.strip()]:
        blocks += ('<div class="copy" style="font-family:Arial,Helvetica,sans-serif;'
                   ' font-size:17px; line-height:1.55; color:' + MAIL_COLORS["text"] + ';'
                   ' text-align:center; padding-top:18px;">'
                   + _esc(para).replace("\n", "<br>") + '</div>')
    values = dict(MAIL_COLORS)
    values.update({
        "heading": _esc(heading or ""),
        "greeting": "Beste %s," % _esc(client or "klant"),
        "intro_blocks": blocks,
        "meta_row": _mail_meta_row(cells),
        "button_block": _mail_button_block(buttons),
        "logo_url": _mail_asset("logos/office-interior.png"),
        "icon_mail": _mail_asset("mail/icon-mail.png"),
        "contact_email": MAIL_CONTACT_EMAIL,
    })
    html = MAIL_TEMPLATE
    for k, v in values.items():
        html = html.replace("{{%s}}" % k, v)
    return html


def _mail_text(client, intro, cells, link=None):
    """Platte-tekstversie: bevat de gegevens EN de link, want als de client de
    HTML blokkeert bestaat de knop niet."""
    lines = ["Beste %s," % (client or "klant"), "", (intro or ""), ""]
    lines += ["%s: %s" % (label, value) for _, label, value in cells]
    if link:
        lines += ["", link]
    lines += ["", "Met vriendelijke groet,", "Office-Interior"]
    return "\n".join(lines)


def _smtp_send(to_list, subject, body, html=None):
    """Verstuur (Resend/HTTPS of SMTP-fallback) via de gedeelde 'email'-integratie. Respecteert testmodus."""
    return _api_send([t for t in (to_list or []) if t], subject, body, html)


def _send_bus_issue_email(monteur_name, bus_label, plate, message):
    """E-mail naar kantoor (Jorik & Stijn) over een gemeld bus-probleem."""
    return _smtp_send(BUS_ISSUE_RECIPIENTS,
                      ("Bus-issue: %s %s" % (bus_label or "onbekende bus", plate or "")).strip(),
                      "%s meldt een probleem met %s (%s):\n\n%s\n\n- OfficeRoute monteur-app"
                      % (monteur_name, bus_label or "-", plate or "-", message))


@bp.route("/api/bus-issue", methods=["POST"])
def api_bus_issue():
    u = current_user()
    if not u:
        return jsonify(ok=False), 403
    message = (request.form.get("message") or "").strip()
    if not message:
        return jsonify(ok=False, error="Omschrijf het probleem even."), 400
    bus = _current_bus()
    label = bus["label"] if bus else None
    plate = bus["plate"] if bus else None
    conn = db()
    conn.execute("""INSERT INTO bus_issues(monteur_id,monteur_name,reporter_email,bus_label,plate,message,status,created_at)
                    VALUES(?,?,?,?,?,?, 'open', ?)""",
                 (u["monteur_id"], u["name"], u["email"], label, plate, message,
                  datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    emailed = _send_bus_issue_email(u["name"], label, plate, message)
    return jsonify(ok=True, emailed=emailed)


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
@bp.route("/version")
def version():
    return jsonify(v=APP_VERSION)


@bp.route("/manifest.webmanifest")
def manifest():
    i192 = url_for("static", filename="icon-192.png", v="2")
    i512 = url_for("static", filename="icon-512.png", v="2")
    data = {"name": "OfficeRoute - Monteur", "short_name": "OfficeRoute",
            "start_url": "/monteur", "scope": "/", "display": "standalone",
            "background_color": "#0f3d3e", "theme_color": "#0f3d3e",
            "icons": [{"src": i192, "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": i512, "sizes": "512x512", "type": "image/png", "purpose": "any"},
                      {"src": i512, "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
    return Response(json.dumps(data), mimetype="application/manifest+json")


@bp.after_request
def _no_cache_html(resp):
    # Dynamische HTML nooit cachen: dwingt iPhone/Safari/PWA om de verse versie te halen.
    if resp.headers.get("Content-Type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp


@bp.route("/sw.js")
def service_worker():
    js = ("const C='officeroute-app-v2';"
          "self.addEventListener('install',e=>self.skipWaiting());"
          "self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(ks=>Promise.all("
          "ks.filter(k=>k!==C).map(k=>caches.delete(k)))).then(()=>self.clients.claim())));"
          "self.addEventListener('fetch',e=>{const r=e.request;if(r.method!=='GET')return;"
          "if(r.mode==='navigate'){e.respondWith(fetch(r).catch(()=>caches.match(r)));return;}"
          "e.respondWith(fetch(r).then(res=>{const cp=res.clone();caches.open(C).then(c=>c.put(r,cp));return res;})"
          ".catch(()=>caches.match(r)));});")
    return Response(js, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": "/",
                             "Cache-Control": "no-cache, no-store, must-revalidate"})


init_db()


bp.teardown_app_request(_release_request_conns)
