import json
import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg2
import psycopg2.extras
import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import stripe
except ImportError:
    stripe = None

logging.basicConfig(level=logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=90)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PREFERRED_URL_SCHEME="https",
)

DATABASE_URL = os.getenv("DATABASE_URL", "")
SERVER_URL = os.getenv("SERVER_URL", "https://cod-ia.fr").rstrip("/")

ADMIN_EMAILS = {
    x.strip().lower()
    for x in os.getenv("ADMIN_EMAILS", "contact@cod-ia.fr").split(",")
    if x.strip()
}
ADMIN_IDS = {
    x.strip()
    for x in os.getenv("ADMIN_IDS", "8091031583,6886937051").split(",")
    if x.strip()
}

try:
    PRICE_CENTS = int(os.getenv("PRICE_CENTS", "999"))
except ValueError:
    PRICE_CENTS = 999

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_UI_MODE = os.getenv("STRIPE_UI_MODE", "embedded_page")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
ADMIN_TELEGRAM_ID = os.getenv("ADMIN_ID", "")

if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL est manquante.")
    return psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
        sslmode="require",
    )


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    if not dt:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def normalize_email(value):
    return (value or "").strip().lower()


def clean_username(value):
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_.-]", "", value)
    return value[:30]


def send_telegram_message(chat_id, text):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as exc:
        logging.error("Telegram send error: %s", exc)


def ensure_column(cur, table, column, definition):
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s AND column_name=%s
        """,
        (table, column),
    )
    if not cur.fetchone():
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        logging.info("Colonne ajoutée: %s.%s", table, column)


def init_db():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    bio TEXT DEFAULT '',
                    avatar_initials TEXT DEFAULT 'CO',
                    referral_code TEXT UNIQUE NOT NULL,
                    referred_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
                    telegram_id TEXT UNIQUE,
                    is_admin BOOLEAN DEFAULT FALSE,
                    is_paid BOOLEAN DEFAULT FALSE,
                    stripe_customer_id TEXT,
                    stripe_session_id TEXT,
                    hidden_codes JSONB DEFAULT '[]'::jsonb,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
            ensure_column(cur, "users", "password_hash", "TEXT")
            ensure_column(cur, "users", "display_name", "TEXT")
            ensure_column(cur, "users", "bio", "TEXT DEFAULT ''")
            ensure_column(cur, "users", "avatar_initials", "TEXT DEFAULT 'CO'")
            ensure_column(cur, "users", "referral_code", "TEXT")
            ensure_column(cur, "users", "referred_by", "BIGINT")
            ensure_column(cur, "users", "telegram_id", "TEXT")
            ensure_column(cur, "users", "is_admin", "BOOLEAN DEFAULT FALSE")
            ensure_column(cur, "users", "is_paid", "BOOLEAN DEFAULT FALSE")
            ensure_column(cur, "users", "stripe_customer_id", "TEXT")
            ensure_column(cur, "users", "stripe_session_id", "TEXT")
            ensure_column(cur, "users", "hidden_codes", "JSONB DEFAULT '[]'::jsonb")
            ensure_column(cur, "users", "updated_at", "TIMESTAMPTZ DEFAULT NOW()")
            ensure_column(cur, "users", "created_at", "TIMESTAMPTZ DEFAULT NOW()")
            ensure_column(cur, "codes", "kind", "TEXT DEFAULT 'PROMO'")
            ensure_column(cur, "codes", "category", "TEXT DEFAULT 'Autres'")
            ensure_column(cur, "codes", "brand", "TEXT DEFAULT ''")
            ensure_column(cur, "codes", "title", "TEXT")
            ensure_column(cur, "codes", "description", "TEXT DEFAULT ''")
            ensure_column(cur, "codes", "code", "TEXT DEFAULT ''")
            ensure_column(cur, "codes", "url", "TEXT DEFAULT ''")
            ensure_column(cur, "codes", "image_url", "TEXT DEFAULT ''")
            ensure_column(cur, "codes", "expires_at", "TIMESTAMPTZ")
            ensure_column(cur, "codes", "status", "TEXT DEFAULT 'VALIDEE'")
            ensure_column(cur, "codes", "likes_count", "INTEGER DEFAULT 0")
            ensure_column(cur, "codes", "copies_count", "INTEGER DEFAULT 0")
            ensure_column(cur, "codes", "clicks_count", "INTEGER DEFAULT 0")
            ensure_column(cur, "codes", "reports_count", "INTEGER DEFAULT 0")
            ensure_column(cur, "codes", "copy_reward_awarded", "BOOLEAN DEFAULT FALSE")
            ensure_column(cur, "codes", "created_at", "TIMESTAMPTZ DEFAULT NOW()")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS codes (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL DEFAULT 'PROMO',
                    category TEXT DEFAULT 'Autres',
                    brand TEXT DEFAULT '',
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    code TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    image_url TEXT DEFAULT '',
                    expires_at TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'VALIDEE',
                    likes_count INTEGER DEFAULT 0,
                    copies_count INTEGER DEFAULT 0,
                    clicks_count INTEGER DEFAULT 0,
                    reports_count INTEGER DEFAULT 0,
                    copy_reward_awarded BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS likes (
                    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    code_id BIGINT REFERENCES codes(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY(user_id, code_id)
                );
                CREATE TABLE IF NOT EXISTS favorites (
                    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    code_id BIGINT REFERENCES codes(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY(user_id, code_id)
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    code_id BIGINT REFERENCES codes(id) ON DELETE CASCADE,
                    reason TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, code_id)
                );
                CREATE TABLE IF NOT EXISTS follows (
                    follower_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    following_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY(follower_id, following_id),
                    CHECK(follower_id <> following_id)
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    type TEXT DEFAULT 'INFO',
                    is_read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS support_tickets (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    subject TEXT NOT NULL,
                    message TEXT NOT NULL,
                    admin_reply TEXT,
                    status TEXT DEFAULT 'OPEN',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS badges (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                    badge_key TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(user_id, badge_key)
                );
                CREATE TABLE IF NOT EXISTS search_logs (
                    q TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                );
                """
            )
            challenge_start = os.getenv("CHALLENGE_START_AT")
            cur.execute(
                """
                INSERT INTO settings(key, value)
                VALUES('challenge_start', %s)
                ON CONFLICT(key) DO NOTHING
                """,
                (challenge_start or now_utc().isoformat(),),
            )
            for email in ADMIN_EMAILS:
                cur.execute(
                    "UPDATE users SET is_admin=TRUE, is_paid=TRUE WHERE LOWER(email)=%s",
                    (email,),
                )
            for telegram_id in ADMIN_IDS:
                cur.execute(
                    "UPDATE users SET is_admin=TRUE, is_paid=TRUE WHERE telegram_id=%s",
                    (telegram_id,),
                )
        conn.commit()
        logging.info("DB initialisée")
    finally:
        conn.close()


