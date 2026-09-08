import os
import re
import sqlite3
import secrets
import smtplib
from email.message import EmailMessage
from functools import wraps
from datetime import datetime

import stripe
from flask import Flask, request, jsonify, session, send_from_directory, redirect
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:
    psycopg2 = None
    RealDictCursor = None

BASE = os.path.dirname(os.path.abspath(__file__))

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

VOLUME = (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.environ.get("DATA_DIR") or "").rstrip("/")
DATA = VOLUME or os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
SQLITE_DB = os.path.join(DATA, "codia.db")
USE_PG = bool(DATABASE_URL and psycopg2)

app = Flask(__name__)
app.secret_key = os.environ.get("CODIA_SECRET", secrets.token_hex(32))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30

PAYOUT_TO = "contact@cod-ia.fr"
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PK = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:3000").rstrip("/")

ADMIN_EMAIL = (os.environ.get("ADMIN_EMAIL") or "contact@cod-ia.fr").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or ""

LEVEL_ORDER = ["START", "PRO", "ELITE"]
TIER_LABELS = ["premier", "deuxième", "troisième", "quatrième", "cinquième"]

LEVELS = {
    "START": {
        "price": 0,
        "rewards": [
            {"points": 10, "reward": 20},
            {"points": 25, "reward": 50},
            {"points": 50, "reward": 120},
        ],
    },
    "PRO": {
        "price": 0,
        "rewards": [
            {"points": 100, "reward": 200},
            {"points": 250, "reward": 500},
            {"points": 500, "reward": 1000},
        ],
    },
    "ELITE": {
        "price": 0,
        "rewards": [
            {"points": 1100, "reward": 2200},
            {"points": 1335, "reward": 2670},
            {"points": 1700, "reward": 3400},
        ],
    },
}

DEMO_MEMBERS = [
    {"id": -1, "name": "Lina Moreau", "username": "lina", "points": 8, "level": "START"},
    {"id": -2, "name": "Noah Bernard", "username": "noah", "points": 14, "level": "START"},
    {"id": -3, "name": "Emma Laurent", "username": "emma", "points": 22, "level": "START"},
    {"id": -4, "name": "Lucas Petit", "username": "lucas", "points": 6, "level": "START"},
    {"id": -5, "name": "Hugo Richard", "username": "hugo", "points": 11, "level": "START"},
    {"id": -6, "name": "Léa Durand", "username": "lea", "points": 19, "level": "START"},
    {"id": -7, "name": "Raphaël Dubois", "username": "raphael", "points": 4, "level": "START"},
    {"id": -8, "name": "Louis Simon", "username": "louis", "points": 9, "level": "START"},
    {"id": -9, "name": "Inès Michel", "username": "ines", "points": 16, "level": "START"},
    {"id": -10, "name": "Adam Lefevre", "username": "adam", "points": 3, "level": "START"},
    {"id": -11, "name": "Nathan Garcia", "username": "nathan", "points": 12, "level": "START"},
    {"id": -12, "name": "Jade David", "username": "jade", "points": 7, "level": "START"},
    {"id": -13, "name": "Théo Bertrand", "username": "theo", "points": 18, "level": "START"},
    {"id": -14, "name": "Tom Vincent", "username": "tom", "points": 5, "level": "START"},
    {"id": -15, "name": "Louise Fournier", "username": "louise", "points": 13, "level": "START"},
    {"id": -16, "name": "Alice Girard", "username": "alice", "points": 10, "level": "START"},
    {"id": -17, "name": "Evan Lambert", "username": "evan", "points": 2, "level": "START"},
    {"id": -18, "name": "Nina Bonnet", "username": "nina", "points": 21, "level": "START"},
    {"id": -19, "name": "Arthur Francois", "username": "arthur", "points": 15, "level": "START"},
    {"id": -20, "name": "Jules Lefebvre", "username": "jules", "points": 1, "level": "START"},
    {"id": -21, "name": "Maya Rousseau", "username": "maya", "points": 17, "level": "START"},
    {"id": -22, "name": "Ethan Nicolas", "username": "ethan", "points": 9, "level": "START"},
    {"id": -23, "name": "Louna Henry", "username": "louna", "points": 23, "level": "START"},
    {"id": -24, "name": "Chloé Robert", "username": "chloe", "points": 118, "level": "PRO"},
    {"id": -25, "name": "Manon Morel", "username": "manon", "points": 142, "level": "PRO"},
    {"id": -26, "name": "Camille Roux", "username": "camille", "points": 187, "level": "PRO"},
    {"id": -27, "name": "Sarah Roux", "username": "sarah", "points": 96, "level": "PRO"},
    {"id": -28, "name": "Maxime Moreau", "username": "maxime", "points": 211, "level": "PRO"},
    {"id": -29, "name": "Eva Martinez", "username": "eva", "points": 164, "level": "PRO"},
    {"id": -30, "name": "Sacha Perrin", "username": "sacha", "points": 1260, "level": "ELITE"},
]


