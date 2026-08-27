import os
import json
import logging
import secrets
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
import stripe
import requests
from flask import Flask, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)

DATABASE_URL = os.getenv("DATABASE_URL")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
SERVER_URL = os.getenv("SERVER_URL", "https://cod-ia.fr")
PRICE_CENTS = int(os.getenv("PRICE_CENTS", "999"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")

ADMIN_EMAILS = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "contact@cod-ia.fr").split(",") if e.strip()]
ADMIN_IDS = set()
for x in os.getenv("ADMIN_IDS", "8091031583,6886937051").split(","):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE,
            username TEXT UNIQUE,
            password_hash TEXT,
            telegram_id BIGINT UNIQUE,
            bio TEXT,
            instagram TEXT,
            snapchat TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS paid_users (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            telegram_id BIGINT,
            paid_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS codes (
            id SERIAL PRIMARY KEY,
            type TEXT,
            site TEXT,
            code TEXT,
            description TEXT,
            url TEXT,
            expires_at TIMESTAMP,
            added_by TEXT,
            user_id INTEGER,
            photo_url TEXT,
            likes INTEGER DEFAULT 0,
            dislikes INTEGER DEFAULT 0,
            copies INTEGER DEFAULT 0,
            reports INTEGER DEFAULT 0,
            deleted BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            message TEXT,
            read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS follows (
            follower_id INTEGER,
            followed_id INTEGER,
            UNIQUE(follower_id, followed_id)
        );
        CREATE TABLE IF NOT EXISTS saved_codes (
            user_id INTEGER,
            code_id INTEGER,
            UNIQUE(user_id, code_id)
        );
        CREATE TABLE IF NOT EXISTS hidden_codes (
            user_id INTEGER,
            code_id INTEGER,
            UNIQUE(user_id, code_id)
        );
        CREATE TABLE IF NOT EXISTS search_logs (
            q TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS referrals (
            user_id INTEGER PRIMARY KEY,
            code TEXT UNIQUE,
            used_code TEXT
        );
        CREATE TABLE IF NOT EXISTS referral_uses (
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE
        );
        CREATE TABLE IF NOT EXISTS support_messages (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            user_name TEXT,
            message TEXT,
            admin_reply TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS push_settings (
            user_id INTEGER PRIMARY KEY,
            enabled BOOLEAN DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS daily_challenges (
            user_id INTEGER,
            day DATE,
            progress INTEGER DEFAULT 0,
            completed BOOLEAN DEFAULT FALSE,
            UNIQUE(user_id, day)
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()
    logging.info("DB initialisée")


def session_user_id():
    uid = session.get("user_id")
    try:
        return int(uid) if uid is not None else None
    except Exception:
        return None


def is_admin_email(email):
    return (email or "").lower() in ADMIN_EMAILS


def is_admin_id(uid):
    try:
        return int(uid) in ADMIN_IDS
    except Exception:
        return False


def is_paid(uid, telegram_id=None):
    if not uid and not telegram_id:
        return False
    conn = get_conn()
    cur = conn.cursor()
    if uid:
        cur.execute("SELECT 1 FROM paid_users WHERE user_id=%s LIMIT 1", (uid,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return True
    if telegram_id:
        cur.execute("SELECT 1 FROM paid_users WHERE telegram_id=%s LIMIT 1", (telegram_id,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return True
    cur.close()
    conn.close()
    return False


def get_user_by_id(uid):
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def send_telegram_message(chat_id, text):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        logging.error(e)


@app.route("/")
def landing():
    return send_from_directory(".", "landing.html")


@app.route("/app")
def miniapp():
    return send_from_directory(".", "miniapp.html")


@app.route("/miniapp")
def miniapp_old():
    return send_from_directory(".", "miniapp.html")


@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory("assets", filename)


@app.route("/config")
def config():
    return jsonify({"stripe_pk": STRIPE_PUBLISHABLE_KEY or "", "price": PRICE_CENTS})


@app.route("/me")
def me():
    uid = session_user_id()
    if not uid:
        return jsonify({"user": None, "paid": False, "is_admin": False})
    user = get_user_by_id(uid)
    if not user:
        session.clear()
        return jsonify({"user": None, "paid": False, "is_admin": False})
    admin = is_admin_email(user.get("email")) or is_admin_id(user.get("telegram_id"))
    paid = admin or is_paid(uid, user.get("telegram_id"))
    return jsonify(
        {
            "user": {
                "id": user["id"],
                "email": user.get("email"),
                "username": user.get("username"),
                "instagram": user.get("instagram"),
                "snapchat": user.get("snapchat"),
            },
            "paid": paid,
            "is_admin": admin,
        }
    )


@app.route("/access")
def access():
    uid = request.args.get("user_id")
    try:
        uid = int(uid)
    except Exception:
        return jsonify({"paid": False, "is_admin": False})
    admin = is_admin_id(uid)
    return jsonify({"paid": admin or is_paid(None, uid) or is_paid(uid), "is_admin": admin})


@app.route("/auth/register", methods=["POST"])
def auth_register():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    username = (data.get("username") or "").strip().lstrip("@")
    password = data.get("password") or ""
    if not email or "@" not in email:
        return jsonify(success=False, error="Email invalide")
    if len(username) < 3:
        return jsonify(success=False, error="Username trop court")
    if len(password) < 8:
        return jsonify(success=False, error="Mot de passe trop court")
    try:
        conn = get_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "INSERT INTO users (email, username, password_hash) VALUES (%s,%s,%s) RETURNING *",
            (email, username, generate_password_hash(password)),
        )
        user = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        return jsonify(success=False, error="Email ou username déjà utilisé")
    session.permanent = True
    session["user_id"] = user["id"]
    admin = is_admin_email(email)
    if admin:
        mark_paid(user["id"], None)
    return jsonify(
        success=True,
        user={
            "id": user["id"],
            "email": email,
            "username": username,
            "paid": admin or is_paid(user["id"]),
            "is_admin": admin,
        },
    )


@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    login = (data.get("login") or "").strip()
    password = data.get("password") or ""
    if not login or not password:
        return jsonify(success=False, error="Identifiants manquants")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if "@" in login:
        cur.execute("SELECT * FROM users WHERE lower(email)=%s", (login.lower(),))
    else:
        cur.execute("SELECT * FROM users WHERE lower(username)=%s", (login.lower().lstrip("@"),))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        return jsonify(success=False, error="Identifiants incorrects")
    session.permanent = True
    session["user_id"] = user["id"]
    admin = is_admin_email(user.get("email")) or is_admin_id(user.get("telegram_id"))
    paid = admin or is_paid(user["id"], user.get("telegram_id"))
    return jsonify(
        success=True,
        user={
            "id": user["id"],
            "email": user.get("email"),
            "username": user.get("username"),
            "paid": paid,
            "is_admin": admin,
        },
    )


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify(success=True)


def mark_paid(user_id=None, telegram_id=None):
    conn = get_conn()
    cur = conn.cursor()
    if user_id:
        cur.execute("SELECT 1 FROM paid_users WHERE user_id=%s", (user_id,))
        if not cur.fetchone():
            cur.execute("INSERT INTO paid_users (user_id, telegram_id) VALUES (%s,%s)", (user_id, telegram_id))
    elif telegram_id:
        cur.execute("SELECT 1 FROM paid_users WHERE telegram_id=%s", (telegram_id,))
        if not cur.fetchone():
            cur.execute("INSERT INTO paid_users (telegram_id) VALUES (%s)", (telegram_id,))
    conn.commit()
    cur.close()
    conn.close()


@app.route("/create-embedded-checkout", methods=["POST"])
def create_embedded_checkout():
    data = request.json or {}
    telegram_id = data.get("telegram_id")
    uid = session_user_id()
    if not uid and not telegram_id:
        return jsonify(error="Connecte-toi d’abord"), 401
    try:
        checkout = stripe.checkout.Session.create(
            mode="payment",
            ui_mode="embedded_page",
            redirect_on_completion="if_required",
            return_url=SERVER_URL + "/app?paid=1",
            line_items=[
                {
                    "price_data": {
                        "currency": "eur",
                        "product_data": {"name": "Accès COD.IA"},
                        "unit_amount": PRICE_CENTS,
                    },
                    "quantity": 1,
                }
            ],
            metadata={
                "user_id": str(uid or ""),
                "telegram_id": str(telegram_id or ""),
            },
        )
        return jsonify(clientSecret=checkout.client_secret)
    except Exception as e:
        logging.error(e)
        return jsonify(error=str(e)), 400


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)
    except Exception as e:
        return jsonify(error=str(e)), 400
    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        obj = event["data"]["object"]
        meta = obj.get("metadata") or {}
        uid = meta.get("user_id")
        tid = meta.get("telegram_id")
        mark_paid(int(uid) if uid and str(uid).isdigit() else None, int(tid) if tid and str(tid).isdigit() else None)
    return jsonify(ok=True)


def hidden_filter(uid):
    if not uid:
        return ""
    return f" AND id NOT IN (SELECT code_id FROM hidden_codes WHERE user_id={int(uid)})"


@app.route("/codes")
def list_codes():
    typ = request.args.get("type")
    expiring = request.args.get("expiring")
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    q = "SELECT * FROM codes WHERE deleted=FALSE"
    params = []
    if typ:
        q += " AND type=%s"
        params.append(typ)
    if expiring:
        q += " AND expires_at IS NOT NULL AND expires_at < NOW() + INTERVAL '4 days' AND expires_at > NOW()"
    q += hidden_filter(uid)
    q += " ORDER BY created_at DESC LIMIT 200"
    cur.execute(q, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/codes/top")
def codes_top():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM codes WHERE deleted=FALSE AND (likes>=100 OR copies>=100) ORDER BY likes+copies DESC LIMIT 10"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/codes/search")
def codes_search():
    q = request.args.get("q") or ""
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM codes WHERE deleted=FALSE AND (site ILIKE %s OR code ILIKE %s OR description ILIKE %s) "
        + hidden_filter(uid)
        + " ORDER BY created_at DESC LIMIT 50",
        (f"%{q}%", f"%{q}%", f"%{q}%"),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/codes/add", methods=["POST"])
def codes_add():
    data = request.json or {}
    uid = data.get("user_id") or session_user_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO codes (type,site,code,description,url,expires_at,added_by,user_id,photo_url)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            data.get("type") or "promo",
            data.get("site"),
            data.get("code"),
            data.get("description"),
            data.get("url"),
            data.get("expires_at") or None,
            data.get("added_by") or "Membre",
            uid,
            data.get("photo_url"),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/codes/mine")
@app.route("/codes/user")
def codes_mine():
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM codes WHERE user_id=%s ORDER BY created_at DESC", (uid,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/codes/saved")
def codes_saved():
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT c.* FROM codes c JOIN saved_codes s ON s.code_id=c.id WHERE s.user_id=%s AND c.deleted=FALSE ORDER BY c.created_at DESC",
        (uid,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/code/copy", methods=["POST"])
def code_copy():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE codes SET copies=COALESCE(copies,0)+1 WHERE id=%s RETURNING copies", (data.get("id"),))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(copies=row[0] if row else 0)


@app.route("/code/react", methods=["POST"])
def code_react():
    data = request.json or {}
    field = "likes" if data.get("reaction") == "like" else "dislikes"
    op = "+1" if data.get("action") == "add" else "-1"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE codes SET {field}=GREATEST(COALESCE({field},0){op},0) WHERE id=%s RETURNING {field}", (data.get("id"),))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(value=row[0] if row else 0)


@app.route("/code/save", methods=["POST"])
def code_save():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO saved_codes (user_id, code_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (data.get("user_id"), data.get("id")),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/code/unsave", methods=["POST"])
def code_unsave():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM saved_codes WHERE user_id=%s AND code_id=%s", (data.get("user_id"), data.get("id")))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/code/delete", methods=["POST"])
def code_delete():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE codes SET deleted=TRUE WHERE id=%s", (data.get("id"),))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/code/restore", methods=["POST"])
def code_restore():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE codes SET deleted=FALSE WHERE id=%s", (data.get("id"),))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/code/edit", methods=["POST"])
def code_edit():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE codes SET site=%s, code=%s, description=%s, url=%s, expires_at=%s WHERE id=%s",
        (
            data.get("site"),
            data.get("code"),
            data.get("description"),
            data.get("url"),
            data.get("expires_at") or None,
            data.get("id"),
        ),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/code/hard-delete", methods=["POST"])
def code_hard_delete():
    data = request.json or {}
    if not (is_admin_id(data.get("user_id")) or is_admin_email((get_user_by_id(session_user_id()) or {}).get("email"))):
        return jsonify(error="unauthorized"), 403
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM codes WHERE id=%s", (data.get("id"),))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/code/report", methods=["POST"])
def code_report():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE codes SET reports=COALESCE(reports,0)+1 WHERE id=%s RETURNING reports", (data.get("id"),))
    row = cur.fetchone()
    if data.get("hide") and data.get("user_id"):
        cur.execute(
            "INSERT INTO hidden_codes (user_id, code_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (data.get("user_id"), data.get("id")),
        )
    if row and row[0] >= 10:
        cur.execute("UPDATE codes SET deleted=TRUE WHERE id=%s", (data.get("id"),))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/search/log", methods=["POST"])
def search_log():
    q = (request.json or {}).get("q")
    if q:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO search_logs (q) VALUES (%s)", (q,))
        conn.commit()
        cur.close()
        conn.close()
    return jsonify(ok=True)


@app.route("/search/recent")
def search_recent():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT q FROM search_logs ORDER BY created_at DESC LIMIT 8")
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(queries=rows)


@app.route("/notifications")
def notifications():
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 30", (uid,))
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM notifications WHERE user_id=%s AND read=FALSE", (uid,))
    unread = cur.fetchone()["count"]
    cur.close()
    conn.close()
    return jsonify(notifications=rows, unread=unread)


@app.route("/notifications/read", methods=["POST"])
def notifications_read():
    uid = (request.json or {}).get("user_id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE notifications SET read=TRUE WHERE user_id=%s", (uid,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(ok=True)


@app.route("/profile/full_stats")
def profile_full_stats():
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    user = cur.fetchone() or {}
    cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(likes),0) AS l, COALESCE(SUM(copies),0) AS k FROM codes WHERE user_id=%s AND deleted=FALSE", (uid,))
    st = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS c FROM follows WHERE followed_id=%s", (uid,))
    followers = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower_id=%s", (uid,))
    following = cur.fetchone()["c"]
    cur.close()
    conn.close()
    return jsonify(
        username=user.get("username"),
        bio=user.get("bio"),
        instagram=user.get("instagram"),
        snapchat=user.get("snapchat"),
        total_codes=st["c"] if st else 0,
        total_likes=st["l"] if st else 0,
        total_copies=st["k"] if st else 0,
        followers=followers,
        following=following,
        badge=None,
    )


@app.route("/profile/bio", methods=["POST"])
def profile_bio():
    data = request.json or {}
    uid = data.get("user_id") or session_user_id()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET bio=%s WHERE id=%s", (data.get("bio"), uid))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/profile/social", methods=["POST"])
def profile_social():
    data = request.json or {}
    uid = session_user_id() or data.get("user_id")
    ig = (data.get("instagram") or "").lstrip("@")
    snap = (data.get("snapchat") or "").lstrip("@")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET instagram=%s, snapchat=%s WHERE id=%s", (ig or None, snap or None, uid))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True, instagram=ig, snapchat=snap)


@app.route("/settings/push")
def get_push():
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT enabled FROM push_settings WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return jsonify(enabled=True if not row else row[0])


@app.route("/settings/push", methods=["POST"])
def set_push():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO push_settings (user_id, enabled) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET enabled=EXCLUDED.enabled",
        (data.get("user_id"), bool(data.get("enabled"))),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(ok=True)


@app.route("/follow", methods=["POST"])
def follow():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO follows (follower_id, followed_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (data.get("follower_id"), data.get("followed_id")),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(ok=True)


@app.route("/unfollow", methods=["POST"])
def unfollow():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM follows WHERE follower_id=%s AND followed_id=%s",
        (data.get("follower_id"), data.get("followed_id")),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(ok=True)


@app.route("/is_following")
def is_following():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM follows WHERE follower_id=%s AND followed_id=%s",
        (request.args.get("follower"), request.args.get("followed")),
    )
    ok = bool(cur.fetchone())
    cur.close()
    conn.close()
    return jsonify(following=ok)


@app.route("/followers")
@app.route("/following")
def follow_lists():
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if request.path.endswith("followers"):
        cur.execute(
            "SELECT u.id AS user_id, COALESCE('@'||u.username,'Membre') AS name FROM follows f JOIN users u ON u.id=f.follower_id WHERE f.followed_id=%s",
            (uid,),
        )
    else:
        cur.execute(
            "SELECT u.id AS user_id, COALESCE('@'||u.username,'Membre') AS name FROM follows f JOIN users u ON u.id=f.followed_id WHERE f.follower_id=%s",
            (uid,),
        )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(users=rows)


@app.route("/leaderboard")
def leaderboard():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT user_id, COALESCE(added_by,'Membre') AS name, COUNT(*) AS codes_count
        FROM codes
        WHERE deleted=FALSE AND created_at > NOW() - INTERVAL '7 days' AND user_id IS NOT NULL
        GROUP BY user_id, added_by
        ORDER BY codes_count DESC
        LIMIT 10
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    out = []
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        out.append(r)
    return jsonify(leaderboard=out)


@app.route("/referral/status")
def referral_status():
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM referrals WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS c FROM referral_uses WHERE referrer_id=%s", (uid,))
    count = cur.fetchone()["c"]
    cur.close()
    conn.close()
    return jsonify(
        my_code=row["code"] if row else None,
        has_used=bool(row and row.get("used_code")),
        referrals_count=count,
    )


@app.route("/referral/generate", methods=["POST"])
def referral_generate():
    uid = (request.json or {}).get("user_id") or session_user_id()
    code = "CODIA" + secrets.token_hex(3).upper()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO referrals (user_id, code) VALUES (%s,%s) ON CONFLICT (user_id) DO NOTHING",
        (uid, code),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True, code=code)


@app.route("/referral/integrate", methods=["POST"])
def referral_integrate():
    data = request.json or {}
    uid = data.get("user_id")
    code = (data.get("code") or "").strip().upper()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM referrals WHERE code=%s", (code,))
    ref = cur.fetchone()
    if not ref:
        cur.close()
        conn.close()
        return jsonify(success=False, error="Code introuvable")
    if int(ref["user_id"]) == int(uid):
        cur.close()
        conn.close()
        return jsonify(success=False, error="Tu ne peux pas utiliser ton propre code")
    cur.execute(
        "INSERT INTO referrals (user_id, used_code) VALUES (%s,%s) ON CONFLICT (user_id) DO UPDATE SET used_code=EXCLUDED.used_code",
        (uid, code),
    )
    cur.execute(
        "INSERT INTO referral_uses (referrer_id, referred_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (ref["user_id"], uid),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/referral/leaderboard")
def referral_leaderboard():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT r.user_id, COALESCE(u.username,'Membre') AS name, COUNT(ru.referred_id) AS referrals_count
        FROM referrals r
        LEFT JOIN referral_uses ru ON ru.referrer_id=r.user_id
        LEFT JOIN users u ON u.id=r.user_id
        GROUP BY r.user_id, u.username
        HAVING COUNT(ru.referred_id) > 0
        ORDER BY referrals_count DESC
        LIMIT 10
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    out = []
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        out.append(r)
    return jsonify(leaderboard=out)


@app.route("/coach/tips")
def coach_tips():
    return jsonify(tips=[{"text": "Publie un code aujourd’hui pour apparaître dans le feed.", "cta": "Publier", "action": "share"}])


@app.route("/coach/daily")
def coach_daily():
    return jsonify(challenge={"label": "Publie 1 code aujourd’hui", "progress": 0, "target": 1, "completed": False}, challenges_completed_total=0)


@app.route("/coach/badges")
def coach_badges():
    return jsonify(total_challenges=0, all=[{"icon": "🌱", "label": "Débutant", "desc": "Premier pas", "unlocked": True}])


@app.route("/coach/badge")
def coach_badge():
    return jsonify(badge=None)


@app.route("/support/send", methods=["POST"])
def support_send():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO support_messages (user_id,user_name,message) VALUES (%s,%s,%s)",
        (data.get("user_id"), data.get("user_name"), data.get("message")),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/support/list")
def support_list():
    uid = request.args.get("user_id")
    user = get_user_by_id(session_user_id()) if session_user_id() else None
    if not (is_admin_id(uid) or is_admin_email((user or {}).get("email"))):
        return jsonify(error="unauthorized"), 403
    status = request.args.get("status") or "open"
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM support_messages WHERE status=%s ORDER BY created_at DESC LIMIT 50", (status,))
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS c FROM support_messages WHERE status='open'")
    open_count = cur.fetchone()["c"]
    cur.close()
    conn.close()
    return jsonify(messages=rows, open_count=open_count)


@app.route("/support/reply", methods=["POST"])
def support_reply():
    data = request.json or {}
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "UPDATE support_messages SET admin_reply=%s, status='replied' WHERE id=%s RETURNING *",
        (data.get("reply"), data.get("id")),
    )
    row = cur.fetchone()
    if row:
        cur.execute(
            "INSERT INTO notifications (user_id, message) VALUES (%s,%s)",
            (row["user_id"], "Réponse du support : " + (data.get("reply") or "")),
        )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/admin/stats")
def admin_stats():
    uid = request.args.get("user_id")
    user = get_user_by_id(session_user_id()) if session_user_id() else None
    if not (is_admin_id(uid) or is_admin_email((user or {}).get("email"))):
        return jsonify(error="unauthorized"), 403
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) AS c FROM paid_users")
    paid = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM users")
    members = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM codes WHERE deleted=FALSE")
    codes = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM referral_uses")
    refs = cur.fetchone()["c"]
    cur.execute(
        "SELECT COALESCE(u.username,'Membre') AS name, p.paid_at FROM paid_users p LEFT JOIN users u ON u.id=p.user_id ORDER BY p.paid_at DESC LIMIT 20"
    )
    joins = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(total_members=members, paid_members=paid, total_codes=codes, total_referrals=refs, recent_joins=joins)


@app.route("/stats")
def stats():
    return jsonify(ok=True)


@app.route("/telegram", methods=["POST"])
def telegram():
    return jsonify(success=True)


try:
    init_db()
except Exception as e:
    logging.error(f"Init DB error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