def make_referral_code():
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(30):
        code = "CODIA" + "".join(secrets.choice(alphabet) for _ in range(6))
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE referral_code=%s", (code,))
                if not cur.fetchone():
                    return code
        finally:
            conn.close()
    raise RuntimeError("Impossible de créer un code de parrainage unique.")


def is_admin(user):
    if not user:
        return False
    return (
        bool(user.get("is_admin"))
        or normalize_email(user.get("email")) in ADMIN_EMAILS
        or str(user.get("telegram_id") or "") in ADMIN_IDS
    )


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            user = cur.fetchone()
        if user and is_admin(user) and not user.get("is_admin"):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET is_admin=TRUE, is_paid=TRUE WHERE id=%s",
                    (user_id,),
                )
            conn.commit()
            user["is_admin"] = True
            user["is_paid"] = True
        return user
    finally:
        conn.close()


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"ok": False, "success": False, "error": "AUTH_REQUIRED"}), 401
        return fn(user, *args, **kwargs)
    return wrapper


def json_error(message, status=400):
    return jsonify({"ok": False, "success": False, "error": message}), status


def create_notification(cur, user_id, title, message, kind="INFO"):
    cur.execute(
        """
        INSERT INTO notifications(user_id, title, message, type)
        VALUES(%s, %s, %s, %s)
        """,
        (user_id, title, message, kind),
    )


def user_stats(user_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE referred_by=%s", (user_id,))
            referrals = int(cur.fetchone()["c"] or 0)
            cur.execute(
                """
                SELECT COALESCE(SUM(likes_count),0) AS likes,
                       COALESCE(SUM(clicks_count),0) AS clicks,
                       COALESCE(SUM(copies_count),0) AS copies
                FROM codes WHERE user_id=%s
                """,
                (user_id,),
            )
            row = cur.fetchone() or {}
        return {
            "referrals": referrals,
            "points": referrals,
            "likes": int(row.get("likes") or 0),
            "clicks": int(row.get("clicks") or 0),
            "copies": int(row.get("copies") or 0),
        }
    finally:
        conn.close()


def challenge_start():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key='challenge_start'")
            row = cur.fetchone()
        if not row:
            return now_utc()
        try:
            return datetime.fromisoformat(row["value"].replace("Z", "+00:00"))
        except Exception:
            return now_utc()
    finally:
        conn.close()


def challenge_info(user_id=None):
    start = challenge_start()
    end = start + timedelta(days=21)
    now = now_utc()
    conn = db()
    try:
        with conn.cursor() as cur:
            if user_id:
                cur.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM users
                    WHERE referred_by=%s AND created_at >= %s AND created_at <= %s
                    """,
                    (user_id, start, end),
                )
                points = int(cur.fetchone()["total"] or 0)
            else:
                points = 0
            if points >= 1500:
                level, reward = "OR", 1500
            elif points >= 1000:
                level, reward = "ARGENT", 1000
            elif points >= 500:
                level, reward = "BRONZE", 500
            else:
                level, reward = "EN COURSE", 0
            cur.execute(
                """
                SELECT COUNT(*) + 1 AS rank
                FROM (
                    SELECT u.id, COUNT(r.id) AS referrals
                    FROM users u
                    LEFT JOIN users r
                      ON r.referred_by=u.id
                     AND r.created_at >= %s AND r.created_at <= %s
                    GROUP BY u.id
                    HAVING COUNT(r.id) > %s
                ) ranking
                """,
                (start, end, points),
            )
            rank = int(cur.fetchone()["rank"] or 1)
        return {
            "start": iso(start),
            "end": iso(end),
            "active": now < end,
            "finished": now >= end,
            "remaining_seconds": max(0, int((end - now).total_seconds())),
            "points": points,
            "rank": rank,
            "level": level,
            "reward": reward,
            "rule": "Lorsqu'un utilisateur rejoint COD.IA et utilise ton code de parrainage, 1 point t'est attribué.",
            "bronze": {"target": 500, "reward": 500},
            "silver": {"target": 1000, "reward": 1000},
            "gold": {"target": 1500, "reward": 1500},
        }
    finally:
        conn.close()


def serialize_code(row, current_user_id=None):
    conn = db()
    try:
        liked = favorite = False
        author = {}
        with conn.cursor() as cur:
            if current_user_id:
                cur.execute(
                    "SELECT 1 FROM likes WHERE user_id=%s AND code_id=%s",
                    (current_user_id, row["id"]),
                )
                liked = bool(cur.fetchone())
                cur.execute(
                    "SELECT 1 FROM favorites WHERE user_id=%s AND code_id=%s",
                    (current_user_id, row["id"]),
                )
                favorite = bool(cur.fetchone())
            cur.execute(
                "SELECT id, username, display_name, avatar_initials FROM users WHERE id=%s",
                (row["user_id"],),
            )
            author = cur.fetchone() or {}
        expires = row.get("expires_at")
        expired = bool(expires) and expires <= now_utc()
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "kind": row["kind"],
            "type": "parrainage" if row["kind"] == "PARRAINAGE" else "promo",
            "category": row["category"],
            "brand": row["brand"],
            "site": row["brand"] or row["title"],
            "title": row["title"],
            "description": row["description"],
            "code": row["code"],
            "url": row["url"],
            "image_url": row.get("image_url"),
            "added_by": author.get("display_name") or author.get("username") or "Membre",
            "expires_at": iso(expires),
            "expired": expired,
            "status": "EXPIREE" if expired else row["status"],
            "likes": row["likes_count"],
            "copies": row["copies_count"],
            "clicks": row["clicks_count"],
            "reports": row["reports_count"],
            "liked": liked,
            "favorite": favorite,
            "created_at": iso(row["created_at"]),
        }
    finally:
        conn.close()


@app.route("/")
def landing():
    return send_from_directory(BASE_DIR, "landing.html")


@app.route("/app")
@app.route("/miniapp")
def miniapp():
    return send_from_directory(BASE_DIR, "miniapp.html")


@app.route("/logout")
def logout_get():
    session.clear()
    return redirect("/")


@app.get("/config")
@app.get("/api/stripe-config")
def config():
    return jsonify({
        "ok": True,
        "stripe_pk": STRIPE_PUBLISHABLE_KEY,
        "publishable_key": STRIPE_PUBLISHABLE_KEY,
        "price_cents": PRICE_CENTS,
        "server_url": SERVER_URL,
        "ui_mode": STRIPE_UI_MODE,
    })


@app.get("/health")
@app.get("/stats")
def health():
    try:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            conn.close()
        return jsonify({"ok": True, "service": "COD.IA", "database": "connected"})
    except Exception as exc:
        return jsonify({"ok": False, "database": "error", "message": str(exc)}), 500


@app.get("/dev/reset-admin")
def reset_admin():
    email = "contact@cod-ia.fr"
    password = "CodiaAdmin2026!"
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, referral_code FROM users WHERE LOWER(email)=LOWER(%s)", (email,))
            row = cur.fetchone()
            pwd = generate_password_hash(password)
            if row:
                ref = row.get("referral_code") or make_referral_code()
                cur.execute(
                    """
                    UPDATE users
                    SET password_hash=%s,
                        is_admin=TRUE,
                        is_paid=TRUE,
                        username=COALESCE(NULLIF(username,''),'admin'),
                        display_name=COALESCE(NULLIF(display_name,''),'Admin COD.IA'),
                        referral_code=COALESCE(NULLIF(referral_code,''),%s)
                    WHERE id=%s
                    """,
                    (pwd, ref, row["id"]),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO users(
                        email, username, password_hash, display_name, avatar_initials,
                        referral_code, is_admin, is_paid
                    ) VALUES(%s,%s,%s,%s,%s,%s,TRUE,TRUE)
                    """,
                    (email, "admin", pwd, "Admin COD.IA", "AD", make_referral_code()),
                )
        conn.commit()
    finally:
        conn.close()
    return jsonify({
        "ok": True,
        "email": email,
        "password": password,
        "message": "Admin prêt. Supprime cette route ensuite.",
    })


