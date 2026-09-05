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
        logging.info("Comptes non payés supprimés: %s", cur.rowcount)


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
            username = clean_username(extra.get("username"))

            if not user and not pending and email:
                cur.execute(
                    "SELECT * FROM pending_signups WHERE LOWER(email)=LOWER(%s) ORDER BY id DESC LIMIT 1",
                    (email,),
                )
                pending = cur.fetchone()

            if not user and not pending and email:
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


@app.route("/login")
@app.route("/register")
@app.route("/auth")
def auth_page():
    return send_from_directory(BASE_DIR, "login.html")


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
                    """UPDATE users SET password_hash=%s, display_name=%s, email=%s, username=%s
                       WHERE id=%s RETURNING *""",
                    (password_hash, display_name or username, email, username, existing["id"]),
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


@app.post("/api/apply-referral")
def apply_referral():
    data = request.get_json(silent=True) or {}
    skip = bool(data.get("skip"))
    code = (data.get("referral_code") or "").strip().upper()
    user = get_current_user()
    pending_id = session.get("pending_id")

    if skip:
        session["referral_done"] = True
        return jsonify({"ok": True, "skipped": True})

    if not code:
        return json_error("Entre un code de parrainage.")

    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, display_name FROM users WHERE referral_code=%s", (code,))
            ref = cur.fetchone()
            if not ref:
                return json_error("Ce code de parrainage n'existe pas.")

            if user:
                if user["id"] == ref["id"]:
                    return json_error("Tu ne peux pas utiliser ton propre code.")
                if user.get("referred_by"):
                    session["referral_done"] = True
                    return jsonify({"ok": True, "already": True})
                cur.execute(
                    "UPDATE users SET referred_by=%s WHERE id=%s AND referred_by IS NULL",
                    (ref["id"], user["id"]),
                )
                create_notification(
                    cur,
                    ref["id"],
                    "Nouveau parrainage",
                    f"{user.get('display_name') or user.get('username')} a rejoint avec ton code. +1 point.",
                    "REFERRAL",
                )
            elif pending_id:
                cur.execute(
                    "UPDATE pending_signups SET referral_code=%s WHERE id=%s",
                    (code, pending_id),
                )
            else:
                return json_error("Connecte-toi d'abord.", 401)
        conn.commit()
    finally:
        conn.close()

    session["referral_done"] = True
    return jsonify({"ok": True})


@app.post("/api/cancel-signup")
def cancel_signup():
    pending_id = session.pop("pending_id", None)
    session.pop("user_id", None)
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
    user = get_current_user()
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
                "is_paid": paid,
                "paid": paid,
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
    pending_email = session.get("pending_email") or ""
    pending_username = session.get("pending_username") or ""
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
        meta["email"] = user.get("email") or ""
        meta["username"] = user.get("username") or ""
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


@app.get("/api/feed")
@require_auth
def feed(user):
    search = (request.args.get("search") or "").strip()
    category = (request.args.get("category") or "TOUS").upper()
    kind = (request.args.get("kind") or "").upper()
    hidden = user.get("hidden_codes") or []
    conn = db()
    try:
        with conn.cursor() as cur:
            params = [user["id"]]
            q = """SELECT * FROM codes c
                   WHERE COALESCE(status,'VALIDEE')='VALIDEE'
                     AND (expires_at IS NULL OR expires_at>NOW())
                     AND NOT EXISTS (
                        SELECT 1 FROM reports r
                        WHERE r.user_id=%s AND r.code_id=c.id
                     )"""
            if hidden:
                q += " AND id <> ALL(%s)"
                params.append(hidden)
            if category not in ("TOUS", ""):
                q += " AND UPPER(category)=UPPER(%s)"
                params.append(category)
            if kind in ("PROMO", "PARRAINAGE"):
                q += " AND kind=%s"
                params.append(kind)
            if search:
                q += """ AND (title ILIKE %s OR description ILIKE %s
                              OR COALESCE(brand,site,'') ILIKE %s OR code ILIKE %s)"""
                t = f"%{search}%"
                params.extend([t, t, t, t])
            q += " ORDER BY created_at DESC LIMIT 80"
            cur.execute(q, params)
            rows = cur.fetchall()
        return jsonify({"ok": True, "codes": [serialize_code(r, user["id"]) for r in rows]})
    except Exception as exc:
        logging.error("FEED: %s", exc)
        return jsonify({"ok": True, "codes": []})
    finally:
        conn.close()