class DB:
    def __init__(self):
        if USE_PG:
            self.con = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
            self.con.autocommit = False
            self.cur = self.con.cursor()
        else:
            self.con = sqlite3.connect(SQLITE_DB)
            self.con.row_factory = sqlite3.Row
            self.con.execute("PRAGMA foreign_keys = ON")
            self.cur = None

    def _sql(self, sql):
        return sql.replace("?", "%s") if USE_PG else sql

    def execute(self, sql, args=()):
        q = self._sql(sql)
        if USE_PG:
            self.cur.execute(q, args)
            return self.cur
        return self.con.execute(q, args)

    def commit(self):
        self.con.commit()

    def rollback(self):
        try:
            self.con.rollback()
        except Exception:
            pass

    def close(self):
        self.con.close()

    def executescript(self, script):
        if not USE_PG:
            self.con.executescript(script)
            return
        for part in script.split(";"):
            part = part.strip()
            if part:
                self.cur.execute(part)


def db():
    return DB()


def is_unique_error(e):
    if isinstance(e, sqlite3.IntegrityError):
        return True
    msg = str(e).lower()
    return "unique" in msg or "duplicate" in msg


def pg_add_column(con, table, column, spec):
    con.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {spec}")


def init_db():
    con = db()
    if USE_PG:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                name TEXT DEFAULT 'Membre',
                username TEXT,
                email TEXT,
                password_hash TEXT,
                code TEXT,
                referred_by INTEGER,
                level TEXT DEFAULT 'START',
                points INTEGER DEFAULT 0,
                claimed INTEGER DEFAULT 0,
                paid INTEGER DEFAULT 0,
                ref_locked INTEGER DEFAULT 0,
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                iban TEXT DEFAULT '',
                photo TEXT DEFAULT '',
                created_at TEXT,
                tier_index INTEGER DEFAULT 0,
                points_locked INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_id INTEGER,
                referred_id INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS activities (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                kind TEXT,
                title TEXT,
                description TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS posts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                type TEXT,
                code TEXT,
                description TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payouts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                amount INTEGER,
                status TEXT DEFAULT 'pending',
                created_at TEXT
            );
            """
        )
        to_add = {
            "users": {
                "name": "TEXT DEFAULT 'Membre'",
                "username": "TEXT",
                "email": "TEXT",
                "password_hash": "TEXT",
                "code": "TEXT",
                "referred_by": "INTEGER",
                "level": "TEXT DEFAULT 'START'",
                "points": "INTEGER DEFAULT 0",
                "claimed": "INTEGER DEFAULT 0",
                "paid": "INTEGER DEFAULT 0",
                "ref_locked": "INTEGER DEFAULT 0",
                "first_name": "TEXT DEFAULT ''",
                "last_name": "TEXT DEFAULT ''",
                "iban": "TEXT DEFAULT ''",
                "photo": "TEXT DEFAULT ''",
                "created_at": "TEXT",
                "tier_index": "INTEGER DEFAULT 0",
                "points_locked": "INTEGER DEFAULT 0",
            },
            "referrals": {
                "referrer_id": "INTEGER",
                "referred_id": "INTEGER",
                "status": "TEXT DEFAULT 'pending'",
                "created_at": "TEXT",
            },
            "activities": {
                "user_id": "INTEGER",
                "kind": "TEXT",
                "title": "TEXT",
                "description": "TEXT",
                "created_at": "TEXT",
            },
            "posts": {
                "user_id": "INTEGER",
                "type": "TEXT",
                "code": "TEXT",
                "description": "TEXT",
                "created_at": "TEXT",
            },
            "payouts": {
                "user_id": "INTEGER",
                "amount": "INTEGER",
                "status": "TEXT DEFAULT 'pending'",
                "created_at": "TEXT",
            },
        }
        for table, cols in to_add.items():
            for col, spec in cols.items():
                try:
                    pg_add_column(con, table, col, spec)
                    con.commit()
                except Exception:
                    con.rollback()
        con.commit()
    else:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                code TEXT NOT NULL UNIQUE,
                referred_by INTEGER,
                level TEXT NOT NULL DEFAULT 'START',
                points INTEGER NOT NULL DEFAULT 0,
                claimed INTEGER NOT NULL DEFAULT 0,
                paid INTEGER NOT NULL DEFAULT 0,
                ref_locked INTEGER NOT NULL DEFAULT 0,
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                iban TEXT DEFAULT '',
                photo TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                code TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            """
        )
        cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
        if "tier_index" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN tier_index INTEGER NOT NULL DEFAULT 0")
        if "points_locked" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN points_locked INTEGER NOT NULL DEFAULT 0")
        con.commit()
    con.close()


