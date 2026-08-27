import os
import logging
import secrets
from datetime import timedelta

import psycopg2
import psycopg2.extras
import stripe
from flask import (
    Flask,
    jsonify,
    request,
    session,
    send_from_directory,
)
from werkzeug.security import generate_password_hash, check_password_hash

logging.basicConfig(level=logging.INFO)

app = Flask(__name__, static_folder=".", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
app.permanent_session_lifetime = timedelta(days=90)

DATABASE_URL = os.environ.get("DATABASE_URL")
SERVER_URL = os.environ.get("SERVER_URL", "https://cod-ia.fr").rstrip("/")
PRICE_CENTS = int(os.environ.get("PRICE_CENTS", "999"))
ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.environ.get("ADMIN_EMAILS", "contact@cod-ia.fr").split(",")
    if e.strip()
]
ADMIN_IDS = [
    x.strip()
    for x in os.environ.get("ADMIN_IDS", "8091031583,6886937051").split(",")
    if x.strip()
]

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL manquant")
    return psycopg2.connect(DATABASE_URL)


def session_user_id():
    return session.get("user_id")


def is_admin_email(email):
    return bool(email) and str(email).lower() in ADMIN_EMAILS


def is_admin_id(uid):
    return uid is not None and str(uid) in ADMIN_IDS


def get_user_by_id(uid):
    if not uid:
        return None
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    return user


def is_paid(uid):
    if not uid:
        return False
    user = get_user_by_id(uid)
    if user and (is_admin_email(user.get("email")) or is_admin_id(uid) or user.get("role") == "admin"):
        return True
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM paid_users WHERE user_id=%s LIMIT 1", (uid,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return bool(row)