@app.get("/api/top-codes")
def top_codes():
    user = get_current_user()
    uid = user["id"] if user else None
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT * FROM codes
                   WHERE COALESCE(likes_count,0)>=100 OR COALESCE(copies_count,0)>=100
                   ORDER BY (COALESCE(likes_count,0)+COALESCE(copies_count,0)) DESC
                   LIMIT 30"""
            )
            rows = cur.fetchall()
        return jsonify({"ok": True, "codes": [serialize_code(r, uid) for r in rows]})
    finally:
        conn.close()


@app.get("/api/codes/<int:code_id>")
@require_auth
def get_code(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM codes WHERE id=%s", (code_id,))
            row = cur.fetchone()
        if not row:
            return json_error("Code introuvable.", 404)
        return jsonify({"ok": True, "code": serialize_code(row, user["id"])})
    finally:
        conn.close()


@app.post("/api/codes")
@require_auth
def create_code(user):
    data = request.get_json(silent=True) or {}
    kind = "PARRAINAGE" if (data.get("kind") or "").upper() == "PARRAINAGE" else "PROMO"
    title = (data.get("title") or data.get("brand") or "").strip()
    code = (data.get("code") or "").strip()
    brand = (data.get("brand") or title).strip()
    if len(title) < 2 or not code:
        return json_error("Marque/titre et code sont obligatoires.")
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
                """INSERT INTO codes(user_id,kind,category,brand,site,title,description,code,url,expires_at,status)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'VALIDEE') RETURNING *""",
                (
                    user["id"],
                    kind,
                    data.get("category") or "Autres",
                    brand,
                    brand,
                    title,
                    data.get("description") or "",
                    code,
                    data.get("url") or "",
                    expires,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return jsonify({"ok": True, "code": serialize_code(row, user["id"])})
    except Exception as exc:
        logging.error("CREATE CODE: %s", exc)
        conn.rollback()
        return json_error("Publication impossible : " + str(exc), 500)
    finally:
        conn.close()


def _delete_code_impl(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM codes WHERE id=%s", (code_id,))
            row = cur.fetchone()
            if not row:
                return json_error("Code introuvable.", 404)
            if row["user_id"] != user["id"] and not is_admin(user):
                return json_error("Tu ne peux supprimer que tes codes.", 403)
            _remove_code(cur, code_id)
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.delete("/api/codes/<int:code_id>")
@require_auth
def delete_code(user, code_id):
    return _delete_code_impl(user, code_id)


@app.post("/api/codes/<int:code_id>/delete")
@require_auth
def delete_code_post(user, code_id):
    return _delete_code_impl(user, code_id)


@app.post("/api/codes/<int:code_id>/copy")
@require_auth
def copy_code(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM codes WHERE id=%s", (code_id,))
            code = cur.fetchone()
            if not code:
                return json_error("Code introuvable.", 404)
            cur.execute(
                "UPDATE codes SET copies_count=COALESCE(copies_count,0)+1 WHERE id=%s",
                (code_id,),
            )
        conn.commit()
        return jsonify({"ok": True, "code": code.get("code"), "url": code.get("url")})
    finally:
        conn.close()


@app.post("/api/codes/<int:code_id>/click")
@require_auth
def click_code(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE codes SET clicks_count=COALESCE(clicks_count,0)+1 WHERE id=%s",
                (code_id,),
            )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/codes/<int:code_id>/like")
@require_auth
def like_code(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM likes WHERE user_id=%s AND code_id=%s", (user["id"], code_id)
            )
            if cur.fetchone():
                cur.execute(
                    "DELETE FROM likes WHERE user_id=%s AND code_id=%s",
                    (user["id"], code_id),
                )
                cur.execute(
                    "UPDATE codes SET likes_count=GREATEST(COALESCE(likes_count,0)-1,0) WHERE id=%s",
                    (code_id,),
                )
            else:
                cur.execute(
                    "INSERT INTO likes(user_id,code_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (user["id"], code_id),
                )
                cur.execute(
                    "UPDATE codes SET likes_count=COALESCE(likes_count,0)+1 WHERE id=%s",
                    (code_id,),
                )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/codes/<int:code_id>/favorite")
@require_auth
def favorite_code(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM favorites WHERE user_id=%s AND code_id=%s",
                (user["id"], code_id),
            )
            exists = bool(cur.fetchone())
            if exists:
                cur.execute(
                    "DELETE FROM favorites WHERE user_id=%s AND code_id=%s",
                    (user["id"], code_id),
                )
            else:
                cur.execute(
                    "INSERT INTO favorites(user_id,code_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (user["id"], code_id),
                )
        conn.commit()
        return jsonify({"ok": True, "favorite": not exists})
    finally:
        conn.close()


@app.post("/api/codes/<int:code_id>/hide")
@require_auth
def hide_code(user, code_id):
    hidden = list(user.get("hidden_codes") or [])
    if code_id not in hidden:
        hidden.append(code_id)
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET hidden_codes=%s WHERE id=%s",
                (psycopg2.extras.Json(hidden), user["id"]),
            )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/favorites")
@require_auth
def favorites(user):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.* FROM codes c
                   JOIN favorites f ON f.code_id=c.id
                   WHERE f.user_id=%s""",
                (user["id"],),
            )
            rows = cur.fetchall()
        return jsonify({"ok": True, "codes": [serialize_code(r, user["id"]) for r in rows]})
    finally:
        conn.close()