init_db()


def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def initials(name):
    parts = [p for p in re.split(r"\s+", name or "") if p]
    return ("".join(p[0] for p in parts)[:2].upper() or "C")


def demo_public(item):
    name = item["name"]
    pts = item["points"]
    return {
        "id": item["id"],
        "name": name,
        "username": item["username"],
        "initials": initials(name),
        "level": item["level"],
        "points": pts,
        "photo": "",
        "refs": max(1, pts // 4),
    }


def merge_members(real):
    seen = {(x.get("username") or "") for x in real}
    fake = [demo_public(x) for x in DEMO_MEMBERS if x["username"] not in seen]
    out = list(real) + fake
    out.sort(key=lambda x: (-int(x.get("points") or 0), x.get("name") or ""))
    return out


def make_code():
    a = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "COD-" + "".join(secrets.choice(a) for _ in range(6))


def is_iban(value):
    s = re.sub(r"\s+", "", str(value or "")).upper()
    return bool(re.match(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}$", s))


def user_level(user):
    return user["level"] if user["level"] in LEVELS else "START"


def tier_index_of(user):
    rewards = LEVELS[user_level(user)]["rewards"]
    try:
        idx = int(user["tier_index"] or 0)
    except (KeyError, TypeError, ValueError):
        idx = 0
    return max(0, min(idx, len(rewards) - 1))


def current_tier(user):
    rewards = LEVELS[user_level(user)]["rewards"]
    return rewards[tier_index_of(user)]


def is_locked(user):
    try:
        locked = int(user["points_locked"] or 0)
    except (KeyError, TypeError, ValueError):
        locked = 0
    tier = current_tier(user)
    return bool(locked or user["points"] >= tier["points"])


def reward_state(user):
    level = user_level(user)
    idx = tier_index_of(user)
    rewards = LEVELS[level]["rewards"]
    tier = rewards[idx]
    locked = is_locked(user)
    nxt = None if locked else tier
    return {
        "current": tier["reward"] if locked else 0,
        "current_points": tier["points"],
        "next": nxt,
        "locked": locked,
        "mustWithdraw": locked,
        "tierIndex": idx,
        "tierNumber": idx + 1,
        "tierLabel": TIER_LABELS[idx] if idx < len(TIER_LABELS) else str(idx + 1),
        "level": level,
    }


def progress_of(user):
    tier = current_tier(user)
    if is_locked(user):
        return 100
    return min(100, round((user["points"] / tier["points"]) * 100)) if tier["points"] else 0


def user_by_id(con, user_id):
    return con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def add_activity(con, user_id, kind, title, description=""):
    con.execute(
        "INSERT INTO activities (user_id, kind, title, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, kind, title, description, now()),
    )


def ensure_admin():
    con = db()
    try:
        email = ADMIN_EMAIL
        existing = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        password = ADMIN_PASSWORD or secrets.token_hex(16)
        if existing:
            con.execute("UPDATE users SET paid=1, ref_locked=1 WHERE email=?", (email,))
            if ADMIN_PASSWORD:
                con.execute(
                    "UPDATE users SET password_hash=? WHERE email=?",
                    (generate_password_hash(ADMIN_PASSWORD), email),
                )
            con.commit()
            return
        con.execute(
            """INSERT INTO users
               (name,username,email,password_hash,code,referred_by,level,points,claimed,paid,ref_locked,created_at)
               VALUES (?,?,?,?,?,NULL,'ELITE',0,0,1,1,?)""",
            ("Admin COD-IA", "admin", email, generate_password_hash(password), "COD-ADMIN1", now()),
        )
        uid = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        add_activity(con, uid, "info", "Compte admin créé", "Accès direct")
        con.commit()
    except Exception:
        con.rollback()
        app.logger.exception("ensure_admin failed")
    finally:
        con.close()


ensure_admin()


def public_user(con, user):
    refs = con.execute(
        "SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='validated'",
        (user["id"],),
    ).fetchone()["c"]
    return {
        "id": user["id"],
        "name": user["name"],
        "username": user["username"],
        "initials": initials(user["name"]),
        "level": user_level(user),
        "points": user["points"],
        "photo": user["photo"] or "",
        "refs": refs,
        "firstName": user["first_name"] or (user["name"] or "").split(" ")[0],
        "lastName": user["last_name"] or "",
    }


def dashboard(con, user):
    validated = con.execute(
        "SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='validated'",
        (user["id"],),
    ).fetchone()["c"]
    higher = con.execute("SELECT COUNT(*) c FROM users WHERE paid=1 AND points > ?", (user["points"],)).fetchone()["c"]
    demo_higher = len([x for x in DEMO_MEMBERS if x["points"] > (user["points"] or 0)])
    reward = reward_state(user)
    return {
        "user": {
            "id": user["id"],
            "name": user["name"],
            "username": user["username"],
            "initials": initials(user["name"]),
            "level": user_level(user),
            "points": user["points"],
            "code": user["code"],
            "photo": user["photo"] or "",
            "firstName": user["first_name"] or "",
            "lastName": user["last_name"] or "",
            "iban": user["iban"] or "",
            "claimed": user["claimed"] or 0,
            "paid": bool(user["paid"]),
            "refLocked": bool(user["ref_locked"]),
            "pointsLocked": reward["locked"],
            "tierIndex": reward["tierIndex"],
        },
        "referrals": {"validated": validated},
        "ranking": {"position": higher + demo_higher + 1 if user["paid"] else None},
        "reward": reward,
        "wallet": {"available": reward["current"]},
        "progress": progress_of(user),
    }


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    con = db()
    user = user_by_id(con, uid)
    con.close()
    return user


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"message": "Non connecté"}), 401
        return fn(user, *args, **kwargs)
    return wrapper


