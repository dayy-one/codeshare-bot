import logging
import os
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg2
import psycopg2.extras
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
PROTECTED_USERNAMES = {"codiaadmin"}
PROTECTED_EMAILS = {"contact@cod-ia.fr"}
try:
    PRICE_CENTS = int(os.getenv("PRICE_CENTS", "999"))
except ValueError:
    PRICE_CENTS = 999
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
if stripe and STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL est manquante.")
    return psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor, sslmode="require"
    )


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def normalize_email(v):
    return (v or "").strip().lower()


def clean_username(v):
    return re.sub(r"[^a-z0-9_.-]", "", (v or "").strip().lower())[:30]


def as_int(v):
    s = str(v or "").strip()
    return int(s) if s.isdigit() else None


def as_meta(obj):
    if not obj:
        return {}
    raw = obj.get("metadata") if hasattr(obj, "get") else None
    if raw is None and hasattr(obj, "metadata"):
        raw = obj.metadata
    if not raw:
        return {}
    try:
        return {str(k): str(v) for k, v in dict(raw).items() if v is not None}
    except Exception:
        return {}


def ensure_column(cur, table, column, definition):
    cur.execute(
        """SELECT 1 FROM information_schema.columns
           WHERE table_schema='public' AND table_name=%s AND column_name=%s""",
        (table, column),
    )
    if not cur.fetchone():
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def is_protected_user(email=None, username=None, is_admin_flag=False):
    if clean_username(username) in PROTECTED_USERNAMES:
        return True
    if normalize_email(email) in PROTECTED_EMAILS:
        return True
    return False


def cleanup_unpaid(cur):
    emails = list(ADMIN_EMAILS) or ["contact@cod-ia.fr"]
    names = list(PROTECTED_USERNAMES)
    cur.execute(
        """DELETE FROM users
           WHERE COALESCE(is_paid, FALSE) = FALSE
             AND COALESCE(is_admin, FALSE) = FALSE
             AND COALESCE(stripe_session_id, '') = ''
             AND created_at < NOW() - INTERVAL '48 hours'
             AND LOWER(email) <> ALL(%s)
             AND LOWER(username) <> ALL(%s)""",
        (emails, names),
    )
    if cur.rowcount:
        logging.info("Comptes non payés expirés supprimés: %s", cur.rowcount)


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
    return "CODIA" + secrets.token_hex(3).upper()