@app.get("/api/my-codes")
@require_auth
def my_codes(user):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM codes WHERE user_id=%s ORDER BY created_at DESC",
                (user["id"],),
            )
            rows = cur.fetchall()
        return jsonify({"ok": True, "codes": [serialize_code(r, user["id"]) for r in rows]})
    finally:
        conn.close()


@app.post("/api/codes/<int:code_id>/report")
@require_auth
def report_code(user, code_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO reports(user_id,code_id,reason)
                   VALUES(%s,%s,%s) ON CONFLICT DO NOTHING""",
                (user["id"], code_id, "Signalement"),
            )
            cur.execute("SELECT COUNT(*) AS c FROM reports WHERE code_id=%s", (code_id,))
            total = int(cur.fetchone()["c"])
            cur.execute("UPDATE codes SET reports_count=%s WHERE id=%s", (total, code_id))
            if total >= 10:
                cur.execute("UPDATE codes SET status='SUPPRIMEE' WHERE id=%s", (code_id,))
        conn.commit()
        return jsonify({"ok": True, "removed": total >= 10})
    finally:
        conn.close()


@app.patch("/api/profile")
@require_auth
def update_profile(user):
    data = request.get_json(silent=True) or {}
    bio = str(data.get("bio") if data.get("bio") is not None else user.get("bio") or "")[:300]
    email = normalize_email(
        data.get("email") if data.get("email") is not None else user.get("email")
    )
    avatar_url = (
        data.get("avatar_url") if data.get("avatar_url") is not None else user.get("avatar_url")
    )
    if avatar_url and len(str(avatar_url)) > 350000:
        return json_error("Photo trop lourde.")
    if "@" not in email:
        return json_error("Adresse email invalide.")
    conn = db()
    try:
        with conn.cursor() as cur:
            if email != normalize_email(user.get("email")):
                cur.execute(
                    "SELECT id FROM users WHERE LOWER(email)=LOWER(%s) AND id<>%s",
                    (email, user["id"]),
                )
                if cur.fetchone():
                    return json_error("Cet email est déjà utilisé.")
            cur.execute(
                """UPDATE users SET bio=%s, email=%s, avatar_url=%s, updated_at=NOW()
                   WHERE id=%s""",
                (bio, email, avatar_url or "", user["id"]),
            )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/leaderboard")
def leaderboard():
    user = get_current_user()
    start = challenge_start()
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.id,
                          COALESCE(u.display_name,u.username,'Membre') AS name,
                          COALESCE(u.avatar_initials,'CO') AS initials,
                          COUNT(r.id) AS points
                   FROM users u
                   LEFT JOIN users r
                     ON r.referred_by=u.id AND r.created_at>=%s AND r.created_at<=%s
                   GROUP BY u.id,u.display_name,u.username,u.avatar_initials
                   ORDER BY points DESC
                   LIMIT 3""",
                (start, start + timedelta(days=21)),
            )
            rows = cur.fetchall()
        challenge = [
            {
                "rank": i,
                "name": r["name"],
                "initials": r.get("initials") or "CO",
                "points": int(r["points"] or 0),
                "me": bool(user and r["id"] == user["id"]),
            }
            for i, r in enumerate(rows, 1)
        ]
        return jsonify({"ok": True, "challenge": challenge, "weekly": []})
    finally:
        conn.close()