@app.post("/api/register")
@app.post("/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email"))
    username = clean_username(data.get("username"))
    password = data.get("password") or ""
    display_name = (data.get("display_name") or username).strip()
    referral_code = (data.get("referral_code") or "").strip().upper()

    if len(email) < 5 or "@" not in email:
        return json_error("Adresse email invalide.")
    if len(username) < 3:
        return json_error("Nom utilisateur trop court.")
    if len(password) < 8:
        return json_error("Mot de passe : 8 caractères minimum.")
    if len(display_name) < 2:
        display_name = username

    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM users
                WHERE LOWER(email)=LOWER(%s) OR LOWER(username)=LOWER(%s)
                """,
                (email, username),
            )
            if cur.fetchone():
                return json_error("Cet email ou ce nom utilisateur existe déjà.", 409)

            referred_by = None
            if referral_code:
                cur.execute("SELECT id FROM users WHERE referral_code=%s", (referral_code,))
                referrer = cur.fetchone()
                if referrer:
                    referred_by = referrer["id"]

            admin = email in ADMIN_EMAILS
            cur.execute(
                """
                INSERT INTO users(
                    email, username, password_hash, display_name, avatar_initials,
                    referral_code, referred_by, is_admin, is_paid
                )
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, email, username
                """,
                (
                    email,
                    username,
                    generate_password_hash(password),
                    display_name,
                    "".join(x[0] for x in display_name.split() if x)[:2].upper() or "CO",
                    make_referral_code(),
                    referred_by,
                    admin,
                    admin,
                ),
            )
            created = cur.fetchone()
            user_id = created["id"]
            if referred_by:
                create_notification(
                    cur,
                    referred_by,
                    "Nouveau parrainage",
                    f"{display_name} a rejoint COD.IA avec ton code. +1 point.",
                    "REFERRAL",
                )
            create_notification(
                cur,
                user_id,
                "Bienvenue sur COD.IA",
                "Ton compte est créé. Bienvenue dans la communauté.",
                "WELCOME",
            )
        conn.commit()
    finally:
        conn.close()

    session.permanent = True
    session["user_id"] = user_id
    return jsonify({
        "ok": True,
        "success": True,
        "redirect": "/app",
        "user": {
            "id": user_id,
            "email": email,
            "username": username,
            "paid": admin,
            "is_paid": admin,
            "is_admin": admin,
        },
    })


@app.post("/api/login")
@app.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or data.get("login") or "").strip()
    password = data.get("password") or ""
    if not identifier or not password:
        return json_error("Identifiants incomplets.")

    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM users
                WHERE LOWER(email)=LOWER(%s) OR LOWER(username)=LOWER(%s)
                """,
                (identifier, identifier.lstrip("@")),
            )
            user = cur.fetchone()
    finally:
        conn.close()

    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        return json_error("Email/username ou mot de passe incorrect.", 401)

    session.permanent = True
    session["user_id"] = user["id"]
    admin = is_admin(user)
    paid = bool(user.get("is_paid")) or admin
    return jsonify({
        "ok": True,
        "success": True,
        "redirect": "/app",
        "user": {
            "id": user["id"],
            "email": user.get("email"),
            "username": user.get("username"),
            "paid": paid,
            "is_paid": paid,
            "is_admin": admin,
        },
    })