def require_paid(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"message": "Non connecté"}), 401
        if not user["paid"]:
            return jsonify({"message": "Paiement requis", "needPay": True}), 402
        return fn(user, *args, **kwargs)
    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return jsonify({"message": "Admin requis"}), 401
        return fn(*args, **kwargs)
    return wrapper


def grant_point(con, referrer_id, referred_name=""):
    referrer = user_by_id(con, referrer_id)
    if not referrer:
        return False
    if is_locked(referrer):
        add_activity(
            con,
            referrer_id,
            "info",
            "Parrainage non comptabilisé",
            "Palier atteint : retire ta récompense pour que les points comptent à nouveau.",
        )
        return False
    tier = current_tier(referrer)
    new_points = referrer["points"] + 1
    locked = 1 if new_points >= tier["points"] else 0
    con.execute("UPDATE users SET points=?, points_locked=? WHERE id=?", (new_points, locked, referrer_id))
    add_activity(con, referrer_id, "point", "Parrainage validé", referred_name)
    if locked:
        label = TIER_LABELS[tier_index_of(referrer)] if tier_index_of(referrer) < len(TIER_LABELS) else str(tier_index_of(referrer) + 1)
        add_activity(
            con,
            referrer_id,
            "palier",
            f"Vous avez franchi un {label} palier",
            "Retrait obligatoire. Les points sont bloqués tant que la récompense n’est pas retirée.",
        )
    return True


def validate_referral(con, user):
    if not user["paid"] or not user["referred_by"]:
        return False
    ref = con.execute("SELECT * FROM referrals WHERE referred_id=?", (user["id"],)).fetchone()
    if not ref or ref["status"] == "validated":
        return False
    con.execute("UPDATE referrals SET status='validated' WHERE id=?", (ref["id"],))
    grant_point(con, ref["referrer_id"], user["name"])
    return True


def mark_paid_user(con, user):
    if not user:
        return user
    if user["paid"]:
        return user_by_id(con, user["id"])
    con.execute("UPDATE users SET paid=1 WHERE id=?", (user["id"],))
    add_activity(con, user["id"], "info", "Entrée payée", "Stripe")
    con.commit()
    user = user_by_id(con, user["id"])
    validate_referral(con, user)
    con.commit()
    return user_by_id(con, user["id"])


def stripe_obj_to_dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    try:
        return obj.to_dict()
    except Exception:
        try:
            return dict(obj)
        except Exception:
            return {}


