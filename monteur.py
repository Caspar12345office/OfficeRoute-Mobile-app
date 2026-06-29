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
import os, json, time, sqlite3, secrets, smtplib
import re as _re
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

# Wagenpark — kentekens van onze vloot (buskeuze toont "Bus N" + kenteken, geen merk/chauffeur)
FLEET = [
    {"id": "bus1", "label": "Bus 1", "plate": "V-16-FGH"},
    {"id": "bus2", "label": "Bus 2", "plate": "VLT-21-N"},
    {"id": "bus3", "label": "Bus 3", "plate": "VVB-14-T"},
    {"id": "bus4", "label": "Bus 4", "plate": "VSN-02-X"},
    {"id": "bus5", "label": "Bus 5", "plate": "VTZ-73-G"},
    {"id": "bus6", "label": "Bus 6", "plate": "VVL-09-B"},
    {"id": "bus7", "label": "Bus 7", "plate": "V-95-DVF"},
    {"id": "bus8", "label": "Bus 8", "plate": "VTZ-77-G"},
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
#  Database-laag (gelijk aan de kantoorsoftware) — SQLite of PostgreSQL
# --------------------------------------------------------------------------- #
_PG_URL = os.environ.get("DATABASE_URL", "")
if _PG_URL.startswith("postgres://"):
    _PG_URL = _PG_URL.replace("postgres://", "postgresql://", 1)
IS_PG = bool(_PG_URL)
_NO_ID_TABLES = {"monteur_location", "route_closed", "integrations", "settings"}


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
"""


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
    return bool((c.get("smtp_host") or "").strip() and (c.get("smtp_user") or "").strip()
                and (c.get("smtp_pass") or "").strip())


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
    """Mail de 6-cijferige inlogcode (nette HTML + platte-tekst-terugval).
    True bij succes, anders False (val terug op scherm)."""
    if not to_email:
        return False
    c = _email_cfg()
    host = (c.get("smtp_host") or "").strip()
    user = (c.get("smtp_user") or "").strip()
    pwd = (c.get("smtp_pass") or "").strip()
    if not (host and user and pwd):
        return False
    if (c.get("send_live") or "0") != "1":
        return False   # testmodus: niets echt versturen
    msg = EmailMessage()
    msg["Subject"] = "Je OfficeRoute-inlogcode: %s" % code
    msg["From"] = "%s <%s>" % ((c.get("from_name") or "OfficeRoute").strip(), user)
    msg["To"] = to_email
    msg.set_content("Hoi %s,\n\nJe verificatiecode voor OfficeRoute is: %s\n\n"
                    "De code is 5 minuten geldig.\n\n"
                    "Niet zelf ingelogd? Neem dan direct contact op met de beheerder en deel "
                    "deze code met niemand.\n" % (name or "collega", code))
    msg.add_alternative(_twofa_email_html(name, code), subtype="html")
    try:
        with smtplib.SMTP(host, int(c.get("smtp_port") or 587), timeout=10) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  Auth-routes (met 2FA)
# --------------------------------------------------------------------------- #
@bp.route("/")
def home():
    return redirect(url_for("planning.monteur_app") if current_user() else url_for("planning.login"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    error = ""; show_2fa = False; demo_code = None; twofa_email = None; code_sent = False
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
                error = "Onjuiste code."; show_2fa = True; twofa_email = tf.get("email")
                if tf.get("sent"):
                    code_sent = True
                else:
                    demo_code = tf.get("code")
        else:
            email = (request.form.get("email") or "").strip().lower()
            pw = request.form.get("password") or ""
            conn = db()
            u = conn.execute("SELECT * FROM users WHERE lower(email)=? AND active=1", (email,)).fetchone()
            conn.close()
            if u and check_password_hash(u["password"], pw):
                code = "%06d" % secrets.randbelow(1000000)
                show_2fa = True; twofa_email = u["email"]
                code_sent = _send_2fa_email(u["email"], code, u["name"])
                session["twofa"] = {"uid": u["id"], "code": code, "exp": time.time() + 300,
                                    "email": u["email"], "sent": code_sent}
                if not code_sent:
                    demo_code = code   # terugval: toon op scherm als e-mail (nog) niet werkt
            else:
                error = "Onjuiste inloggegevens."
    return render_template("planning/login.html", error=error, show_2fa=show_2fa,
                           demo_code=demo_code, twofa_email=twofa_email, code_sent=code_sent)


@bp.route("/logout")
def logout():
    session.pop("p_user_id", None); session.pop("twofa", None)
    session.pop("bus_id", None); session.pop("bus_done", None)
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
        conn.execute("""INSERT INTO bus_choices(user_id,user_email,user_name,bus_id,bus_label,plate,date,ts)
                        VALUES(?,?,?,?,?,?,?,?)""",
                     (u["id"], u["email"], u["name"], item["id"], item["label"], item["plate"],
                      _today_iso(), datetime.now().isoformat(timespec="seconds")))
        conn.commit(); conn.close()
    except Exception:
        pass


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
    # Eerst een bus kiezen (of bewust overslaan) voordat het overzicht verschijnt
    if not session.get("bus_done"):
        return redirect(url_for("planning.kies_bus"))
    u = current_user()
    conn = db()
    _purge_old_customer_notes(conn)
    mid = u["monteur_id"]
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
    conn.close()
    alerts = route_alerts(mid, bool(jobs)) if mid else []
    all_done = bool(jobs) and all(j["status"] == "afgerond" for j in jobs)
    client_notes = [{"client": j["client"], "note": j["customer_note"]}
                    for j in jobs if (j["customer_note"] or "").strip()]
    return render_template("planning/monteur_app.html", monteur=monteur, jobs=jobs, alerts=alerts,
                           closed=closed, all_done=all_done, region=_region_for(jobs),
                           bus=_current_bus(), contacts=OFFICE_CONTACTS, client_notes=client_notes,
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


@bp.route("/monteur/announce/<int:pid>", methods=["POST"])
def monteur_announce(pid):
    """'Ik kom eraan' — informeer de klant (e-mail best-effort) en log het zodat kantoor het ziet."""
    u = current_user()
    if not u or not has_perm("monteur_app"):
        return jsonify(ok=False), 403
    conn = db()
    p = conn.execute("""SELECT p.id, o.client_id, o.email, o.order_number, c.name AS client
                        FROM planning p JOIN orders o ON o.id=p.order_id
                        LEFT JOIN clients c ON c.id=o.client_id
                        WHERE p.id=? AND p.monteur_id=?""", (pid, u["monteur_id"])).fetchone()
    if not p:
        conn.close()
        return jsonify(ok=False), 404
    eta = (datetime.now() + timedelta(minutes=15)).strftime("%H:%M")
    track_url = "https://planning-o-i.onrender.com/track/%s" % p["order_number"]
    subject = "Onze monteur is er bijna"
    body = ("Beste %s,\n\nOnze monteur %s is er bijna en verwacht rond %s bij u te zijn. "
            "U kunt hem live volgen via de link in deze e-mail.\n\nMet vriendelijke groet,\nOffice-Interior"
            % (p["client"] or "klant", u["name"], eta))
    conn.execute("UPDATE planning SET arrival_mailed=1 WHERE id=?", (pid,))
    conn.execute("""INSERT INTO email_log(client_id,direction,subject,body,ts,has_attachment)
                    VALUES(?,?,?,?,?,0)""",
                 (p["client_id"], "out", subject, body, datetime.now().isoformat(timespec="minutes")))
    conn.commit()
    conn.close()
    html = _brand_email(_mailtxt("mailtxt_near_h"),
                        _paras("Beste %s," % (p["client"] or "klant"), _mailtxt("mailtxt_near_b")),
                        info=[("Monteur", u["name"]), ("Verwachte aankomst", "rond %s" % eta),
                              ("Ordernummer", "#%s" % p["order_number"])],
                        button=("Volg live op de kaart", track_url))
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
    """Nette HTML-klantmail in de OFFICE-INTERIOR-huisstijl (teal/goud)."""
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


def _smtp_send(to_list, subject, body, html=None):
    """Best-effort e-mail via de SMTP-config uit de gedeelde 'integrations'-tabel.
    Niet ingesteld (demo) -> False; de actie is dan nog steeds in de software vastgelegd."""
    to_list = [t for t in (to_list or []) if t]
    if not to_list:
        return False
    try:
        conn = db()
        cfg = {r["field"]: r["value"] for r in
               conn.execute("SELECT field,value FROM integrations WHERE ikey=?", ("email",)).fetchall()}
        conn.close()
    except Exception:
        cfg = {}
    host = (cfg.get("smtp_host") or "").strip()
    if not host:
        return False
    if (cfg.get("send_live") or "0") != "1":
        return False   # testmodus: niets echt versturen
    user = (cfg.get("smtp_user") or "").strip()
    pwd = (cfg.get("smtp_pass") or "").strip()
    sender = user or "noreply@office-interior.nl"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "%s <%s>" % ((cfg.get("from_name") or "Office-Interior").strip(), sender)
    msg["To"] = ", ".join(to_list)
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(host, int(cfg.get("smtp_port") or 587), timeout=10) as s:
            s.starttls()
            if user and pwd:
                s.login(user, pwd)
            s.send_message(msg)
        return True
    except Exception:
        return False


def _send_bus_issue_email(monteur_name, bus_label, plate, message):
    """E-mail naar kantoor (Jorik & Stijn) over een gemeld bus-probleem."""
    return _smtp_send(BUS_ISSUE_RECIPIENTS,
                      ("Bus-issue: %s %s" % (bus_label or "onbekende bus", plate or "")).strip(),
                      "%s meldt een probleem met %s (%s):\n\n%s\n\n— OfficeRoute monteur-app"
                      % (monteur_name, bus_label or "—", plate or "—", message))


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
    i192 = url_for("static", filename="icon-192.png")
    i512 = url_for("static", filename="icon-512.png")
    data = {"name": "OfficeRoute — Monteur", "short_name": "OfficeRoute",
            "start_url": "/monteur", "scope": "/", "display": "standalone",
            "background_color": "#0f3d3e", "theme_color": "#0f3d3e",
            "icons": [{"src": i192, "sizes": "192x192", "type": "image/png", "purpose": "any"},
                      {"src": i512, "sizes": "512x512", "type": "image/png", "purpose": "any"},
                      {"src": i512, "sizes": "512x512", "type": "image/png", "purpose": "maskable"}]}
    return Response(json.dumps(data), mimetype="application/manifest+json")


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