@app.post("/api/logout")
@app.post("/auth/logout")
def logout_post():
    session.clear()
    return jsonify({"ok": True, "success": True})


@app.get("/api/me")
@app.get("/me")
def me_any():
    user = get_current_user()
    if not user:
        return jsonify({"ok": True, "user": None, "paid": False, "is_admin": False})
    admin = is_admin(user)
    paid = bool(user.get("is_paid")) or admin
    challenge = challenge_info(user["id"])
    stats = user_stats(user["id"])
    return jsonify({
        "ok": True,
        "user": {
            "id": user["id"],
            "email": user.get("email"),
            "username": user.get("username"),
            "display_name": user.get("display_name"),
            "bio": user.get("bio") or "",
            "avatar_initials": user.get("avatar_initials") or "CO",
            "referral_code": user.get("referral_code"),
            "referral_link": f"{SERVER_URL}/app?ref={user.get('referral_code') or ''}",
            "is_admin": admin,
            "paid": paid,
            "is_paid": paid,
        },
        "paid": paid,
        "is_admin": admin,
        "stats": stats,
        "challenge": challenge,
    })


@app.get("/access")
def access():
    uid = request.args.get("user_id")
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE id::text=%s OR telegram_id=%s",
                (str(uid), str(uid)),
            )
            user = cur.fetchone()
        admin = is_admin(user) if user else str(uid) in ADMIN_IDS
        paid = admin or bool(user and user.get("is_paid"))
        return jsonify({"paid": paid, "is_admin": admin})
    finally:
        conn.close()


@app.post("/api/create-checkout")
@app.post("/create-embedded-checkout")
def create_checkout():
    user = get_current_user()
    data = request.get_json(silent=True) or {}
    if not user and not data.get("telegram_id"):
        return json_error("Connecte-toi d'abord.", 401)
    if user and (user.get("is_paid") or is_admin(user)):
        return jsonify({"ok": True, "already_paid": True})
    if not stripe or not STRIPE_SECRET_KEY:
        return json_error("Stripe n'est pas configuré sur Railway.", 503)

    try:
        line_items = (
            [{"price": STRIPE_PRICE_ID, "quantity": 1}]
            if STRIPE_PRICE_ID
            else [{
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": "Accès COD.IA", "description": "Accès unique à COD.IA"},
                    "unit_amount": PRICE_CENTS,
                },
                "quantity": 1,
            }]
        )
        params = {
            "mode": "payment",
            "line_items": line_items,
            "ui_mode": STRIPE_UI_MODE,
            "metadata": {
                "user_id": str(user["id"] if user else ""),
                "telegram_id": str(data.get("telegram_id") or ""),
                "email": user["email"] if user else "",
            },
        }
        if STRIPE_UI_MODE in ("embedded", "embedded_page"):
            params["return_url"] = f"{SERVER_URL}/app?paid=1&session_id={{CHECKOUT_SESSION_ID}}"
        else:
            params["success_url"] = f"{SERVER_URL}/app?paid=1"
            params["cancel_url"] = f"{SERVER_URL}/app"
        checkout = stripe.checkout.Session.create(**params)
        if user:
            conn = db()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET stripe_session_id=%s WHERE id=%s",
                        (checkout.id, user["id"]),
                    )
                conn.commit()
            finally:
                conn.close()
        return jsonify({
            "ok": True,
            "client_secret": getattr(checkout, "client_secret", None),
            "clientSecret": getattr(checkout, "client_secret", None),
            "session_id": checkout.id,
        })
    except Exception as exc:
        logging.error("Stripe error: %s", exc)
        return json_error(f"Stripe : {exc}", 500)


@app.post("/stripe/webhook")
def stripe_webhook():
    if not stripe:
        return "stripe unavailable", 503
    payload = request.data
    signature = request.headers.get("Stripe-Signature")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, signature, STRIPE_WEBHOOK_SECRET)
        else:
            event = stripe.Event.construct_from(request.json, stripe.api_key)
    except Exception:
        return "invalid webhook", 400

    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        checkout = event["data"]["object"]
        meta = checkout.get("metadata") or {}
        user_id = meta.get("user_id")
        telegram_id = meta.get("telegram_id")
        conn = db()
        try:
            with conn.cursor() as cur:
                if user_id:
                    cur.execute(
                        "UPDATE users SET is_paid=TRUE, stripe_session_id=%s WHERE id=%s",
                        (checkout.get("id"), int(user_id)),
                    )
                    create_notification(
                        cur, int(user_id),
                        "Bienvenue dans COD.IA",
                        "Ton accès COD.IA est maintenant actif.",
                        "PAYMENT",
                    )
                elif telegram_id:
                    cur.execute(
                        "UPDATE users SET is_paid=TRUE WHERE telegram_id=%s",
                        (str(telegram_id),),
                    )
            conn.commit()
        finally:
            conn.close()
    return "ok", 200