def init_db():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL,
                bio TEXT DEFAULT '',
                avatar_initials TEXT DEFAULT 'CO',
                avatar_url TEXT DEFAULT '',
                referral_code TEXT UNIQUE NOT NULL,
                referred_by BIGINT,
                is_admin BOOLEAN DEFAULT FALSE,
                is_paid BOOLEAN DEFAULT FALSE,
                is_blocked BOOLEAN DEFAULT FALSE,
                warnings_count INTEGER DEFAULT 0,
                stripe_session_id TEXT,
                hidden_codes JSONB DEFAULT '[]'::jsonb,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW())"""
            )
            for c, d in [
                ("password_hash", "TEXT"),
                ("display_name", "TEXT"),
                ("bio", "TEXT DEFAULT ''"),
                ("avatar_initials", "TEXT DEFAULT 'CO'"),
                ("avatar_url", "TEXT DEFAULT ''"),
                ("referral_code", "TEXT"),
                ("referred_by", "BIGINT"),
                ("is_admin", "BOOLEAN DEFAULT FALSE"),
                ("is_paid", "BOOLEAN DEFAULT FALSE"),
                ("is_blocked", "BOOLEAN DEFAULT FALSE"),
                ("warnings_count", "INTEGER DEFAULT 0"),
                ("stripe_session_id", "TEXT"),
                ("hidden_codes", "JSONB DEFAULT '[]'::jsonb"),
            ]:
                ensure_column(cur, "users", c, d)

            cur.execute(
                """CREATE TABLE IF NOT EXISTS pending_signups (
                id BIGSERIAL PRIMARY KEY,
                email TEXT NOT NULL,
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                referral_code TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW())"""
            )

            cur.execute(
                """CREATE TABLE IF NOT EXISTS codes (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT DEFAULT 'PROMO',
                category TEXT DEFAULT 'Autres',
                brand TEXT DEFAULT '',
                site TEXT DEFAULT '',
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                code TEXT DEFAULT '',
                url TEXT DEFAULT '',
                expires_at TIMESTAMPTZ,
                status TEXT DEFAULT 'VALIDEE',
                likes_count INTEGER DEFAULT 0,
                copies_count INTEGER DEFAULT 0,
                clicks_count INTEGER DEFAULT 0,
                reports_count INTEGER DEFAULT 0,
                copy_reward_awarded BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW())"""
            )
            for c, d in [
                ("kind", "TEXT DEFAULT 'PROMO'"),
                ("category", "TEXT DEFAULT 'Autres'"),
                ("brand", "TEXT DEFAULT ''"),
                ("site", "TEXT DEFAULT ''"),
                ("title", "TEXT"),
                ("description", "TEXT DEFAULT ''"),
                ("code", "TEXT DEFAULT ''"),
                ("url", "TEXT DEFAULT ''"),
                ("expires_at", "TIMESTAMPTZ"),
                ("status", "TEXT DEFAULT 'VALIDEE'"),
                ("likes_count", "INTEGER DEFAULT 0"),
                ("copies_count", "INTEGER DEFAULT 0"),
                ("clicks_count", "INTEGER DEFAULT 0"),
                ("reports_count", "INTEGER DEFAULT 0"),
                ("copy_reward_awarded", "BOOLEAN DEFAULT FALSE"),
            ]:
                ensure_column(cur, "codes", c, d)

            cur.execute(
                """CREATE TABLE IF NOT EXISTS likes(user_id BIGINT, code_id BIGINT, PRIMARY KEY(user_id,code_id));
                CREATE TABLE IF NOT EXISTS favorites(user_id BIGINT, code_id BIGINT, PRIMARY KEY(user_id,code_id));
                CREATE TABLE IF NOT EXISTS reports(id BIGSERIAL PRIMARY KEY, user_id BIGINT, code_id BIGINT, reason TEXT, UNIQUE(user_id,code_id));
                CREATE TABLE IF NOT EXISTS notifications(id BIGSERIAL PRIMARY KEY, user_id BIGINT, title TEXT, message TEXT, type TEXT DEFAULT 'INFO', is_read BOOLEAN DEFAULT FALSE, created_at TIMESTAMPTZ DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS support_tickets(id BIGSERIAL PRIMARY KEY, user_id BIGINT, subject TEXT, message TEXT, reply TEXT DEFAULT '', replied_at TIMESTAMPTZ, replied_by BIGINT, status TEXT DEFAULT 'OPEN', created_at TIMESTAMPTZ DEFAULT NOW());
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_by BIGINT,
                    created_at TIMESTAMPTZ DEFAULT NOW());"""
            )
            ensure_column(cur, "support_tickets", "reply", "TEXT DEFAULT ''")
            ensure_column(cur, "support_tickets", "replied_at", "TIMESTAMPTZ")
            ensure_column(cur, "support_tickets", "replied_by", "BIGINT")
            cur.execute(
                "INSERT INTO settings(key,value) VALUES('challenge_start',%s) ON CONFLICT DO NOTHING",
                (now_utc().isoformat(),),
            )
            cleanup_unpaid(cur)
            cur.execute("DELETE FROM pending_signups WHERE created_at < NOW() - INTERVAL '24 hours'")
            for email in ADMIN_EMAILS:
                cur.execute(
                    "UPDATE users SET is_admin=TRUE, is_paid=TRUE WHERE LOWER(email)=%s",
                    (email,),
                )
            admin_pass = os.getenv("ADMIN_PASSWORD", "")
            if admin_pass:
                for email in ADMIN_EMAILS:
                    uname = "codiaadmin"
                    cur.execute("SELECT id FROM users WHERE LOWER(email)=LOWER(%s)", (email,))
                    if not cur.fetchone():
                        cur.execute(
                            """INSERT INTO users(
                                email,username,password_hash,display_name,avatar_initials,
                                referral_code,is_admin,is_paid
                            ) VALUES(%s,%s,%s,%s,%s,%s,TRUE,TRUE)""",
                            (
                                email,
                                uname,
                                generate_password_hash(admin_pass),
                                "Admin COD.IA",
                                "AD",
                                make_referral_code(),
                            ),
                        )
        conn.commit()
        logging.info("DB initialisée")
    finally:
        conn.close()


def is_admin(user):
    return bool(user) and (
        bool(user.get("is_admin")) or normalize_email(user.get("email")) in ADMIN_EMAILS
    )


def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
            return cur.fetchone()
    finally:
        conn.close()


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"ok": False, "error": "AUTH_REQUIRED"}), 401
        if user.get("is_blocked") and not is_admin(user):
            return jsonify({"ok": False, "error": "Compte bloqué."}), 403
        if not user.get("is_paid") and not is_admin(user):
            return jsonify({"ok": False, "error": "Paiement requis."}), 402
        return fn(user, *args, **kwargs)

    return wrapper


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"ok": False, "error": "AUTH_REQUIRED"}), 401
        if not is_admin(user):
            return jsonify({"ok": False, "error": "Accès admin refusé."}), 403
        return fn(user, *args, **kwargs)

    return wrapper


def json_error(msg, status=400):
    return jsonify({"ok": False, "error": msg}), status


def create_notification(cur, user_id, title, message, kind="INFO"):
    try:
        cur.execute(
            "INSERT INTO notifications(user_id,title,message,type) VALUES(%s,%s,%s,%s)",
            (user_id, title, message, kind),
        )
    except Exception:
        pass


def user_stats(user_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE referred_by=%s", (user_id,))
            refs = int(cur.fetchone()["c"] or 0)
            try:
                cur.execute(
                    """SELECT COALESCE(SUM(likes_count),0) AS likes,
                              COALESCE(SUM(clicks_count),0) AS clicks
                       FROM codes WHERE user_id=%s""",
                    (user_id,),
                )
                row = cur.fetchone() or {}
            except Exception:
                conn.rollback()
                row = {}
        return {
            "referrals": refs,
            "points": refs,
            "likes": int(row.get("likes") or 0),
            "clicks": int(row.get("clicks") or 0),
        }
    finally:
        conn.close()


def challenge_start():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key='challenge_start'")
            row = cur.fetchone()
        return datetime.fromisoformat(row["value"].replace("Z", "+00:00")) if row else now_utc()
    except Exception:
        return now_utc()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def challenge_info(user_id=None):
    start = challenge_start()
    end = start + timedelta(days=21)
    points = 0
    conn = db()
    try:
        with conn.cursor() as cur:
            if user_id:
                cur.execute(
                    """SELECT COUNT(*) AS total FROM users
                       WHERE referred_by=%s AND created_at>=%s AND created_at<=%s""",
                    (user_id, start, end),
                )
                points = int(cur.fetchone()["total"] or 0)
    except Exception:
        pass
    finally:
        conn.close()
    return {"points": points, "remaining_seconds": max(0, int((end - now_utc()).total_seconds()))}


def serialize_code(row, uid=None):
    liked = favorite = False
    added_by = None
    if uid:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM likes WHERE user_id=%s AND code_id=%s", (uid, row["id"])
                )
                liked = bool(cur.fetchone())
                cur.execute(
                    "SELECT 1 FROM favorites WHERE user_id=%s AND code_id=%s",
                    (uid, row["id"]),
                )
                favorite = bool(cur.fetchone())
        finally:
            conn.close()
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT username, display_name FROM users WHERE id=%s", (row.get("user_id"),)
            )
            author = cur.fetchone()
            if author:
                added_by = author.get("display_name") or author.get("username")
    except Exception:
        pass
    finally:
        conn.close()
    return {
        "id": row["id"],
        "kind": row.get("kind") or "PROMO",
        "brand": row.get("brand") or row.get("site"),
        "title": row.get("title"),
        "description": row.get("description"),
        "code": row.get("code"),
        "url": row.get("url"),
        "expires_at": iso(row.get("expires_at")),
        "created_at": iso(row.get("created_at")),
        "likes": row.get("likes_count") or 0,
        "copies": row.get("copies_count") or 0,
        "clicks": row.get("clicks_count") or 0,
        "reports": row.get("reports_count") or 0,
        "liked": liked,
        "favorite": favorite,
        "added_by": added_by or "Membre",
        "owner_id": row.get("user_id"),
    }


def _remove_code(cur, code_id):
    cur.execute("DELETE FROM likes WHERE code_id=%s", (code_id,))
    cur.execute("DELETE FROM favorites WHERE code_id=%s", (code_id,))
    cur.execute("DELETE FROM reports WHERE code_id=%s", (code_id,))
    cur.execute("DELETE FROM codes WHERE id=%s", (code_id,))


def activate_paid_user(pending_id=None, user_id=None, stripe_session_id=None, extra=None):
    extra = extra or {}
    conn = db()
    try:
        with conn.cursor() as cur:
            user = None
            pending = None

            if user_id:
                cur.execute(
                    """UPDATE users SET is_paid=TRUE, stripe_session_id=COALESCE(%s, stripe_session_id)
                       WHERE id=%s RETURNING *""",
                    (stripe_session_id, user_id),
                )
                user = cur.fetchone()

            if not user and pending_id:
                cur.execute("SELECT * FROM pending_signups WHERE id=%s", (pending_id,))
                pending = cur.fetchone()

            email = normalize_email(extra.get("email"))
            if not user and not pending and email:
                cur.execute(
                    "SELECT * FROM pending_signups WHERE LOWER(email)=LOWER(%s) ORDER BY id DESC LIMIT 1",
                    (email,),
                )
                pending = cur.fetchone()

            if not user and email:
                cur.execute("SELECT * FROM users WHERE LOWER(email)=LOWER(%s)", (email,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """UPDATE users SET is_paid=TRUE, stripe_session_id=COALESCE(%s, stripe_session_id)
                           WHERE id=%s RETURNING *""",
                        (stripe_session_id, existing["id"]),
                    )
                    user = cur.fetchone()

            if not user and pending:
                cur.execute(
                    """SELECT * FROM users
                       WHERE LOWER(email)=LOWER(%s) OR LOWER(username)=LOWER(%s)""",
                    (pending["email"], pending["username"]),
                )
                existing = cur.fetchone()
                if existing:
                    cur.execute(
                        """UPDATE users SET is_paid=TRUE, stripe_session_id=%s
                           WHERE id=%s RETURNING *""",
                        (stripe_session_id, existing["id"]),
                    )
                    user = cur.fetchone()
                else:
                    referred_by = None
                    if pending.get("referral_code"):
                        cur.execute(
                            "SELECT id FROM users WHERE referral_code=%s",
                            (pending["referral_code"],),
                        )
                        ref = cur.fetchone()
                        if ref:
                            referred_by = ref["id"]
                    initials = (
                        "".join(
                            x[0]
                            for x in (pending.get("display_name") or pending["username"]).split()
                            if x
                        )[:2].upper()
                        or "CO"
                    )
                    admin = is_protected_user(pending["email"], pending["username"])
                    cur.execute(
                        """INSERT INTO users(
                            email,username,password_hash,display_name,avatar_initials,
                            referral_code,referred_by,is_admin,is_paid,stripe_session_id
                        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,TRUE,%s) RETURNING *""",
                        (
                            pending["email"],
                            pending["username"],
                            pending["password_hash"],
                            pending.get("display_name") or pending["username"],
                            initials,
                            make_referral_code(),
                            referred_by,
                            admin,
                            stripe_session_id,
                        ),
                    )
                    user = cur.fetchone()
                    if referred_by:
                        create_notification(
                            cur,
                            referred_by,
                            "Nouveau parrainage",
                            f"{user['display_name']} a rejoint avec ton code. +1 point.",
                            "REFERRAL",
                        )
                cur.execute("DELETE FROM pending_signups WHERE id=%s", (pending["id"],))

            conn.commit()
            return user
    except Exception as exc:
        logging.error("ACTIVATE PAID: %s", exc)
        conn.rollback()
        return None
    finally:
        conn.close()


@app.route("/")
def landing():
    return send_from_directory(BASE_DIR, "landing.html")


@app.route("/app")
@app.route("/miniapp")
def miniapp():
    return send_from_directory(BASE_DIR, "miniapp.html")


DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "CodiaAdmin2026!")


@app.route("/dashboard", methods=["GET", "POST"])
@app.route("/dashboard.html", methods=["GET", "POST"])
def dashboard():
    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        if password == DASHBOARD_PASSWORD:
            session["dashboard_ok"] = True
            return redirect("/dashboard")
        error = "Mot de passe incorrect."
    else:
        error = ""

    if session.get("dashboard_ok"):
        return send_from_directory(BASE_DIR, "dashboard.html")

    err_html = f'<p style="color:#ff6b6b;margin:0 0 12px">{error}</p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>COD.IA — Accès stats</title>
  <style>
    body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
         background:#07080d;font-family:Inter,system-ui,sans-serif;color:#f4f6fb}}
    form{{width:min(380px,92vw);background:#12141c;border:1px solid #232636;
          border-radius:18px;padding:28px}}
    h1{{margin:0 0 8px;font-size:22px}}
    p{{color:#9aa3b8;margin:0 0 18px}}
    input{{width:100%;box-sizing:border-box;padding:12px 14px;border-radius:12px;
           border:1px solid #232636;background:#0c0e14;color:#fff;font-size:15px}}
    button{{width:100%;margin-top:12px;padding:12px;border:0;border-radius:12px;
            background:#7c5cff;color:#fff;font-weight:700;cursor:pointer}}
  </style>
</head>
<body>
  <form method="post">
    <h1>Accès stats</h1>
    <p>Entre le mot de passe pour voir l’analyse.</p>
    {err_html}
    <input type="password" name="password" placeholder="Mot de passe" autofocus required>
    <button type="submit">Entrer</button>
  </form>
</body>
</html>"""