@app.get("/api/notifications")
@require_auth
def notifications(user):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT title, message, created_at, 'USER' AS kind
                   FROM notifications
                   WHERE user_id=%s
                   ORDER BY created_at DESC LIMIT 50""",
                (user["id"],),
            )
            rows = list(cur.fetchall() or [])
            cur.execute(
                """SELECT title, message, created_at, 'BROADCAST' AS kind
                   FROM broadcasts
                   WHERE created_at > NOW() - INTERVAL '24 hours'
                   ORDER BY created_at DESC"""
            )
            rows = list(cur.fetchall() or []) + rows
            cur.execute(
                """SELECT COUNT(*) AS c FROM notifications
                   WHERE user_id=%s AND COALESCE(is_read,FALSE)=FALSE""",
                (user["id"],),
            )
            unread = int(cur.fetchone()["c"] or 0)
            cur.execute(
                """SELECT COUNT(*) AS c FROM broadcasts
                   WHERE created_at > NOW() - INTERVAL '24 hours'"""
            )
            unread += int(cur.fetchone()["c"] or 0)
        rows.sort(key=lambda r: r.get("created_at") or now_utc(), reverse=True)
        return jsonify(
            {
                "ok": True,
                "unread": unread,
                "notifications": [
                    {
                        "title": r.get("title"),
                        "message": r.get("message"),
                        "kind": r.get("kind"),
                    }
                    for r in rows[:50]
                ],
            }
        )
    except Exception:
        return jsonify({"ok": True, "unread": 0, "notifications": []})
    finally:
        conn.close()


@app.post("/api/notifications/read")
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
@require_auth
def support(user):
    data = request.get_json(silent=True) or {}
    if len((data.get("message") or "")) < 3:
        return json_error("Message trop court.")
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO support_tickets(user_id,subject,message) VALUES(%s,%s,%s)",
                (user["id"], data.get("subject") or "Support", data.get("message")),
            )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/support")
@require_auth
def my_support(user):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id,subject,message,reply,status,created_at,replied_at
                   FROM support_tickets WHERE user_id=%s
                   ORDER BY created_at DESC LIMIT 50""",
                (user["id"],),
            )
            rows = cur.fetchall()
        return jsonify(
            {
                "ok": True,
                "tickets": [
                    {
                        "id": r["id"],
                        "subject": r.get("subject"),
                        "message": r.get("message"),
                        "reply": r.get("reply") or "",
                        "status": r.get("status") or "OPEN",
                        "created_at": iso(r.get("created_at")),
                        "replied_at": iso(r.get("replied_at")),
                    }
                    for r in rows
                ],
            }
        )
    finally:
        conn.close()