def mark_paid_from_session(session_obj):
    s = stripe_obj_to_dict(session_obj)
    if not s:
        return None
    status = s.get("payment_status")
    state = s.get("status")
    if status not in ("paid", "no_payment_required") and state != "complete":
        return None
    con = db()
    user = None
    meta = s.get("metadata") or {}
    if not isinstance(meta, dict):
        try:
            meta = dict(meta)
        except Exception:
            meta = {}
    uid = s.get("client_reference_id") or meta.get("user_id")
    if uid:
        try:
            user = user_by_id(con, int(uid))
        except Exception:
            user = None
    if not user:
        details = s.get("customer_details") or {}
        if not isinstance(details, dict):
            try:
                details = dict(details)
            except Exception:
                details = {}
        email = (details.get("email") or s.get("customer_email") or "").lower()
        if email:
            user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if user:
        user = mark_paid_user(con, user)
    con.close()
    return user


@app.get("/")
def landing():
    path = os.path.join(BASE, "landing.html")
    if os.path.exists(path):
        return send_from_directory(BASE, "landing.html")
    return send_from_directory(BASE, "index.html")


@app.get("/app")
def spa():
    return send_from_directory(BASE, "index.html")


@app.get("/pay/success")
def pay_success_page():
    session_id = request.args.get("session_id") or ""
    if session_id and stripe.api_key:
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            user = mark_paid_from_session(s)
            if user:
                session["uid"] = user["id"]
                session.permanent = True
        except Exception:
            app.logger.exception("pay_success retrieve failed")
    return redirect("/app")


@app.get("/api/config")
def config():
    return jsonify({"levels": LEVELS, "brand": "COD-IA", "payoutHours": 48, "accessPrice": 9.99})


@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "Membre").strip()
    username = (data.get("username") or "").strip().lower()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if len(username) < 3 or "@" not in email or len(password) < 8:
        return jsonify({"message": "Champs invalides"}), 400
    con = db()
    try:
        code = make_code()
        while con.execute("SELECT id FROM users WHERE code=?", (code,)).fetchone():
            code = make_code()
        if USE_PG:
            row = con.execute(
                """INSERT INTO users (name,username,email,password_hash,code,referred_by,level,points,claimed,paid,ref_locked,created_at)
                   VALUES (?,?,?,?,?,NULL,'START',0,0,0,0,?) RETURNING id""",
                (name, username, email, generate_password_hash(password), code, now()),
            ).fetchone()
            uid = row["id"]
        else:
            cur = con.execute(
                """INSERT INTO users (name,username,email,password_hash,code,referred_by,level,points,claimed,paid,ref_locked,created_at)
                   VALUES (?,?,?,?,?,NULL,'START',0,0,0,0,?)""",
                (name, username, email, generate_password_hash(password), code, now()),
            )
            uid = cur.lastrowid
        add_activity(con, uid, "info", "Compte créé", "En attente de paiement")
        add_activity(con, uid, "rules", "Règles du Parrainage", "Appuie pour lire les règles")
        con.commit()
        user = user_by_id(con, uid)
        session["uid"] = uid
        session.permanent = True
        return jsonify({"dashboard": dashboard(con, user), "needPay": True})
    except Exception as e:
        con.rollback()
        con.close()
        if is_unique_error(e):
            return jsonify({"message": "Email ou nom d’utilisateur déjà utilisé"}), 400
        raise


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip().lower()
    password = data.get("password") or ""
    con = db()
    user = con.execute("SELECT * FROM users WHERE email=? OR username=?", (identifier, identifier)).fetchone()
    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        con.close()
        return jsonify({"message": "Identifiants incorrects"}), 400
    session["uid"] = user["id"]
    session.permanent = True
    payload = dashboard(con, user)
    con.close()
    return jsonify({"dashboard": payload, "needPay": not bool(user["paid"])})


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def me():
    user = current_user()
    if not user:
        return jsonify({"authenticated": False, "paid": False})
    con = db()
    payload = dashboard(con, user)
    con.close()
    return jsonify({"authenticated": True, "paid": bool(user["paid"]), "dashboard": payload, "needPay": not bool(user["paid"])})


@app.get("/api/pay/config")
def pay_config():
    return jsonify({"pk": STRIPE_PK})