@app.route("/logout")
def logout_get():
    session.clear()
    return redirect("/")


@app.get("/config")
@app.get("/api/stripe-config")
def config():
    return jsonify(
        {"ok": True, "stripe_pk": STRIPE_PUBLISHABLE_KEY, "publishable_key": STRIPE_PUBLISHABLE_KEY}
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.post("/api/register")
@app.post("/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    email = normalize_email(data.get("email"))
    username = clean_username(data.get("username"))
    password = data.get("password") or ""
    display_name = (data.get("display_name") or username).strip()
    referral_code = (data.get("referral_code") or "").strip().upper()
    if "@" not in email:
        return json_error("Adresse email invalide.")
    if len(username) < 3:
        return json_error("Nom utilisateur trop court.")
    if len(password) < 8:
        return json_error("Mot de passe : 8 caractères minimum.")
    if is_protected_user(email, username):
        return json_error("Ce compte est réservé.")

    password_hash = generate_password_hash(password)
    conn = db()
    try:
        with conn.cursor() as cur:
            cleanup_unpaid(cur)
            cur.execute(
                """SELECT * FROM users
                   WHERE LOWER(email)=LOWER(%s) OR LOWER(username)=LOWER(%s)""",
                (email, username),
            )
            existing = cur.fetchone()
            if existing:
                if existing.get("is_paid") or is_admin(existing):
                    return json_error("Cet email ou ce nom utilisateur existe déjà.", 409)
                cur.execute(
                    """UPDATE users
                       SET password_hash=%s, display_name=%s, email=%s, username=%s
                       WHERE id=%s RETURNING *""",
                    (
                        password_hash,
                        display_name or existing.get("display_name") or username,
                        email,
                        username,
                        existing["id"],
                    ),
                )
                user = cur.fetchone()
            else:
                referred_by = None
                if referral_code:
                    cur.execute("SELECT id FROM users WHERE referral_code=%s", (referral_code,))
                    ref = cur.fetchone()
                    if ref:
                        referred_by = ref["id"]
                initials = (
                    "".join(x[0] for x in (display_name or username).split() if x)[:2].upper()
                    or "CO"
                )
                cur.execute(
                    """INSERT INTO users(
                        email,username,password_hash,display_name,avatar_initials,
                        referral_code,referred_by,is_admin,is_paid
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE) RETURNING *""",
                    (
                        email,
                        username,
                        password_hash,
                        display_name or username,
                        initials,
                        make_referral_code(),
                        referred_by,
                    ),
                )
                user = cur.fetchone()

            cur.execute(
                """DELETE FROM pending_signups
                   WHERE LOWER(email)=LOWER(%s) OR LOWER(username)=LOWER(%s)""",
                (email, username),
            )
            cur.execute(
                """INSERT INTO pending_signups(email,username,password_hash,display_name,referral_code)
                   VALUES(%s,%s,%s,%s,%s) RETURNING id""",
                (email, username, password_hash, display_name or username, referral_code),
            )
            pending_id = cur.fetchone()["id"]
        conn.commit()
    except Exception as exc:
        logging.error("REGISTER ERROR: %s", exc)
        conn.rollback()
        return json_error("Inscription impossible : " + str(exc), 500)
    finally:
        conn.close()

    session.permanent = True
    session["user_id"] = user["id"]
    session["pending_id"] = pending_id
    session["pending_email"] = email
    session["pending_username"] = username
    return jsonify(
        {
            "ok": True,
            "pending": True,
            "user": {
                "id": user["id"],
                "email": email,
                "username": username,
                "is_paid": False,
                "is_admin": False,
            },
        }
    )


@app.post("/api/cancel-signup")
def cancel_signup():
    pending_id = session.pop("pending_id", None)
    session.pop("pending_email", None)
    session.pop("pending_username", None)
    if pending_id:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM pending_signups WHERE id=%s", (pending_id,))
            conn.commit()
        finally:
            conn.close()
    return jsonify({"ok": True})


@app.post("/api/login")
@app.post("/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip()
    password = data.get("password") or ""
    conn = db()
    try:
        with conn.cursor() as cur:
            cleanup_unpaid(cur)
            conn.commit()
            cur.execute(
                """SELECT * FROM users
                   WHERE LOWER(email)=LOWER(%s) OR LOWER(username)=LOWER(%s)""",
                (identifier, identifier.lstrip("@")),
            )
            user = cur.fetchone()
    finally:
        conn.close()
    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        return json_error("Email/username ou mot de passe incorrect.", 401)
    admin = is_admin(user)
    if user.get("is_blocked") and not admin:
        return json_error("Ce compte a été bloqué.", 403)
    if not user.get("is_paid") and not admin:
        session.permanent = True
        session["user_id"] = user["id"]
        session["pending_email"] = user.get("email")
        return json_error("Ce compte n'est pas activé. Inscris-toi puis paie 9,99 €.", 401)
    session.permanent = True
    session["user_id"] = user["id"]
    session.pop("pending_id", None)
    return jsonify(
        {
            "ok": True,
            "user": {
                "id": user["id"],
                "email": user.get("email"),
                "is_paid": True,
                "is_admin": admin,
            },
        }
    )


@app.get("/api/me")
def me_any():
    sid = request.args.get("session_id") or ""
    if sid and stripe and not session.get("user_id"):
        try:
            checkout = stripe.checkout.Session.retrieve(sid)
            status = str(checkout.get("payment_status") or "").lower()
            if status in ("paid", "no_payment_required"):
                meta = as_meta(checkout)
                user = activate_paid_user(
                    pending_id=as_int(
                        meta.get("pending_id")
                        or session.get("pending_id")
                        or checkout.get("client_reference_id")
                    ),
                    user_id=as_int(meta.get("user_id")),
                    stripe_session_id=sid,
                    extra=meta,
                )
                if user:
                    session["user_id"] = user["id"]
                    session.pop("pending_id", None)
                    session.pop("pending_email", None)
                    session.pop("pending_username", None)
                    session.permanent = True
        except Exception as exc:
            logging.error("ME CONFIRM: %s", exc)

    user = get_current_user()
    if not user:
        pending_id = session.get("pending_id")
        email = normalize_email(session.get("pending_email"))
        conn = db()
        try:
            with conn.cursor() as cur:
                if not email and pending_id:
                    cur.execute("SELECT email FROM pending_signups WHERE id=%s", (pending_id,))
                    row = cur.fetchone()
                    if row:
                        email = normalize_email(row.get("email"))
                if email:
                    cur.execute(
                        """SELECT * FROM users
                           WHERE LOWER(email)=LOWER(%s)
                           ORDER BY id DESC LIMIT 1""",
                        (email,),
                    )
                    user = cur.fetchone()
                    if user and (user.get("is_paid") or is_admin(user)):
                        session["user_id"] = user["id"]
                        session.pop("pending_id", None)
                        session.pop("pending_email", None)
                        session.pop("pending_username", None)
                        session.permanent = True
                    else:
                        user = None
        finally:
            conn.close()

    if not user:
        return jsonify({"ok": True, "user": None, "pending": bool(session.get("pending_id"))})
    admin = is_admin(user)
    if user.get("is_blocked") and not admin:
        session.clear()
        return jsonify({"ok": False, "error": "Compte bloqué.", "blocked": True})
    paid = bool(user.get("is_paid")) or admin
    if not paid:
        return jsonify({"ok": True, "user": None, "pending": True})
    return jsonify(
        {
            "ok": True,
            "user": {
                "id": user["id"],
                "email": user.get("email"),
                "username": user.get("username"),
                "display_name": user.get("display_name"),
                "bio": user.get("bio") or "",
                "avatar_initials": user.get("avatar_initials") or "CO",
                "avatar_url": user.get("avatar_url") or "",
                "referral_code": user.get("referral_code"),
                "referral_link": f"{SERVER_URL}/app?ref={user.get('referral_code') or ''}",
                "is_admin": admin,
                "is_paid": True,
                "paid": True,
                "is_blocked": bool(user.get("is_blocked")),
            },
            "stats": user_stats(user["id"]),
            "challenge": challenge_info(user["id"]),
        }
    )


@app.post("/api/create-checkout")
def create_checkout():
    user = get_current_user()
    pending_id = session.get("pending_id")
    if user and (user.get("is_paid") or is_admin(user)):
        return jsonify({"ok": True, "already_paid": True})
    if not pending_id and not user:
        return json_error("Inscris-toi d'abord.", 401)
    if not stripe or not STRIPE_SECRET_KEY:
        return json_error("Stripe n'est pas configuré sur Railway.", 503)
    items = (
        [{"price": STRIPE_PRICE_ID, "quantity": 1}]
        if STRIPE_PRICE_ID
        else [
            {
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": "Accès COD.IA"},
                    "unit_amount": PRICE_CENTS,
                },
                "quantity": 1,
            }
        ]
    )
    return_url = f"{SERVER_URL}/app?paid=1&session_id={{CHECKOUT_SESSION_ID}}"
    pending_email = session.get("pending_email") or (user.get("email") if user else "")
    pending_username = session.get("pending_username") or (user.get("username") if user else "")
    if pending_id:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT email, username FROM pending_signups WHERE id=%s",
                    (pending_id,),
                )
                row = cur.fetchone()
                if row:
                    pending_email = row.get("email") or pending_email
                    pending_username = row.get("username") or pending_username
        finally:
            conn.close()

    meta = {}
    if pending_id:
        meta["pending_id"] = str(pending_id)
    if user:
        meta["user_id"] = str(user["id"])
    if pending_email:
        meta["email"] = pending_email
    if pending_username:
        meta["username"] = pending_username

    try:
        checkout = stripe.checkout.Session.create(
            mode="payment",
            line_items=items,
            ui_mode="embedded_page",
            return_url=return_url,
            client_reference_id=str(pending_id or (user["id"] if user else "")),
            metadata=meta,
        )
        secret = getattr(checkout, "client_secret", None)
        if not secret:
            raise RuntimeError("Stripe n'a pas renvoyé de client_secret")
        return jsonify({"ok": True, "client_secret": secret, "clientSecret": secret})
    except Exception as exc:
        logging.exception("STRIPE CHECKOUT ERROR")
        return json_error("Stripe : " + str(exc), 500)


@app.get("/api/confirm-payment")
def confirm_payment():
    sid = request.args.get("session_id") or ""
    if not (sid and stripe):
        return jsonify({"ok": False, "paid": False})
    try:
        checkout = stripe.checkout.Session.retrieve(sid)
        status = str(checkout.get("payment_status") or "").lower()
        if status not in ("paid", "no_payment_required"):
            return jsonify({"ok": False, "paid": False, "status": status})

        meta = as_meta(checkout)
        pending_id = as_int(
            meta.get("pending_id")
            or session.get("pending_id")
            or checkout.get("client_reference_id")
        )
        user_id = as_int(meta.get("user_id"))
        if not meta.get("email"):
            meta["email"] = session.get("pending_email") or ""
        if not meta.get("username"):
            meta["username"] = session.get("pending_username") or ""

        user = activate_paid_user(
            pending_id=pending_id,
            user_id=user_id,
            stripe_session_id=sid,
            extra=meta,
        )
        if not user:
            return jsonify({"ok": False, "paid": False, "error": "Compte introuvable après paiement"})

        session["user_id"] = user["id"]
        session.pop("pending_id", None)
        session.pop("pending_email", None)
        session.pop("pending_username", None)
        session.permanent = True
        return jsonify({"ok": True, "paid": True, "email": user.get("email")})
    except Exception as exc:
        logging.error("Confirm payment: %s", exc)
        return jsonify({"ok": False, "paid": False, "error": str(exc)})


@app.post("/stripe/webhook")
def stripe_webhook():
    if not stripe:
        return "no stripe", 503
    try:
        event = (
            stripe.Webhook.construct_event(
                request.data, request.headers.get("Stripe-Signature"), STRIPE_WEBHOOK_SECRET
            )
            if STRIPE_WEBHOOK_SECRET
            else stripe.Event.construct_from(request.json, stripe.api_key)
        )
    except Exception:
        return "invalid", 400
    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        obj = event["data"]["object"]
        meta = as_meta(obj)
        pending_id = as_int(meta.get("pending_id") or obj.get("client_reference_id"))
        activate_paid_user(
            pending_id=pending_id,
            user_id=as_int(meta.get("user_id")),
            stripe_session_id=obj.get("id"),
            extra=meta,
        )
    return "ok", 200