@app.get("/api/admin/overview")
@require_admin
def admin_overview(admin):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM users")
            users_n = int(cur.fetchone()["c"] or 0)
            cur.execute("SELECT COUNT(*) AS c FROM users WHERE COALESCE(is_paid,FALSE)=TRUE")
            paid_n = int(cur.fetchone()["c"] or 0)
            cur.execute("SELECT COUNT(*) AS c FROM codes")
            codes_n = int(cur.fetchone()["c"] or 0)
            cur.execute(
                "SELECT COUNT(*) AS c FROM support_tickets WHERE COALESCE(status,'OPEN')='OPEN'"
            )
            tickets_n = int(cur.fetchone()["c"] or 0)
        return jsonify(
            {
                "ok": True,
                "stats": {
                    "users": users_n,
                    "paid": paid_n,
                    "codes": codes_n,
                    "tickets": tickets_n,
                },
            }
        )
    finally:
        conn.close()


@app.get("/api/admin/users")
@require_admin
def admin_users(admin):
    q = (request.args.get("q") or "").strip()
    conn = db()
    try:
        with conn.cursor() as cur:
            if q:
                like = f"%{q}%"
                cur.execute(
                    """SELECT id,email,username,display_name,is_admin,is_paid,is_blocked,
                              warnings_count,referral_code,created_at
                       FROM users
                       WHERE email ILIKE %s OR username ILIKE %s OR display_name ILIKE %s
                       ORDER BY created_at DESC LIMIT 200""",
                    (like, like, like),
                )
            else:
                cur.execute(
                    """SELECT id,email,username,display_name,is_admin,is_paid,is_blocked,
                              warnings_count,referral_code,created_at
                       FROM users ORDER BY created_at DESC LIMIT 200"""
                )
            rows = cur.fetchall()
        return jsonify(
            {
                "ok": True,
                "users": [
                    {
                        "id": r["id"],
                        "email": r.get("email"),
                        "username": r.get("username"),
                        "display_name": r.get("display_name"),
                        "is_admin": bool(r.get("is_admin")),
                        "is_paid": bool(r.get("is_paid")),
                        "is_blocked": bool(r.get("is_blocked")),
                        "warnings_count": int(r.get("warnings_count") or 0),
                        "referral_code": r.get("referral_code"),
                        "created_at": iso(r.get("created_at")),
                        "protected": is_protected_user(
                            r.get("email"), r.get("username"), r.get("is_admin")
                        ),
                    }
                    for r in rows
                ],
            }
        )
    finally:
        conn.close()