@app.post("/api/pay/session")
@require_auth
def pay_session(user):
    if user["paid"]:
        return jsonify({"paid": True})
    if not stripe.api_key or not STRIPE_PK:
        return jsonify({"message": "Stripe non configuré"}), 500
    session_obj = stripe.checkout.Session.create(
        ui_mode="embedded_page",
        mode="payment",
        customer_email=user["email"],
        client_reference_id=str(user["id"]),
        metadata={"user_id": str(user["id"]), "email": user["email"]},
        line_items=[{
            "price_data": {
                "currency": "eur",
                "unit_amount": 999,
                "product_data": {"name": "Accès COD-IA"},
            },
            "quantity": 1,
        }],
        return_url=APP_URL + "/pay/success?session_id={CHECKOUT_SESSION_ID}",
    )
    return jsonify({"clientSecret": session_obj.client_secret, "paid": False})


@app.get("/api/pay/confirm")
def pay_confirm():
    session_id = request.args.get("session_id") or ""
    user = current_user()
    if session_id and stripe.api_key:
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            paid_user = mark_paid_from_session(s)
            if paid_user:
                session["uid"] = paid_user["id"]
                session.permanent = True
                user = paid_user
        except Exception:
            app.logger.exception("pay_confirm stripe retrieve failed")
    if not user:
        return jsonify({"ok": False, "paid": False, "message": "Session expirée. Reconnecte-toi, sans repayer."}), 401
    con = db()
    user = user_by_id(con, user["id"])
    payload = dashboard(con, user)
    paid = bool(user["paid"])
    con.close()
    return jsonify({"ok": True, "paid": paid, "dashboard": payload})


@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return jsonify({"message": "Webhook invalide"}), 400
    etype = event.get("type")
    obj = event.get("data", {}).get("object")
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        mark_paid_from_session(obj)
    return jsonify({"ok": True})


@app.post("/api/referral/attach")
@require_auth
def attach_referral(user):
    if not user["paid"]:
        return jsonify({"message": "Paiement requis"}), 402
    if user["ref_locked"]:
        return jsonify({"message": "Code déjà enregistré", "counted": False})
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if not code:
        con = db()
        con.execute("UPDATE users SET ref_locked=1 WHERE id=?", (user["id"],))
        con.commit()
        payload = dashboard(con, user_by_id(con, user["id"]))
        con.close()
        return jsonify({"ok": True, "counted": False, "dashboard": payload})
    con = db()
    sponsor = con.execute("SELECT * FROM users WHERE code=?", (code,)).fetchone()
    if not sponsor or sponsor["id"] == user["id"]:
        con.close()
        return jsonify({"message": "Code parrain invalide"}), 400
    existing = con.execute("SELECT id FROM referrals WHERE referred_id=?", (user["id"],)).fetchone()
    if not existing:
        con.execute(
            "INSERT INTO referrals (referrer_id, referred_id, status, created_at) VALUES (?, ?, 'pending', ?)",
            (sponsor["id"], user["id"], now()),
        )
        con.execute("UPDATE users SET referred_by=? WHERE id=?", (sponsor["id"], user["id"]))
    con.execute("UPDATE users SET ref_locked=1 WHERE id=?", (user["id"],))
    user = user_by_id(con, user["id"])
    counted = validate_referral(con, user)
    con.commit()
    payload = dashboard(con, user_by_id(con, user["id"]))
    con.close()
    return jsonify({"ok": True, "counted": counted, "dashboard": payload})


@app.get("/api/activity")
@require_paid
def activity(user):
    con = db()
    has_rules = con.execute("SELECT id FROM activities WHERE user_id=? AND kind='rules'", (user["id"],)).fetchone()
    if not has_rules:
        add_activity(con, user["id"], "rules", "Règles du Parrainage", "Appuie pour lire les règles")
        con.commit()
    rows = con.execute(
        "SELECT kind, title, description FROM activities WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (user["id"],),
    ).fetchall()
    con.close()
    items = [dict(r) for r in rows]
    items.sort(key=lambda a: 0 if a.get("kind") == "rules" else 1)
    return jsonify({"activities": items})


@app.get("/api/members")
@require_paid
def members(user):
    q = (request.args.get("q") or "").strip().lower()
    con = db()
    rows = con.execute(
        "SELECT * FROM users WHERE paid=1 AND (name LIKE ? OR username LIKE ?) ORDER BY points DESC, id ASC LIMIT 50",
        (f"%{q}%", f"%{q}%"),
    ).fetchall()
    real = [public_user(con, r) for r in rows]
    con.close()
    out = merge_members(real)
    if q:
        out = [x for x in out if q in (x.get("name") or "").lower() or q in (x.get("username") or "").lower()]
    return jsonify({"members": out[:50]})