@app.get("/api/feed")
@require_auth
def feed(user):
    search = (request.args.get("search") or request.args.get("q") or "").strip()
    category = (request.args.get("category") or "TOUS").strip().upper()
    kind = (request.args.get("type") or request.args.get("kind") or "").strip().upper()
    mode = (request.args.get("mode") or "recent").strip()
    expiring = request.args.get("expiring")
    hidden = user.get("hidden_codes") or []

    conn = db()
    try:
        with conn.cursor() as cur:
            params = [user["id"]]
            query = """
                SELECT c.* FROM codes c
                WHERE c.status='VALIDEE'
                  AND (c.expires_at IS NULL OR c.expires_at > NOW())
                  AND NOT EXISTS (
                      SELECT 1 FROM reports r
                      WHERE r.user_id=%s AND r.code_id=c.id
                  )
            """
            if hidden:
                query += " AND c.id <> ALL(%s)"
                params.append(hidden)
            if category not in ("TOUS", ""):
                query += " AND UPPER(c.category)=UPPER(%s)"
                params.append(category)
            if kind in ("PROMO", "PARRAINAGE"):
                query += " AND c.kind=%s"
                params.append(kind)
            if expiring:
                query += " AND c.expires_at IS NOT NULL AND c.expires_at < NOW() + INTERVAL '4 days'"
            if search:
                query += """
                    AND (c.title ILIKE %s OR c.description ILIKE %s OR c.brand ILIKE %s OR c.code ILIKE %s)
                """
                term = f"%{search}%"
                params.extend([term, term, term, term])
            if mode == "top":
                query += " ORDER BY (c.likes_count + c.copies_count) DESC, c.created_at DESC"
            else:
                query += " ORDER BY c.created_at DESC"
            query += " LIMIT 80"
            cur.execute(query, params)
            rows = cur.fetchall()
        return jsonify({"ok": True, "codes": [serialize_code(row, user["id"]) for row in rows]})
    finally:
        conn.close()


@app.get("/codes")
def codes_compat():
    user = get_current_user()
    if not user:
        conn = db()
        try:
            with conn.cursor() as cur:
                q = "SELECT * FROM codes WHERE status='VALIDEE' ORDER BY created_at DESC LIMIT 80"
                cur.execute(q)
                rows = cur.fetchall()
            return jsonify({"codes": [serialize_code(row) for row in rows]})
        finally:
            conn.close()
    return feed(user)


@app.get("/api/top-codes")
@app.get("/codes/top")
def top_codes():
    user = get_current_user()
    uid = user["id"] if user else None
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM codes
                WHERE status='VALIDEE'
                  AND (likes_count >= 100 OR copies_count >= 100)
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY (likes_count + copies_count) DESC
                LIMIT 30
                """
            )
            rows = cur.fetchall()
        return jsonify({"ok": True, "codes": [serialize_code(row, uid) for row in rows]})
    finally:
        conn.close()


@app.post("/api/codes")
@app.post("/codes/add")
@require_auth
def create_code(user):
    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or data.get("type") or "PROMO").upper()
    if kind in ("PARRAINAGE",):
        kind = "PARRAINAGE"
    else:
        kind = "PROMO"
    title = (data.get("title") or data.get("site") or "").strip()
    code = (data.get("code") or "").strip()
    description = (data.get("description") or "").strip()
    url = (data.get("url") or "").strip()
    brand = (data.get("brand") or data.get("site") or title).strip()
    if len(title) < 2 or not code:
        return json_error("Site et code obligatoires.")
    expires = None
    if data.get("expires_at"):
        try:
            expires = datetime.fromisoformat(str(data["expires_at"]).replace("Z", "+00:00"))
        except Exception:
            expires = None
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO codes(user_id, kind, category, brand, title, description, code, url, expires_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING *
                """,
                (user["id"], kind, data.get("category") or "Autres", brand, title, description, code, url, expires),
            )
            row = cur.fetchone()
            create_notification(cur, user["id"], "Publication créée", "Ton code a bien été publié sur COD.IA.", "PUBLICATION")
        conn.commit()
        if CHANNEL_ID:
            send_telegram_message(CHANNEL_ID, f"{kind}\n{user.get('username')}\n{title}\n{code}")
        return jsonify({"ok": True, "success": True, "code": serialize_code(row, user["id"])})
    finally:
        conn.close()


@app.post("/api/codes/<int:code_id>/copy")
@app.post("/code/copy")
@require_auth
def copy_code(user, code_id=None):
    if code_id is None:
        code_id = int((request.get_json(silent=True) or {}).get("id") or 0)
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM codes WHERE id=%s", (code_id,))
            code = cur.fetchone()
            if not code:
                return json_error("Code introuvable.", 404)
            if code["expires_at"] and code["expires_at"] <= now_utc():
                return json_error("Ce code est expiré.", 410)
            cur.execute(
                "UPDATE codes SET copies_count=copies_count+1 WHERE id=%s RETURNING copies_count",
                (code_id,),
            )
            new_count = int(cur.fetchone()["copies_count"])
            if new_count >= 250 and not code.get("copy_reward_awarded"):
                cur.execute("UPDATE codes SET copy_reward_awarded=TRUE WHERE id=%s", (code_id,))
                create_notification(
                    cur,
                    code["user_id"],
                    "Récompense Copies",
                    f"Ton code « {code['title']} » a atteint 250 copies. Récompense : 100 €.",
                    "REWARD",
                )
        conn.commit()
        return jsonify({"ok": True, "copies": new_count, "code": code["code"], "url": code["url"]})
    finally:
        conn.close()