@app.post("/api/admin/users/<int:user_id>/toggle-paid")
@require_admin
def admin_toggle_paid(admin, user_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if not row:
                return json_error("Utilisateur introuvable.", 404)
            if is_protected_user(row.get("email"), row.get("username"), row.get("is_admin")):
                return json_error("Compte protégé.")
            new_val = not bool(row.get("is_paid"))
            cur.execute("UPDATE users SET is_paid=%s WHERE id=%s", (new_val, user_id))
        conn.commit()
        return jsonify({"ok": True, "is_paid": new_val})
    finally:
        conn.close()


@app.post("/api/admin/users/<int:user_id>/block")
@require_admin
def admin_block_user(admin, user_id):
    if user_id == admin["id"]:
        return json_error("Tu ne peux pas te bloquer.")
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if not row:
                return json_error("Utilisateur introuvable.", 404)
            if is_protected_user(row.get("email"), row.get("username"), row.get("is_admin")):
                return json_error("Compte protégé.")
            blocked = not bool(row.get("is_blocked"))
            cur.execute("UPDATE users SET is_blocked=%s WHERE id=%s", (blocked, user_id))
            if blocked:
                create_notification(
                    cur,
                    user_id,
                    "Compte bloqué",
                    "Ton compte a été bloqué par l’équipe COD.IA.",
                    "ALERT",
                )
        conn.commit()
        return jsonify({"ok": True, "is_blocked": blocked})
    finally:
        conn.close()


@app.post("/api/admin/users/<int:user_id>/warn")
@require_admin
def admin_warn_user(admin, user_id):
    data = request.get_json(silent=True) or {}
    reason = (data.get("reason") or data.get("message") or "").strip()
    if len(reason) < 3:
        return json_error("Précise le motif du signalement.")
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if not row:
                return json_error("Utilisateur introuvable.", 404)
            if user_id == admin["id"]:
                return json_error("Tu ne peux pas te signaler.")
            cur.execute(
                "UPDATE users SET warnings_count=COALESCE(warnings_count,0)+1 WHERE id=%s RETURNING warnings_count",
                (user_id,),
            )
            count = int(cur.fetchone()["warnings_count"] or 1)
            create_notification(
                cur,
                user_id,
                "Signalement COD.IA",
                reason[:400],
                "ALERT",
            )
        conn.commit()
        return jsonify({"ok": True, "warnings_count": count})
    finally:
        conn.close()


@app.post("/api/admin/users/<int:user_id>/delete")
@require_admin
def admin_delete_user(admin, user_id):
    if user_id == admin["id"]:
        return json_error("Tu ne peux pas te supprimer.")
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
            row = cur.fetchone()
            if not row:
                return json_error("Utilisateur introuvable.", 404)
            if is_protected_user(row.get("email"), row.get("username"), row.get("is_admin")):
                return json_error("Compte protégé.")
            cur.execute("SELECT id FROM codes WHERE user_id=%s", (user_id,))
            for c in cur.fetchall():
                _remove_code(cur, c["id"])
            cur.execute("DELETE FROM likes WHERE user_id=%s", (user_id,))
            cur.execute("DELETE FROM favorites WHERE user_id=%s", (user_id,))
            cur.execute("DELETE FROM reports WHERE user_id=%s", (user_id,))
            cur.execute("DELETE FROM notifications WHERE user_id=%s", (user_id,))
            cur.execute("DELETE FROM support_tickets WHERE user_id=%s", (user_id,))
            cur.execute("UPDATE users SET referred_by=NULL WHERE referred_by=%s", (user_id,))
            cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/admin/codes")
@require_admin
def admin_codes(admin):
    q = (request.args.get("q") or "").strip()
    conn = db()
    try:
        with conn.cursor() as cur:
            if q:
                like = f"%{q}%"
                cur.execute(
                    """SELECT c.*, u.username, u.display_name, u.email
                       FROM codes c
                       LEFT JOIN users u ON u.id=c.user_id
                       WHERE c.title ILIKE %s OR c.code ILIKE %s
                          OR COALESCE(c.brand,c.site,'') ILIKE %s
                       ORDER BY c.created_at DESC LIMIT 200""",
                    (like, like, like),
                )
            else:
                cur.execute(
                    """SELECT c.*, u.username, u.display_name, u.email
                       FROM codes c
                       LEFT JOIN users u ON u.id=c.user_id
                       ORDER BY c.created_at DESC LIMIT 200"""
                )
            rows = cur.fetchall()
        return jsonify(
            {
                "ok": True,
                "codes": [
                    {
                        **serialize_code(r, admin["id"]),
                        "status": r.get("status"),
                        "author_email": r.get("email"),
                        "author_username": r.get("username"),
                    }
                    for r in rows
                ],
            }
        )
    finally:
        conn.close()


@app.post("/api/admin/codes/<int:code_id>/delete")
@require_admin
def admin_delete_code(admin, code_id):
    return _delete_code_impl(admin, code_id)


@app.get("/api/admin/reports")
@require_admin
def admin_reports(admin):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT r.id, r.reason, r.code_id, r.user_id,
                          c.title AS code_title, c.code AS code_value, c.brand,
                          u.username AS reporter, u.email AS reporter_email,
                          a.username AS author, a.email AS author_email, a.id AS author_id
                   FROM reports r
                   LEFT JOIN codes c ON c.id=r.code_id
                   LEFT JOIN users u ON u.id=r.user_id
                   LEFT JOIN users a ON a.id=c.user_id
                   ORDER BY r.id DESC LIMIT 200"""
            )
            rows = cur.fetchall()
        return jsonify(
            {
                "ok": True,
                "reports": [
                    {
                        "id": r["id"],
                        "reason": r.get("reason") or "Signalement",
                        "code_id": r.get("code_id"),
                        "code_title": r.get("code_title"),
                        "code": r.get("code_value"),
                        "brand": r.get("brand"),
                        "reporter": r.get("reporter"),
                        "reporter_email": r.get("reporter_email"),
                        "author": r.get("author"),
                        "author_email": r.get("author_email"),
                        "author_id": r.get("author_id"),
                    }
                    for r in rows
                ],
            }
        )
    finally:
        conn.close()


@app.get("/api/admin/tickets")
@require_admin
def admin_tickets(admin):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT t.*, u.email, u.username, u.display_name
                   FROM support_tickets t
                   LEFT JOIN users u ON u.id=t.user_id
                   ORDER BY t.created_at DESC LIMIT 200"""
            )
            rows = cur.fetchall()
        return jsonify(
            {
                "ok": True,
                "tickets": [
                    {
                        "id": r["id"],
                        "subject": r.get("subject"),
                        "message": r.get("message"),
                        "reply": r.get("reply") or "",
                        "status": r.get("status") or "OPEN",
                        "created_at": iso(r.get("created_at")),
                        "replied_at": iso(r.get("replied_at")),
                        "user": r.get("display_name") or r.get("username") or "Membre",
                        "email": r.get("email"),
                        "user_id": r.get("user_id"),
                    }
                    for r in rows
                ],
            }
        )
    finally:
        conn.close()