@app.get("/api/ranking")
@require_paid
def ranking(user):
    con = db()
    rows = con.execute("SELECT * FROM users WHERE paid=1 ORDER BY points DESC, id ASC LIMIT 50").fetchall()
    real = [public_user(con, r) for r in rows]
    con.close()
    all_rows = merge_members(real)
    ranking_list = []
    my_pos = None
    for i, item in enumerate(all_rows[:10], start=1):
        item["position"] = i
        ranking_list.append(item)
        if item.get("id") == user["id"]:
            my_pos = i
    return jsonify({"me": {"position": my_pos or 1, "points": user["points"]}, "ranking": ranking_list})


@app.get("/api/posts")
@require_paid
def list_posts(user):
    con = db()
    rows = con.execute(
        """SELECT p.id, p.user_id, p.type, p.code, p.description, p.created_at, u.name
           FROM posts p JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 100"""
    ).fetchall()
    con.close()
    return jsonify({"posts": [{"id": r["id"], "userId": r["user_id"], "type": r["type"], "code": r["code"], "desc": r["description"], "by": r["name"], "at": r["created_at"]} for r in rows]})


@app.post("/api/posts")
@require_paid
def create_post(user):
    data = request.get_json(silent=True) or {}
    ptype, code, desc = data.get("type"), (data.get("code") or "").strip().upper(), (data.get("desc") or "").strip()
    if ptype not in ("promo", "parrainage") or len(code) < 4 or len(desc) < 8:
        return jsonify({"message": "Publication invalide"}), 400
    con = db()
    con.execute(
        "INSERT INTO posts (user_id, type, code, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (user["id"], ptype, code, desc, now()),
    )
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.delete("/api/posts/<int:post_id>")
@require_paid
def delete_post(user, post_id):
    con = db()
    row = con.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row or row["user_id"] != user["id"]:
        con.close()
        return jsonify({"message": "Impossible de supprimer"}), 403
    con.execute("DELETE FROM posts WHERE id=?", (post_id,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.post("/api/profile")
@require_paid
def update_profile(user):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or user["username"]).strip().lower()
    photo = data.get("photo") if data.get("photo") is not None else user["photo"]
    if len(username) < 3:
        return jsonify({"message": "Username trop court"}), 400
    con = db()
    try:
        con.execute("UPDATE users SET username=?, photo=? WHERE id=?", (username, photo or "", user["id"]))
        con.commit()
        return jsonify({"dashboard": dashboard(con, user_by_id(con, user["id"]))})
    except Exception as e:
        con.rollback()
        if is_unique_error(e):
            return jsonify({"message": "Username déjà utilisé"}), 400
        raise
    finally:
        con.close()


@app.post("/api/profile/bank")
@require_paid
def update_bank(user):
    data = request.get_json(silent=True) or {}
    first = (data.get("firstName") or "").strip()
    last = (data.get("lastName") or "").strip()
    iban = re.sub(r"\s+", "", data.get("iban") or "").upper()
    if len(first) < 2 or len(last) < 2 or not is_iban(iban):
        return jsonify({"message": "Coordonnées invalides"}), 400
    con = db()
    con.execute(
        "UPDATE users SET first_name=?, last_name=?, iban=?, name=? WHERE id=?",
        (first, last, iban, f"{first} {last}", user["id"]),
    )
    con.commit()
    payload = dashboard(con, user_by_id(con, user["id"]))
    con.close()
    return jsonify({"dashboard": payload})


@app.post("/api/payout")
@require_paid
def payout(user):
    state = reward_state(user)
    amount = state["current"]
    first = (user["first_name"] or "").strip()
    last = (user["last_name"] or "").strip()
    iban = re.sub(r"\s+", "", user["iban"] or "").upper()
    if amount <= 0 or not state["locked"]:
        return jsonify({"message": "Aucune récompense à retirer. Un palier doit d’abord être atteint."}), 400
    if len(first) < 2 or len(last) < 2 or not is_iban(iban):
        return jsonify({"message": "Coordonnées bancaires manquantes"}), 400
    if not SMTP_USER or not SMTP_PASS:
        return jsonify({"message": "SMTP non configuré"}), 500
    msg = EmailMessage()
    msg["Subject"] = f"Demande de retrait COD-IA — {first} {last}"
    msg["From"] = SMTP_USER
    msg["To"] = PAYOUT_TO
    msg.set_content(
        f"Prénom : {first}\nNom : {last}\nIBAN : {iban}\nRécompense : {amount} €\n"
        f"Points du palier : {user['points']}\nNiveau : {user_level(user)}\n"
        f"Palier : {state['tierNumber']}\nCode : {user['code']}\nDélai : 10 jours\n"
    )
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception:
        return jsonify({"message": "Envoi mail impossible"}), 500

    level = user_level(user)
    idx = tier_index_of(user) + 1
    rewards = LEVELS[level]["rewards"]
    unlocked = None
    if idx >= len(rewards):
        li = LEVEL_ORDER.index(level)
        if li < len(LEVEL_ORDER) - 1:
            level = LEVEL_ORDER[li + 1]
            idx = 0
            unlocked = level
        else:
            idx = len(rewards) - 1

    con = db()
    con.execute(
        "UPDATE users SET points=0, points_locked=0, claimed=?, tier_index=?, level=? WHERE id=?",
        ((user["claimed"] or 0) + amount, idx, level, user["id"]),
    )
    con.execute(
        "INSERT INTO payouts (user_id, amount, status, created_at) VALUES (?, ?, 'pending', ?)",
        (user["id"], amount, now()),
    )
    add_activity(con, user["id"], "money", "Retrait demandé", f"{amount} € · points remis à 0 · 10 jours")
    if unlocked:
        add_activity(con, user["id"], "info", "Niveau débloqué", f"Tu passes au niveau {unlocked}")
    con.commit()
    payload = dashboard(con, user_by_id(con, user["id"]))
    con.close()
    return jsonify({"ok": True, "dashboard": payload})


@app.get("/admin")
@app.get("/admin/payouts")
def admin_page():
    path = os.path.join(BASE, "admin.html")
    if os.path.exists(path):
        return send_from_directory(BASE, "admin.html")
    return jsonify({"message": "admin.html manquant"}), 404


@app.get("/api/admin/me")
def admin_me():
    if not session.get("admin"):
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "email": session.get("admin_email")})