@app.post("/api/codes/<int:code_id>/like")
@require_auth
def like_code(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM likes WHERE user_id=%s AND code_id=%s", (user["id"], code_id))
            already = bool(cur.fetchone())
            if already:
                cur.execute("DELETE FROM likes WHERE user_id=%s AND code_id=%s", (user["id"], code_id))
                cur.execute(
                    "UPDATE codes SET likes_count=GREATEST(likes_count-1,0) WHERE id=%s RETURNING likes_count",
                    (code_id,),
                )
                liked = False
            else:
                cur.execute(
                    "INSERT INTO likes(user_id, code_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (user["id"], code_id),
                )
                cur.execute(
                    "UPDATE codes SET likes_count=likes_count+1 WHERE id=%s RETURNING likes_count",
                    (code_id,),
                )
                liked = True
            count = int(cur.fetchone()["likes_count"])
        conn.commit()
        return jsonify({"ok": True, "liked": liked, "likes": count, "value": count})
    finally:
        conn.close()


@app.post("/code/react")
@require_auth
def code_react(user):
    data = request.get_json(silent=True) or {}
    return like_code(user, int(data.get("id") or 0))


@app.post("/api/codes/<int:code_id>/favorite")
@require_auth
def favorite_code(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM favorites WHERE user_id=%s AND code_id=%s", (user["id"], code_id))
            exists = bool(cur.fetchone())
            if exists:
                cur.execute("DELETE FROM favorites WHERE user_id=%s AND code_id=%s", (user["id"], code_id))
                favorite = False
            else:
                cur.execute(
                    "INSERT INTO favorites(user_id, code_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (user["id"], code_id),
                )
                favorite = True
        conn.commit()
        return jsonify({"ok": True, "favorite": favorite})
    finally:
        conn.close()


@app.post("/api/codes/<int:code_id>/hide")
@require_auth
def hide_code(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            hidden = user.get("hidden_codes") or []
            if isinstance(hidden, str):
                try:
                    hidden = json.loads(hidden)
                except Exception:
                    hidden = []
            if code_id not in hidden:
                hidden.append(code_id)
            cur.execute(
                "UPDATE users SET hidden_codes=%s WHERE id=%s",
                (psycopg2.extras.Json(hidden), user["id"]),
            )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/code/save")
@require_auth
def code_save(user):
    data = request.get_json(silent=True) or {}
    return favorite_code(user, int(data.get("id") or 0))


@app.post("/code/unsave")
@require_auth
def code_unsave(user):
    data = request.get_json(silent=True) or {}
    code_id = int(data.get("id") or 0)
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM favorites WHERE user_id=%s AND code_id=%s", (user["id"], code_id))
        conn.commit()
        return jsonify({"ok": True, "success": True})
    finally:
        conn.close()


@app.get("/api/favorites")
@app.get("/codes/saved")
@require_auth
def favorites(user):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.* FROM codes c
                INNER JOIN favorites f ON f.code_id=c.id
                WHERE f.user_id=%s
                ORDER BY f.created_at DESC
                """,
                (user["id"],),
            )
            rows = cur.fetchall()
        return jsonify({"ok": True, "codes": [serialize_code(row, user["id"]) for row in rows]})
    finally:
        conn.close()


@app.get("/api/my-codes")
@app.get("/codes/mine")
@app.get("/codes/user")
@require_auth
def my_codes(user):
    target = request.args.get("user_id") or user["id"]
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM codes WHERE user_id=%s ORDER BY created_at DESC", (target,))
            rows = cur.fetchall()
        return jsonify({"ok": True, "codes": [serialize_code(row, user["id"]) for row in rows]})
    finally:
        conn.close()


@app.post("/api/codes/<int:code_id>/report")
@app.post("/code/report")
@require_auth
def report_code(user, code_id=None):
    data = request.get_json(silent=True) or {}
    if code_id is None:
        code_id = int(data.get("id") or 0)
    reason = (data.get("reason") or "Contenu inapproprié").strip()
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM codes WHERE id=%s", (code_id,))
            code = cur.fetchone()
            if not code:
                return json_error("Code introuvable.", 404)
            cur.execute(
                """
                INSERT INTO reports(user_id, code_id, reason)
                VALUES(%s,%s,%s) ON CONFLICT DO NOTHING
                """,
                (user["id"], code_id, reason),
            )
            cur.execute("SELECT COUNT(*) AS total FROM reports WHERE code_id=%s", (code_id,))
            total = int(cur.fetchone()["total"])
            if data.get("hide"):
                hidden = user.get("hidden_codes") or []
                if code_id not in hidden:
                    hidden.append(code_id)
                cur.execute(
                    "UPDATE users SET hidden_codes=%s WHERE id=%s",
                    (psycopg2.extras.Json(hidden), user["id"]),
                )
            if total >= 10:
                cur.execute("UPDATE codes SET status='SUPPRIMEE' WHERE id=%s", (code_id,))
                create_notification(
                    cur, code["user_id"],
                    "Publication retirée",
                    f"Ta publication « {code['title']} » a été retirée après plusieurs signalements.",
                    "MODERATION",
                )
            cur.execute("UPDATE codes SET reports_count=%s WHERE id=%s", (total, code_id))
        conn.commit()
        return jsonify({"ok": True, "success": True, "reports": total, "removed": total >= 10})
    finally:
        conn.close()


@app.post("/code/delete")
@require_auth
def code_delete(user):
    data = request.get_json(silent=True) or {}
    code_id = data.get("id")
    conn = db()
    try:
        with conn.cursor() as cur:
            if is_admin(user):
                cur.execute("UPDATE codes SET status='SUPPRIMEE' WHERE id=%s", (code_id,))
            else:
                cur.execute(
                    "UPDATE codes SET status='SUPPRIMEE' WHERE id=%s AND user_id=%s",
                    (code_id, user["id"]),
                )
        conn.commit()
        return jsonify({"ok": True, "success": True})
    finally:
        conn.close()


@app.post("/code/restore")
@require_auth
def code_restore(user):
    data = request.get_json(silent=True) or {}
    conn = db()
    try:
        with conn.cursor() as cur:
            if is_admin(user):
                cur.execute("UPDATE codes SET status='VALIDEE' WHERE id=%s", (data.get("id"),))
            else:
                cur.execute(
                    "UPDATE codes SET status='VALIDEE' WHERE id=%s AND user_id=%s",
                    (data.get("id"), user["id"]),
                )
        conn.commit()
        return jsonify({"ok": True, "success": True})
    finally:
        conn.close()


@app.post("/code/hard-delete")
@require_auth
def code_hard_delete(user):
    if not is_admin(user):
        return json_error("unauthorized", 403)
    data = request.get_json(silent=True) or {}
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM codes WHERE id=%s", (data.get("id"),))
        conn.commit()
        return jsonify({"ok": True, "success": True})
    finally:
        conn.close()


@app.post("/code/edit")
@require_auth
def code_edit(user):
    data = request.get_json(silent=True) or {}
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE codes
                SET brand=%s, title=%s, code=%s, description=%s, url=%s, expires_at=%s
                WHERE id=%s AND (user_id=%s OR %s)
                """,
                (
                    data.get("site") or data.get("brand"),
                    data.get("site") or data.get("title"),
                    data.get("code"),
                    data.get("description"),
                    data.get("url"),
                    data.get("expires_at") or None,
                    data.get("id"),
                    user["id"],
                    is_admin(user),
                ),
            )
        conn.commit()
        return jsonify({"ok": True, "success": True})
    finally:
        conn.close()


@app.patch("/api/profile")
@app.post("/profile/bio")
@require_auth
def update_profile(user):
    data = request.get_json(silent=True) or {}
    display_name = data.get("display_name") if data.get("display_name") is not None else user["display_name"]
    bio = data.get("bio") if data.get("bio") is not None else user.get("bio")
    display_name = str(display_name).strip()[:80]
    bio = str(bio or "").strip()[:300]
    initials = "".join(part[0] for part in display_name.split() if part)[:2].upper() or "CO"
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users SET display_name=%s, bio=%s, avatar_initials=%s, updated_at=NOW()
                WHERE id=%s
                """,
                (display_name, bio, initials, user["id"]),
            )
        conn.commit()
        return jsonify({"ok": True, "success": True})
    finally:
        conn.close()


@app.get("/profile/full_stats")
@require_auth
def profile_full_stats(user):
    target = request.args.get("user_id") or user["id"]
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (target,))
            u = cur.fetchone() or {}
            cur.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(likes_count),0) AS l, COALESCE(SUM(copies_count),0) AS k FROM codes WHERE user_id=%s AND status='VALIDEE'",
                (target,),
            )
            st = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS c FROM follows WHERE following_id=%s", (target,))
            followers = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower_id=%s", (target,))
            following = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE referred_by=%s", (target,))
            referrals = cur.fetchone()["c"]
        return jsonify({
            "username": u.get("username"),
            "bio": u.get("bio"),
            "total_codes": st["c"] if st else 0,
            "total_likes": st["l"] if st else 0,
            "total_copies": st["k"] if st else 0,
            "followers": followers,
            "following": following,
            "referrals": referrals,
        })
    finally:
        conn.close()


@app.get("/api/leaderboard")
@app.get("/leaderboard")
@app.get("/referral/leaderboard")
def leaderboard():
    user = get_current_user()
    start = challenge_start()
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, COALESCE(u.display_name,u.username,'Membre') AS name,
                       COALESCE(u.avatar_initials,'CO') AS initials,
                       COUNT(r.id) AS points
                FROM users u
                LEFT JOIN users r ON r.referred_by=u.id
                 AND r.created_at >= %s AND r.created_at <= %s
                GROUP BY u.id, u.display_name, u.username, u.avatar_initials
                ORDER BY points DESC, u.created_at ASC
                LIMIT 10
                """,
                (start, start + timedelta(days=21)),
            )
            rows = cur.fetchall()
            cur.execute(
                """
                SELECT user_id, COALESCE(brand,title,'Membre') AS name, COUNT(*) AS codes_count
                FROM codes
                WHERE status='VALIDEE' AND created_at > NOW() - INTERVAL '7 days'
                GROUP BY user_id, brand, title
                ORDER BY codes_count DESC LIMIT 10
                """
            )
            week = cur.fetchall()
        challenge = []
        for i, row in enumerate(rows, 1):
            challenge.append({
                "rank": i,
                "user_id": row["id"],
                "name": row["name"],
                "initials": row.get("initials") or "CO",
                "points": int(row["points"] or 0),
                "referrals_count": int(row["points"] or 0),
                "me": bool(user and row["id"] == user["id"]),
            })
        weekly = []
        for i, row in enumerate(week, 1):
            weekly.append({
                "rank": i,
                "user_id": row["user_id"],
                "name": row["name"],
                "points": row["codes_count"],
                "codes_count": row["codes_count"],
                "me": bool(user and row["user_id"] == user["id"]),
            })
        return jsonify({"ok": True, "leaderboard": weekly, "challenge": challenge, "weekly": weekly})
    finally:
        conn.close()


@app.get("/referral/status")
@require_auth
def referral_status(user):
    info = challenge_info(user["id"])
    return jsonify({
        "my_code": user.get("referral_code"),
        "has_used": bool(user.get("referred_by")),
        "referrals_count": info["points"],
    })


@app.post("/referral/generate")
@require_auth
def referral_generate(user):
    return jsonify({"success": True, "ok": True, "code": user.get("referral_code")})


@app.post("/referral/integrate")
@require_auth
def referral_integrate(user):
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE referral_code=%s", (code,))
            ref = cur.fetchone()
            if not ref:
                return json_error("Code introuvable.")
            if ref["id"] == user["id"]:
                return json_error("Tu ne peux pas utiliser ton propre code.")
            if user.get("referred_by"):
                return json_error("Tu as déjà utilisé un code.")
            cur.execute("UPDATE users SET referred_by=%s WHERE id=%s", (ref["id"], user["id"]))
            create_notification(
                cur, ref["id"],
                "Nouveau parrainage",
                f"{user.get('display_name')} a rejoint COD.IA avec ton code. +1 point.",
                "REFERRAL",
            )
        conn.commit()
        return jsonify({"ok": True, "success": True})
    finally:
        conn.close()


@app.get("/api/notifications")
@app.get("/notifications")
@require_auth
def notifications(user):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 50",
                (user["id"],),
            )
            rows = cur.fetchall()
            cur.execute(
                "SELECT COUNT(*) AS total FROM notifications WHERE user_id=%s AND is_read=FALSE",
                (user["id"],),
            )
            unread = int(cur.fetchone()["total"] or 0)
        return jsonify({
            "ok": True,
            "unread": unread,
            "notifications": [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "message": row["message"],
                    "type": row["type"],
                    "read": row["is_read"],
                    "created_at": iso(row["created_at"]),
                }
                for row in rows
            ],
        })
    finally:
        conn.close()