@app.post("/api/admin/tickets/<int:ticket_id>/reply")
@require_admin
def admin_reply_ticket(admin, ticket_id):
    data = request.get_json(silent=True) or {}
    reply = (data.get("message") or data.get("reply") or "").strip()
    if len(reply) < 2:
        return json_error("Réponse trop courte.")
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM support_tickets WHERE id=%s", (ticket_id,))
            ticket = cur.fetchone()
            if not ticket:
                return json_error("Ticket introuvable.", 404)
            cur.execute(
                """UPDATE support_tickets
                   SET reply=%s, replied_at=NOW(), replied_by=%s, status='REPLIED'
                   WHERE id=%s""",
                (reply, admin["id"], ticket_id),
            )
            if ticket.get("user_id"):
                create_notification(
                    cur,
                    ticket["user_id"],
                    "Réponse du support",
                    reply[:280],
                    "SUPPORT",
                )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.post("/api/admin/tickets/<int:ticket_id>/close")
@require_admin
def admin_close_ticket(admin, ticket_id):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE support_tickets SET status='CLOSED' WHERE id=%s",
                (ticket_id,),
            )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.get("/api/admin/broadcasts")
@require_admin
def admin_list_broadcasts(admin):
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, message, created_at
                   FROM broadcasts
                   ORDER BY created_at DESC LIMIT 30"""
            )
            rows = cur.fetchall()
        return jsonify(
            {
                "ok": True,
                "broadcasts": [
                    {
                        "id": r["id"],
                        "title": r.get("title"),
                        "message": r.get("message"),
                        "created_at": iso(r.get("created_at")),
                        "active": bool(
                            r.get("created_at")
                            and (now_utc() - r["created_at"].astimezone(timezone.utc)).total_seconds()
                            < 86400
                        ),
                    }
                    for r in rows
                ],
            }
        )
    finally:
        conn.close()


@app.post("/api/admin/broadcasts")
@require_admin
def admin_create_broadcast(admin):
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "Message COD.IA").strip()[:80]
    message = (data.get("message") or "").strip()
    if len(message) < 3:
        return json_error("Message trop court.")
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO broadcasts(title,message,created_by) VALUES(%s,%s,%s)",
                (title or "Message COD.IA", message, admin["id"]),
            )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


try:
    init_db()
except Exception as e:
    logging.error("DB INIT: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)