def user_public(user, paid=None, admin=None):
    if not user:
        return None
    uid = user["id"]
    admin = is_admin_email(user.get("email")) or is_admin_id(uid) or user.get("role") == "admin" if admin is None else admin
    paid = True if admin else (is_paid(uid) if paid is None else paid)
    return {
        "id": uid,
        "username": user.get("username"),
        "email": user.get("email"),
        "points": user.get("points") or 0,
        "role": "admin" if admin else (user.get("role") or "user"),
        "paid": paid,
        "is_admin": admin,
    }


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT,
            email TEXT UNIQUE,
            password TEXT,
            telegram_id TEXT,
            points INTEGER DEFAULT 0,
            role TEXT DEFAULT 'user',
            bio TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS paid_users (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            telegram_id TEXT,
            paid_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS codes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            type TEXT,
            site TEXT,
            title TEXT,
            code TEXT,
            description TEXT,
            url TEXT,
            expires_at DATE,
            added_by TEXT,
            likes INTEGER DEFAULT 0,
            copies INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            deleted BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS code_saves (
            user_id INTEGER,
            code_id INTEGER,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, code_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS code_reacts (
            user_id INTEGER,
            code_id INTEGER,
            reaction TEXT,
            UNIQUE(user_id, code_id, reaction)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE,
            code TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referral_uses (
            id SERIAL PRIMARY KEY,
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS support_messages (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            user_name TEXT,
            message TEXT,
            admin_reply TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    for stmt in [
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'approved'",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS clicks INTEGER DEFAULT 0",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS title TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS copies INTEGER DEFAULT 0",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS likes INTEGER DEFAULT 0",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS url TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS expires_at DATE",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS added_by TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS user_id INTEGER",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS type TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS site TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS code TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS description TEXT",
    ]:
        try:
            cur.execute(stmt)
        except Exception as e:
            logging.warning("alter skip: %s", e)
            conn.rollback()
    cur.execute(
        "UPDATE users SET role='admin' WHERE lower(email)=ANY(%s)",
        (ADMIN_EMAILS,),
    )
    conn.commit()
    cur.close()
    conn.close()


@app.route("/")
def home():
    return send_from_directory(".", "landing.html")


@app.route("/app")
@app.route("/miniapp")
def miniapp():
    return send_from_directory(".", "miniapp.html")


@app.route("/config")
def config():
    return jsonify(stripe_pk=os.environ.get("STRIPE_PUBLISHABLE_KEY", ""))


@app.route("/me")
def me():
    uid = session_user_id()
    user = get_user_by_id(uid)
    if not user:
        return jsonify(user=None, paid=False, is_admin=False)
    admin = is_admin_email(user.get("email")) or is_admin_id(uid) or user.get("role") == "admin"
    paid = True if admin else is_paid(uid)
    return jsonify(user=user_public(user, paid=paid, admin=admin), paid=paid, is_admin=admin)


@app.route("/access")
def access():
    uid = session_user_id()
    return jsonify(ok=is_paid(uid))


@app.route("/auth/register", methods=["POST"])
def auth_register():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not email or not username or not password:
        return jsonify(success=False, error="Tous les champs sont obligatoires."), 400
    if len(password) < 8:
        return jsonify(success=False, error="Mot de passe : 8 caractères minimum."), 400
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM users WHERE lower(email)=%s", (email,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return jsonify(success=False, error="Cet email est déjà utilisé."), 409
    role = "admin" if email in ADMIN_EMAILS else "user"
    points = 50
    cur.execute(
        """
        INSERT INTO users (username, email, password, points, role)
        VALUES (%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (username, email, generate_password_hash(password), points, role),
    )
    user = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    session.permanent = True
    session["user_id"] = user["id"]
    return jsonify(success=True, user=user_public(user))


@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    login = (data.get("login") or data.get("email") or "").strip()
    password = data.get("password") or ""
    if not login or not password:
        return jsonify(success=False, error="Identifiant et mot de passe requis."), 400
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM users WHERE lower(email)=%s OR lower(username)=%s",
        (login.lower(), login.lower()),
    )
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user or not user.get("password") or not check_password_hash(user["password"], password):
        return jsonify(success=False, error="Email ou mot de passe incorrect."), 401
    if is_admin_email(user.get("email")):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE users SET role='admin' WHERE id=%s", (user["id"],))
        conn.commit()
        cur.close()
        conn.close()
        user["role"] = "admin"
    session.permanent = True
    session["user_id"] = user["id"]
    return jsonify(success=True, user=user_public(user))


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify(success=True)


@app.route("/create-embedded-checkout", methods=["POST"])
def create_embedded_checkout():
    uid = session_user_id()
    if not uid:
        return jsonify(error="Connecte-toi d’abord"), 401
    if not stripe.api_key:
        return jsonify(error="Stripe non configuré"), 500
    checkout = stripe.checkout.Session.create(
        mode="payment",
        ui_mode="embedded_page",
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
        metadata={"user_id": str(uid)},
    )
    return jsonify(clientSecret=checkout.client_secret)


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = request.get_json()
    except Exception as e:
        logging.error("webhook: %s", e)
        return jsonify(error="invalid"), 400
    etype = event.get("type") if isinstance(event, dict) else getattr(event, "type", "")
    data = event.get("data", {}).get("object", {}) if isinstance(event, dict) else {}
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        meta = data.get("metadata") or {}
        uid = meta.get("user_id")
        if uid:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM paid_users WHERE user_id=%s", (uid,))
            if not cur.fetchone():
                cur.execute("INSERT INTO paid_users (user_id) VALUES (%s)", (uid,))
            conn.commit()
            cur.close()
            conn.close()
    return jsonify(ok=True)


def approved_filter():
    return "AND COALESCE(deleted,FALSE)=FALSE AND (status='approved' OR status IS NULL)"


@app.route("/codes")
def codes_list():
    typ = request.args.get("type")
    expiring = request.args.get("expiring")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    q = "SELECT * FROM codes WHERE 1=1 " + approved_filter()
    params = []
    if typ in ("promo", "parrainage"):
        q += " AND type=%s"
        params.append(typ)
    if expiring:
        q += " AND expires_at IS NOT NULL AND expires_at <= (CURRENT_DATE + INTERVAL '7 day')"
    q += " ORDER BY id DESC LIMIT 200"
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
        "SELECT * FROM codes WHERE 1=1 " + approved_filter() + " ORDER BY COALESCE(likes,0) DESC, COALESCE(copies,0) DESC LIMIT 8"
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/codes/search")
def codes_search():
    qtxt = (request.args.get("q") or "").strip()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM codes WHERE 1=1 "
        + approved_filter()
        + " AND (site ILIKE %s OR code ILIKE %s OR description ILIKE %s OR COALESCE(title,'') ILIKE %s) ORDER BY id DESC LIMIT 100",
        (f"%{qtxt}%", f"%{qtxt}%", f"%{qtxt}%", f"%{qtxt}%"),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/codes/add", methods=["POST"])
def codes_add():
    uid = session_user_id()
    if not uid:
        return jsonify(success=False, error="Connecte-toi"), 401
    data = request.get_json(silent=True) or {}
    site = (data.get("site") or data.get("store") or "").strip()
    code = (data.get("code") or "").strip()
    typ = (data.get("type") or "promo").strip()
    title = (data.get("title") or "").strip()
    desc = (data.get("description") or title).strip()
    url = (data.get("url") or data.get("link") or "").strip() or None
    expires = data.get("expires_at") or None
    added_by = data.get("added_by") or None
    if not site or not code:
        return jsonify(success=False, error="Site et code obligatoires."), 400
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO codes (user_id, type, site, title, code, description, url, expires_at, added_by, status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
        RETURNING id
        """,
        (uid, typ, site, title, code, desc, url, expires, added_by),
    )
    new_id = cur.fetchone()[0]
    cur.execute("UPDATE users SET points = COALESCE(points,0)+10 WHERE id=%s", (uid,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True, id=new_id)


@app.route("/codes/mine")
def codes_mine():
    uid = request.args.get("user_id") or session_user_id()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM codes WHERE user_id=%s AND COALESCE(deleted,FALSE)=FALSE ORDER BY id DESC",
        (uid,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/codes/user")
def codes_user():
    uid = request.args.get("user_id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM codes WHERE user_id=%s " + approved_filter() + " ORDER BY id DESC",
        (uid,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/codes/saved")
def codes_saved():
    uid = request.args.get("user_id") or session_user_id()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT c.* FROM codes c
        JOIN code_saves s ON s.code_id=c.id
        WHERE s.user_id=%s """
        + approved_filter().replace("AND COALESCE", "AND COALESCE")
        + """
        ORDER BY s.created_at DESC
        """,
        (uid,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/code/copy", methods=["POST"])
def code_copy():
    data = request.get_json(silent=True) or {}
    cid = data.get("id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE codes SET copies=COALESCE(copies,0)+1 WHERE id=%s RETURNING copies", (cid,))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True, copies=row[0] if row else 0)


@app.route("/code/click", methods=["POST"])
def code_click():
    data = request.get_json(silent=True) or {}
    cid = data.get("id")
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "UPDATE codes SET clicks=COALESCE(clicks,0)+1 WHERE id=%s RETURNING url, clicks",
        (cid,),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not row:
        return jsonify(error="introuvable"), 404
    return jsonify(ok=True, url=row.get("url"), clicks=row.get("clicks") or 0)


@app.route("/code/react", methods=["POST"])
def code_react():
    uid = session_user_id() or (request.get_json(silent=True) or {}).get("user_id")
    data = request.get_json(silent=True) or {}
    cid = data.get("id")
    reaction = data.get("reaction") or "like"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO code_reacts (user_id, code_id, reaction) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
        (uid, cid, reaction),
    )
    if cur.rowcount:
        cur.execute("UPDATE codes SET likes=COALESCE(likes,0)+1 WHERE id=%s", (cid,))
    cur.execute("SELECT likes FROM codes WHERE id=%s", (cid,))
    likes = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True, value=likes)


@app.route("/code/save", methods=["POST"])
def code_save():
    data = request.get_json(silent=True) or {}
    uid = session_user_id() or data.get("user_id")
    cid = data.get("id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO code_saves (user_id, code_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (uid, cid),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/code/unsave", methods=["POST"])
def code_unsave():
    data = request.get_json(silent=True) or {}
    uid = session_user_id() or data.get("user_id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM code_saves WHERE user_id=%s AND code_id=%s", (uid, data.get("id")))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/code/delete", methods=["POST"])
def code_delete():
    data = request.get_json(silent=True) or {}
    uid = session_user_id()
    user = get_user_by_id(uid)
    admin = user and (is_admin_email(user.get("email")) or user.get("role") == "admin")
    conn = get_conn()
    cur = conn.cursor()
    if admin:
        cur.execute("UPDATE codes SET deleted=TRUE WHERE id=%s", (data.get("id"),))
    else:
        cur.execute("UPDATE codes SET deleted=TRUE WHERE id=%s AND user_id=%s", (data.get("id"), uid))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/code/restore", methods=["POST"])
def code_restore():
    data = request.get_json(silent=True) or {}
    uid = session_user_id()
    user = get_user_by_id(uid)
    if not user or not (is_admin_email(user.get("email")) or user.get("role") == "admin"):
        return jsonify(error="unauthorized"), 403
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE codes SET deleted=FALSE WHERE id=%s", (data.get("id"),))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/profile/full_stats")
def profile_full_stats():
    uid = request.args.get("user_id") or session_user_id()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT username, bio, points FROM users WHERE id=%s", (uid,))
    u = cur.fetchone() or {}
    cur.execute(
        """
        SELECT COUNT(*) AS total_codes,
               COALESCE(SUM(likes),0) AS total_likes,
               COALESCE(SUM(copies),0) AS total_copies,
               COALESCE(SUM(clicks),0) AS total_clicks
        FROM codes WHERE user_id=%s AND COALESCE(deleted,FALSE)=FALSE
        """,
        (uid,),
    )
    s = cur.fetchone() or {}
    cur.close()
    conn.close()
    return jsonify(
        username=u.get("username"),
        bio=u.get("bio") or "",
        points=u.get("points") or 0,
        total_codes=s.get("total_codes") or 0,
        total_likes=s.get("total_likes") or 0,
        total_copies=s.get("total_copies") or 0,
        total_clicks=s.get("total_clicks") or 0,
    )


@app.route("/profile/bio", methods=["POST"])
def profile_bio():
    uid = session_user_id()
    data = request.get_json(silent=True) or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET bio=%s WHERE id=%s", ((data.get("bio") or "")[:160], uid))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/referral/status")
def referral_status():
    uid = request.args.get("user_id") or session_user_id()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT code FROM referrals WHERE user_id=%s", (uid,))
    row = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS c FROM referral_uses WHERE referrer_id=%s", (uid,))
    count = cur.fetchone()["c"]
    cur.execute("SELECT 1 FROM referral_uses WHERE referred_id=%s", (uid,))
    used = bool(cur.fetchone())
    cur.close()
    conn.close()
    return jsonify(my_code=row["code"] if row else None, referrals_count=count, has_used=used)


@app.route("/referral/generate", methods=["POST"])
def referral_generate():
    uid = session_user_id()
    if not uid:
        return jsonify(error="login"), 401
    code = "CODIA" + secrets.token_hex(2).upper()
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
    uid = session_user_id()
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT user_id FROM referrals WHERE upper(code)=%s", (code,))
    ref = cur.fetchone()
    if not ref:
        cur.close()
        conn.close()
        return jsonify(success=False, error="Code inconnu")
    if str(ref["user_id"]) == str(uid):
        cur.close()
        conn.close()
        return jsonify(success=False, error="Tu ne peux pas utiliser ton propre code")
    cur.execute(
        "INSERT INTO referral_uses (referrer_id, referred_id) VALUES (%s,%s) ON CONFLICT (referred_id) DO NOTHING",
        (ref["user_id"], uid),
    )
    if cur.rowcount:
        cur.execute("UPDATE users SET points=COALESCE(points,0)+100 WHERE id=%s", (ref["user_id"],))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/leaderboard")
def leaderboard():
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT COALESCE(u.username,'Membre') AS name, COUNT(c.id) AS codes_count
        FROM users u
        LEFT JOIN codes c ON c.user_id=u.id AND COALESCE(c.deleted,FALSE)=FALSE
        GROUP BY u.id, u.username
        ORDER BY codes_count DESC
        LIMIT 20
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(leaderboard=rows)


@app.route("/admin/stats")
def admin_stats():
    uid = session_user_id()
    user = get_user_by_id(uid)
    if not user or not (is_admin_email(user.get("email")) or user.get("role") == "admin" or is_admin_id(uid)):
        return jsonify(error="unauthorized"), 403
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT COUNT(*) AS c FROM users")
    members = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM paid_users")
    paid = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM codes WHERE COALESCE(deleted,FALSE)=FALSE")
    codes = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM referral_uses")
    refs = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) AS c FROM codes WHERE status='pending'")
    pending = cur.fetchone()["c"]
    cur.execute("SELECT COALESCE(SUM(clicks),0) AS c FROM codes")
    clicks = cur.fetchone()["c"]
    cur.execute(
        """
        SELECT COALESCE(u.username,'Membre') AS name, p.paid_at
        FROM paid_users p LEFT JOIN users u ON u.id=p.user_id
        ORDER BY p.paid_at DESC LIMIT 20
        """
    )
    joins = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(
        total_members=members,
        paid_members=paid,
        total_codes=codes,
        total_referrals=refs,
        pending=pending,
        clicks=clicks,
        recent_joins=joins,
    )


@app.route("/admin/moderation")
def admin_moderation():
    uid = session_user_id()
    user = get_user_by_id(uid)
    if not user or not (is_admin_email(user.get("email")) or user.get("role") == "admin" or is_admin_id(uid)):
        return jsonify(error="unauthorized"), 403
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT c.*, COALESCE(u.username,'Membre') AS username, u.email
        FROM codes c
        LEFT JOIN users u ON u.id=c.user_id
        WHERE COALESCE(c.deleted,FALSE)=FALSE
        ORDER BY c.id DESC
        LIMIT 200
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(codes=rows)


@app.route("/admin/code/approve", methods=["POST"])
def admin_approve():
    uid = session_user_id()
    user = get_user_by_id(uid)
    if not user or not (is_admin_email(user.get("email")) or user.get("role") == "admin"):
        return jsonify(error="unauthorized"), 403
    data = request.get_json(silent=True) or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE codes SET status='approved' WHERE id=%s RETURNING user_id", (data.get("id"),))
    row = cur.fetchone()
    if row and row[0]:
        cur.execute("UPDATE users SET points=COALESCE(points,0)+25 WHERE id=%s", (row[0],))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/admin/code/reject", methods=["POST"])
def admin_reject():
    uid = session_user_id()
    user = get_user_by_id(uid)
    if not user or not (is_admin_email(user.get("email")) or user.get("role") == "admin"):
        return jsonify(error="unauthorized"), 403
    data = request.get_json(silent=True) or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE codes SET status='rejected' WHERE id=%s", (data.get("id"),))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/support/send", methods=["POST"])
def support_send():
    data = request.get_json(silent=True) or {}
    uid = session_user_id() or data.get("user_id")
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify(success=False, error="Message vide"), 400
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO support_messages (user_id, user_name, message) VALUES (%s,%s,%s)",
        (uid, data.get("user_name") or "Membre", msg[:1000]),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/support/list")
def support_list():
    uid = session_user_id()
    user = get_user_by_id(uid)
    if not user or not (is_admin_email(user.get("email")) or user.get("role") == "admin"):
        return jsonify(error="unauthorized"), 403
    status = request.args.get("status") or "open"
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT * FROM support_messages WHERE status=%s ORDER BY id DESC LIMIT 100",
        (status,),
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(messages=rows)


@app.route("/support/reply", methods=["POST"])
def support_reply():
    uid = session_user_id()
    user = get_user_by_id(uid)
    if not user or not (is_admin_email(user.get("email")) or user.get("role") == "admin"):
        return jsonify(error="unauthorized"), 403
    data = request.get_json(silent=True) or {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE support_messages SET admin_reply=%s, status='replied' WHERE id=%s",
        (data.get("reply"), data.get("id")),
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(success=True)


@app.route("/coach/tips")
def coach_tips():
    return jsonify(tips=[{"text": "Publie un code clair : site + code + lien."}])


@app.route("/coach/daily")
def coach_daily():
    return jsonify(challenge={"label": "Partage 1 code utile aujourd’hui", "progress": 0, "target": 1})


@app.route("/stats")
def stats():
    return jsonify(ok=True)


@app.route("/telegram", methods=["POST"])
def telegram():
    return jsonify(success=True)


try:
    init_db()
except Exception as e:
    logging.error("Init DB error: %s", e)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