@app.post("/api/notifications/read")
@app.post("/notifications/read")
@require_auth
def notifications_read(user):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET is_read=TRUE WHERE user_id=%s", (user["id"],))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/support")
@app.post("/support/send")
@require_auth
def support(user):
    data = request.get_json(silent=True) or {}
    subject = (data.get("subject") or "Support").strip()
    message = (data.get("message") or "").strip()
    if len(message) < 3:
        return json_error("Message trop court.")
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO support_tickets(user_id, subject, message) VALUES(%s,%s,%s)",
                (user["id"], subject, message),
            )
            create_notification(cur, user["id"], "Message envoyé", "L'équipe COD.IA a bien reçu ta demande.", "SUPPORT")
        conn.commit()
        if ADMIN_TELEGRAM_ID:
            send_telegram_message(ADMIN_TELEGRAM_ID, f"Support COD.IA\n{user.get('username')}\n{message}")
        return jsonify({"ok": True, "success": True})
    finally:
        conn.close()


@app.get("/support/list")
@app.get("/api/admin/support")
@require_auth
def support_list(user):
    if not is_admin(user):
        return json_error("unauthorized", 403)
    status = (request.args.get("status") or "OPEN").upper()
    if status == "OPEN":
        status = "OPEN"
    elif status in ("REPLIED", "replied"):
        status = "REPLIED"
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.*, u.username AS user_name
                FROM support_tickets t
                LEFT JOIN users u ON u.id=t.user_id
                WHERE t.status=%s
                ORDER BY t.created_at DESC LIMIT 50
                """,
                (status,),
            )
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) AS c FROM support_tickets WHERE status='OPEN'")
            open_count = cur.fetchone()["c"]
        return jsonify({"ok": True, "messages": rows, "open_count": open_count})
    finally:
        conn.close()


@app.post("/support/reply")
@require_auth
def support_reply(user):
    if not is_admin(user):
        return json_error("unauthorized", 403)
    data = request.get_json(silent=True) or {}
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE support_tickets
                SET admin_reply=%s, status='REPLIED'
                WHERE id=%s
                RETURNING *
                """,
                (data.get("reply"), data.get("id")),
            )
            row = cur.fetchone()
            if row:
                create_notification(
                    cur, row["user_id"],
                    "Réponse du support",
                    data.get("reply") or "",
                    "SUPPORT",
                )
        conn.commit()
        return jsonify({"ok": True, "success": True})
    finally:
        conn.close()