@app.post("/api/admin/login")
def admin_login():
    if not ADMIN_PASSWORD:
        return jsonify({"message": "ADMIN_PASSWORD manquant sur Railway"}), 500
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if email != ADMIN_EMAIL or password != ADMIN_PASSWORD:
        return jsonify({"message": "Identifiants admin incorrects"}), 400
    session["admin"] = True
    session["admin_email"] = email
    return jsonify({"ok": True})


@app.post("/api/admin/logout")
def admin_logout():
    session.pop("admin", None)
    session.pop("admin_email", None)
    return jsonify({"ok": True})


@app.get("/api/admin/payouts")
@require_admin
def admin_payouts():
    status = (request.args.get("status") or "pending").strip().lower()
    con = db()
    sql = """SELECT p.id, p.amount, p.status, p.created_at,
                    u.name, u.email, u.iban, u.first_name, u.last_name,
                    u.level, u.code, u.points
             FROM payouts p JOIN users u ON u.id=p.user_id"""
    if status == "all":
        rows = con.execute(sql + " ORDER BY p.id DESC LIMIT 200").fetchall()
    else:
        rows = con.execute(sql + " WHERE p.status=? ORDER BY p.id DESC LIMIT 200", (status,)).fetchall()
    con.close()
    return jsonify({
        "payouts": [{
            "id": r["id"], "amount": r["amount"], "status": r["status"], "createdAt": r["created_at"],
            "name": r["name"], "email": r["email"], "iban": r["iban"],
            "firstName": r["first_name"] or "", "lastName": r["last_name"] or "",
            "level": r["level"], "code": r["code"], "points": r["points"],
        } for r in rows]
    })


@app.post("/api/admin/payouts/<int:payout_id>/paid")
@require_admin
def admin_mark_paid(payout_id):
    con = db()
    row = con.execute("SELECT * FROM payouts WHERE id=?", (payout_id,)).fetchone()
    if not row:
        con.close()
        return jsonify({"message": "Demande introuvable"}), 404
    if row["status"] == "paid":
        con.close()
        return jsonify({"ok": True, "already": True})
    con.execute("UPDATE payouts SET status='paid' WHERE id=?", (payout_id,))
    add_activity(con, row["user_id"], "money", "Virement envoyé", f"{row['amount']} €")
    con.commit()
    con.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)), debug=True)