@app.get("/api/admin/stats")
@app.get("/admin/stats")
@require_auth
def admin_stats(user):
    if not is_admin(user):
        return json_error("Accès administrateur refusé.", 403)
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            users = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM users WHERE is_paid=TRUE")
            paid = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM codes WHERE status='VALIDEE'")
            codes = int(cur.fetchone()["n"])
            cur.execute("SELECT COUNT(*) AS n FROM users WHERE referred_by IS NOT NULL")
            refs = int(cur.fetchone()["n"])
            cur.execute(
                """
                SELECT COALESCE(u.display_name,u.username,'Membre') AS name, u.created_at AS paid_at
                FROM users u WHERE u.is_paid=TRUE ORDER BY u.created_at DESC LIMIT 20
                """
            )
            joins = cur.fetchall()
        return jsonify({
            "ok": True,
            "total_members": users,
            "users": users,
            "paid": paid,
            "paid_members": paid,
            "codes": codes,
            "total_codes": codes,
            "total_referrals": refs,
            "recent_joins": joins,
        })
    finally:
        conn.close()


@app.get("/coach/tips")
@require_auth
def coach_tips(user):
    return jsonify({
        "tips": [{
            "text": "Publie un code aujourd'hui pour apparaître dans le feed.",
            "cta": "Publier",
            "action": "share",
        }]
    })


@app.get("/coach/daily")
@require_auth
def coach_daily(user):
    return jsonify({
        "challenge": {"label": "Publie 1 code aujourd'hui", "progress": 0, "target": 1, "completed": False},
        "challenges_completed_total": 0,
    })


@app.get("/coach/badges")
@require_auth
def coach_badges(user):
    return jsonify({"total_challenges": 0, "all": [{"icon": "🌱", "label": "Débutant", "desc": "Premier pas", "unlocked": True}]})


@app.get("/coach/badge")
def coach_badge():
    return jsonify({"badge": None})


@app.post("/telegram")
def telegram():
    payload = request.get_json(silent=True) or {}
    message = payload.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    from_user = message.get("from") or {}
    telegram_id = str(from_user.get("id") or "")
    if text.startswith("/start"):
        send_telegram_message(chat_id, f"COD.IA\nOuvre la plateforme : {SERVER_URL}")
    elif text.startswith("/startadmin") and telegram_id in ADMIN_IDS:
        send_telegram_message(chat_id, f"Admin COD.IA\n{SERVER_URL}/app?admin=1")
    return jsonify(success=True)


try:
    init_db()
except Exception as startup_error:
    logging.error("COD.IA DATABASE INIT ERROR: %s", startup_error)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
