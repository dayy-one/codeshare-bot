import os
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, date
from functools import wraps

from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash
import stripe
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=90)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "1") != "0"

logging.basicConfig(level=logging.INFO)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
DATABASE_URL = os.getenv("DATABASE_URL")
SERVER_URL = os.getenv("SERVER_URL", "https://cod-ia.fr")
MINIAPP_URL = os.getenv("MINIAPP_URL", f"{SERVER_URL}/")
PRICE_CENTS = int(os.getenv("PRICE_CENTS", "999"))

BASE_MEMBERS = 2345
REPORT_THRESHOLD = 10
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,20}$")

ADMIN_IDS = set()
_raw_admins = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "8091031583,6886937051"))
for _id in _raw_admins.replace(" ", "").split(","):
    if _id.isdigit():
        ADMIN_IDS.add(int(_id))
ADMIN_ID = next(iter(ADMIN_IDS)) if ADMIN_IDS else 8091031583

ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
}


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    if not DATABASE_URL:
        logging.warning("DATABASE_URL manquant")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            instagram TEXT,
            snapchat TEXT,
            telegram_id BIGINT UNIQUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS paid_users (
            user_id BIGINT PRIMARY KEY,
            paid_at TIMESTAMP DEFAULT NOW(),
            stripe_session_id TEXT,
            first_name TEXT,
            username TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS codes (
            id SERIAL PRIMARY KEY,
            type TEXT DEFAULT 'promo',
            site TEXT,
            code TEXT,
            description TEXT,
            url TEXT,
            expires_at TIMESTAMP,
            added_by TEXT,
            user_id BIGINT,
            photo_url TEXT,
            likes INT DEFAULT 0,
            dislikes INT DEFAULT 0,
            copies INT DEFAULT 0,
            reports INT DEFAULT 0,
            deleted BOOLEAN DEFAULT FALSE,
            verified BOOLEAN DEFAULT FALSE,
            tested_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            message TEXT,
            is_read BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS follows (
            follower_id BIGINT,
            followed_id BIGINT,
            created_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (follower_id, followed_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS saved_codes (
            user_id BIGINT, code_id INT, created_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (user_id, code_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS copied_codes (
            user_id BIGINT, code_id INT, created_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (user_id, code_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hidden_codes (
            user_id BIGINT, code_id INT, PRIMARY KEY (user_id, code_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id BIGINT PRIMARY KEY,
            bio TEXT,
            push_enabled BOOLEAN DEFAULT TRUE,
            points INT DEFAULT 0,
            referral_used BOOLEAN DEFAULT FALSE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS search_logs (
            id SERIAL PRIMARY KEY, query TEXT, created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reactions (
            user_id BIGINT, code_id INT, reaction TEXT,
            PRIMARY KEY (user_id, code_id, reaction)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referral_codes (
            user_id BIGINT PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            referred_id BIGINT PRIMARY KEY,
            referrer_id BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_challenges (
            user_id BIGINT NOT NULL,
            challenge_date DATE NOT NULL,
            challenge_key TEXT NOT NULL,
            target INT NOT NULL,
            completed BOOLEAN DEFAULT FALSE,
            completed_at TIMESTAMP,
            PRIMARY KEY (user_id, challenge_date)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_badges (
            user_id BIGINT NOT NULL, badge_key TEXT NOT NULL,
            earned_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (user_id, badge_key)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coach_events (
            id SERIAL PRIMARY KEY,
            user_id BIGINT, owner_id BIGINT, event_type TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS support_messages (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            user_name TEXT,
            message TEXT NOT NULL,
            admin_reply TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT NOW(),
            replied_at TIMESTAMP,
            replied_by BIGINT
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    logging.info("DB initialisée")


def get_user_by_id(user_id):
    if not user_id:
        return None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id=%s", (int(user_id),))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row
    except Exception as e:
        logging.error(e)
        return None


def session_user_id():
    uid = session.get("user_id")
    try:
        return int(uid) if uid is not None else None
    except Exception:
        return None


def is_admin_user(u):
    if not u:
        return False
    if u.get("email") and str(u["email"]).lower() in ADMIN_EMAILS:
        return True
    try:
        if int(u["id"]) in ADMIN_IDS:
            return True
    except Exception:
        pass
    tid = u.get("telegram_id")
    if tid and int(tid) in ADMIN_IDS:
        return True
    return False


def is_admin(user_id):
    if not user_id:
        return False
    try:
        if int(user_id) in ADMIN_IDS:
            return True
    except Exception:
        pass
    return is_admin_user(get_user_by_id(user_id))


def is_paid(user_id):
    if not user_id:
        return False
    if is_admin(user_id):
        return True
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM paid_users WHERE user_id=%s", (int(user_id),))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row)
    except Exception as e:
        logging.error(e)
        return False


def user_public(u):
    if not u:
        return None
    uid = int(u["id"])
    return {
        "id": uid,
        "user_id": uid,
        "email": u["email"],
        "username": u["username"],
        "instagram": u.get("instagram"),
        "snapchat": u.get("snapchat"),
        "paid": is_paid(uid),
        "is_admin": is_admin_user(u),
    }


def send_telegram_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Telegram send error: {e}")


def discover_keyboard(paid: bool):
    btn_text = "Ouvrir COD.IA" if paid else "Decouvrir COD.IA"
    return {"inline_keyboard": [[{"text": btn_text, "web_app": {"url": MINIAPP_URL}}]]}


def admin_keyboard():
    return {
        "inline_keyboard": [[{
            "text": "Ouvrir le Serveur Admin COD.IA",
            "web_app": {"url": f"{MINIAPP_URL}?admin=1"},
        }]]
    }


def log_coach_event(owner_id, event_type, actor_id=None):
    if not owner_id or event_type not in ("like", "copy"):
        return
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO coach_events (user_id, owner_id, event_type) VALUES (%s,%s,%s)",
            (actor_id, owner_id, event_type),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logging.error(f"coach_event: {e}")


def mark_paid(uid, stripe_session_id=None, first_name="Membre", username=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO paid_users (user_id, stripe_session_id, first_name, username)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (user_id) DO UPDATE SET
             stripe_session_id=COALESCE(EXCLUDED.stripe_session_id, paid_users.stripe_session_id),
             first_name=COALESCE(EXCLUDED.first_name, paid_users.first_name),
             username=COALESCE(EXCLUDED.username, paid_users.username)""",
        (int(uid), stripe_session_id, first_name, username),
    )
    cur.execute("SELECT referrer_id FROM referrals WHERE referred_id=%s", (int(uid),))
    ref = cur.fetchone()
    if ref:
        referrer = ref["referrer_id"]
        cur.execute(
            """INSERT INTO user_profiles (user_id, points) VALUES (%s,1)
               ON CONFLICT (user_id) DO UPDATE SET points=COALESCE(user_profiles.points,0)+1""",
            (referrer,),
        )
        cur.execute(
            "INSERT INTO notifications (user_id, message, is_read) VALUES (%s,%s,FALSE)",
            (referrer, f"{first_name} a rejoint COD.IA. +1 point (Offre de Lancement)."),
        )
    conn.commit()
    cur.close()
    conn.close()


@app.route("/")
@app.route("/miniapp")
def miniapp():
    return send_from_directory(".", "miniapp.html")


@app.route("/health")
def health():
    return "COD.IA API OK", 200


@app.route("/config")
def config():
    return jsonify({"stripe_pk": STRIPE_PUBLISHABLE_KEY})


@app.route("/me")
def me():
    u = get_user_by_id(session_user_id())
    if not u:
        return jsonify({"user": None, "paid": False, "is_admin": False})
    return jsonify({"user": user_public(u), "paid": is_paid(u["id"]), "is_admin": is_admin_user(u)})


@app.route("/auth/register", methods=["POST"])
def auth_register():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    username = (data.get("username") or "").strip().lstrip("@")
    if not email or "@" not in email:
        return jsonify({"error": "Email invalide"}), 400
    if len(password) < 8:
        return jsonify({"error": "Mot de passe : 8 caractères minimum"}), 400
    if not USERNAME_RE.match(username):
        return jsonify({"error": "Username : 3-20 lettres, chiffres ou _"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"error": "Email déjà utilisé"}), 400
        cur.execute("SELECT 1 FROM users WHERE LOWER(username)=LOWER(%s)", (username,))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"error": "Username déjà pris"}), 400
        cur.execute(
            """INSERT INTO users (email, password_hash, username)
               VALUES (%s,%s,%s) RETURNING *""",
            (email, generate_password_hash(password), username),
        )
        u = cur.fetchone()
        conn.commit()
        cur.close(); conn.close()
        session.permanent = True
        session["user_id"] = u["id"]
        return jsonify({"success": True, "user": user_public(u)})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    ident = (data.get("login") or data.get("email") or data.get("username") or "").strip()
    password = data.get("password") or ""
    if not ident or not password:
        return jsonify({"error": "Identifiant et mot de passe requis"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor()
        if "@" in ident:
            cur.execute("SELECT * FROM users WHERE email=%s", (ident.lower(),))
        else:
            cur.execute(
                "SELECT * FROM users WHERE LOWER(username)=LOWER(%s)",
                (ident.lstrip("@"),),
            )
        u = cur.fetchone()
        cur.close(); conn.close()
        if not u or not check_password_hash(u["password_hash"], password):
            return jsonify({"error": "Identifiant ou mot de passe incorrect"}), 401
        session.permanent = True
        session["user_id"] = u["id"]
        return jsonify({"success": True, "user": user_public(u)})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/stats")
def stats():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM paid_users")
        paid_count = cur.fetchone()["c"] or 0
        cur.close(); conn.close()
        total = BASE_MEMBERS + paid_count
        display = f"{total/1000:.3f}".replace(".", ",") + "k" if total >= 1000 else str(total)
        return jsonify({"members": total, "members_display": display, "paid": paid_count})
    except Exception:
        return jsonify({"members": BASE_MEMBERS, "members_display": "2,345k", "paid": 0})


@app.route("/access")
def access():
    user_id = request.args.get("user_id", type=int) or session_user_id()
    return jsonify({"paid": is_paid(user_id), "is_admin": is_admin(user_id)})


@app.route("/create-embedded-checkout", methods=["POST"])
def create_embedded_checkout():
    data = request.json or {}
    telegram_id = data.get("telegram_id")
    web_user_id = session_user_id()
    if not telegram_id and not web_user_id:
        return jsonify({"error": "Connecte-toi d'abord"}), 401
    meta = {}
    customer_email = None
    if web_user_id:
        meta["user_id"] = str(web_user_id)
        u = get_user_by_id(web_user_id)
        if u:
            customer_email = u.get("email")
    if telegram_id:
        meta["telegram_id"] = str(telegram_id)
    try:
        kwargs = {
            "ui_mode": "embedded",
            "mode": "payment",
            "line_items": [{
                "price_data": {
                    "currency": "eur",
                    "product_data": {
                        "name": "COD.IA – Accès complet",
                        "description": "Accès à tous les codes promo & parrainages",
                    },
                    "unit_amount": PRICE_CENTS,
                },
                "quantity": 1,
            }],
            "return_url": f"{SERVER_URL}/?paid=1",
            "metadata": meta,
        }
        if customer_email:
            kwargs["customer_email"] = customer_email
        sess = stripe.checkout.Session.create(**kwargs)
        return jsonify({"clientSecret": sess.client_secret})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return jsonify({"error": "invalid"}), 400

    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        meta = sess.get("metadata", {}) or {}
        uid = None
        first_name, username = "Membre", None
        if meta.get("user_id"):
            uid = int(meta["user_id"])
            u = get_user_by_id(uid)
            if u:
                first_name = u.get("username") or "Membre"
                username = u.get("username")
        elif meta.get("telegram_id"):
            uid = int(meta["telegram_id"])
            try:
                tg_info = requests.get(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat",
                    params={"chat_id": uid}, timeout=5,
                ).json()
                if tg_info.get("ok"):
                    first_name = tg_info["result"].get("first_name") or "Membre"
                    username = tg_info["result"].get("username")
            except Exception:
                pass
        if uid:
            try:
                mark_paid(uid, sess.get("id"), first_name, username)
                if meta.get("telegram_id"):
                    send_telegram_message(
                        uid,
                        "Paiement reçu.\n\nBienvenue sur COD.IA.\nTon accès est actif.",
                        reply_markup=discover_keyboard(paid=True),
                    )
            except Exception as e:
                logging.error(e)
    return jsonify({"ok": True})


@app.route("/admin/stats")
def admin_stats():
    user_id = session_user_id() or request.args.get("user_id", type=int)
    if not is_admin(user_id):
        return jsonify({"error": "unauthorized"}), 403
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM paid_users")
        paid = cur.fetchone()["c"] or 0
        cur.execute("SELECT COUNT(*) as c FROM codes WHERE deleted=FALSE")
        total_codes = cur.fetchone()["c"] or 0
        cur.execute("SELECT COUNT(*) as c FROM referrals")
        total_referrals = cur.fetchone()["c"] or 0
        cur.execute("SELECT first_name, username, paid_at FROM paid_users ORDER BY paid_at DESC LIMIT 30")
        recent = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({
            "total_members": BASE_MEMBERS + paid,
            "paid_members": paid,
            "total_codes": total_codes,
            "total_referrals": total_referrals,
            "recent_joins": [
                {
                    "name": r["username"] or r["first_name"] or "Membre",
                    "username": r["username"],
                    "paid_at": r["paid_at"].isoformat() if r["paid_at"] else None,
                }
                for r in recent
            ],
        })
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/support/send", methods=["POST"])
def support_send():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    message = (data.get("message") or "").strip()
    user_name = (data.get("user_name") or "Membre").strip()
    if session_user_id():
        u = get_user_by_id(session_user_id())
        if u:
            user_name = u["username"]
            user_id = u["id"]
    if not user_id or not message:
        return jsonify({"error": "Message vide"}), 400
    if len(message) > 1000:
        return jsonify({"error": "Max 1000 caractères"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO support_messages (user_id, user_name, message, status)
               VALUES (%s,%s,%s,'open') RETURNING id""",
            (int(user_id), user_name, message),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close(); conn.close()
        for admin in ADMIN_IDS:
            send_telegram_message(admin, f"📩 Support\n\nDe : {user_name} ({user_id})\n\n{message[:400]}")
        return jsonify({"success": True, "id": row["id"]})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/support/list")
def support_list():
    user_id = session_user_id() or request.args.get("user_id", type=int)
    if not is_admin(user_id):
        return jsonify({"error": "unauthorized"}), 403
    status = request.args.get("status", "open")
    try:
        conn = get_conn()
        cur = conn.cursor()
        if status == "replied":
            cur.execute(
                "SELECT * FROM support_messages WHERE status='replied' ORDER BY replied_at DESC NULLS LAST LIMIT 100"
            )
        else:
            cur.execute(
                "SELECT * FROM support_messages WHERE status='open' ORDER BY created_at ASC LIMIT 100"
            )
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) as c FROM support_messages WHERE status='open'")
        open_count = cur.fetchone()["c"] or 0
        cur.close(); conn.close()
        return jsonify({
            "messages": [{
                "id": r["id"], "user_id": r["user_id"], "user_name": r["user_name"],
                "message": r["message"], "admin_reply": r["admin_reply"], "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "replied_at": r["replied_at"].isoformat() if r["replied_at"] else None,
            } for r in rows],
            "open_count": open_count,
        })
    except Exception as e:
        logging.error(e)
        return jsonify({"messages": [], "open_count": 0})


@app.route("/support/reply", methods=["POST"])
def support_reply():
    data = request.json or {}
    admin_id = session_user_id() or data.get("admin_id")
    msg_id = data.get("id")
    reply = (data.get("reply") or "").strip()
    if not is_admin(admin_id):
        return jsonify({"error": "unauthorized"}), 403
    if not msg_id or not reply:
        return jsonify({"error": "Réponse vide"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM support_messages WHERE id=%s", (msg_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"error": "Introuvable"}), 404
        cur.execute(
            """UPDATE support_messages
               SET admin_reply=%s, status='replied', replied_at=NOW(), replied_by=%s WHERE id=%s""",
            (reply, int(admin_id), msg_id),
        )
        notif_text = f"Réponse Support : {reply}"
        if len(notif_text) > 500:
            notif_text = notif_text[:497] + "..."
        cur.execute(
            "INSERT INTO notifications (user_id, message, is_read) VALUES (%s,%s,FALSE)",
            (row["user_id"], notif_text),
        )
        conn.commit()
        cur.close(); conn.close()
        send_telegram_message(row["user_id"], f"💬 <b>Réponse support COD.IA</b>\n\n{reply}")
        return jsonify({"success": True})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


# ---------- CODES (logique d’origine) ----------

@app.route("/codes")
def get_codes():
    type_filter = request.args.get("type")
    expiring = request.args.get("expiring")
    user_id = session_user_id() or request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        query = """
            SELECT * FROM codes WHERE deleted=FALSE
            AND (expires_at IS NULL OR expires_at > NOW() - INTERVAL '4 days')
        """
        params = []
        if type_filter in ("promo", "parrainage"):
            query += " AND type=%s"
            params.append(type_filter)
        if expiring == "1":
            query += " AND expires_at IS NOT NULL AND expires_at > NOW() AND expires_at < NOW() + INTERVAL '7 days'"
        if user_id:
            query += " AND id NOT IN (SELECT code_id FROM hidden_codes WHERE user_id=%s)"
            params.append(user_id)
        query += " ORDER BY created_at DESC LIMIT 100"
        cur.execute(query, params)
        codes = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"codes": codes})
    except Exception as e:
        logging.error(e)
        return jsonify({"codes": []})


@app.route("/codes/top")
def codes_top():
    user_id = session_user_id() or request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        base = """
            SELECT * FROM codes WHERE deleted=FALSE
            AND (expires_at IS NULL OR expires_at > NOW())
            AND (likes>=100 OR copies>=100)
        """
        if user_id:
            cur.execute(
                base + " AND id NOT IN (SELECT code_id FROM hidden_codes WHERE user_id=%s) ORDER BY (likes+copies) DESC LIMIT 5",
                (user_id,),
            )
        else:
            cur.execute(base + " ORDER BY (likes+copies) DESC LIMIT 5")
        codes = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"codes": codes})
    except Exception as e:
        logging.error(e)
        return jsonify({"codes": []})


@app.route("/codes/search")
def codes_search():
    q = request.args.get("q", "").strip()
    user_id = session_user_id() or request.args.get("user_id", type=int)
    if not q:
        return jsonify({"codes": []})
    try:
        conn = get_conn()
        cur = conn.cursor()
        query = """
            SELECT * FROM codes WHERE deleted=FALSE
            AND (expires_at IS NULL OR expires_at > NOW() - INTERVAL '4 days')
            AND (site ILIKE %s OR code ILIKE %s OR description ILIKE %s)
        """
        params = [f"%{q}%", f"%{q}%", f"%{q}%"]
        if user_id:
            query += " AND id NOT IN (SELECT code_id FROM hidden_codes WHERE user_id=%s)"
            params.append(user_id)
        query += " ORDER BY created_at DESC LIMIT 50"
        cur.execute(query, params)
        codes = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"codes": codes})
    except Exception as e:
        logging.error(e)
        return jsonify({"codes": []})


@app.route("/codes/add", methods=["POST"])
def codes_add():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    if not is_paid(user_id):
        return jsonify({"error": "Accès réservé"}), 403
    u = get_user_by_id(user_id) if session_user_id() else None
    added_by = data.get("added_by") or (("@" + u["username"]) if u else "Membre")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO codes (type, site, code, description, url, expires_at, added_by, user_id, photo_url)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                data.get("type", "promo"), data.get("site"), data.get("code"),
                data.get("description"), data.get("url"), data.get("expires_at") or None,
                added_by, user_id, data.get("photo_url"),
            ),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/codes/mine")
def codes_mine():
    user_id = request.args.get("user_id", type=int) or session_user_id()
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM codes WHERE user_id=%s ORDER BY created_at DESC", (user_id,))
        codes = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"codes": codes})
    except Exception:
        return jsonify({"codes": []})


@app.route("/codes/user")
def codes_user():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM codes WHERE user_id=%s AND deleted=FALSE ORDER BY created_at DESC",
            (user_id,),
        )
        codes = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"codes": codes})
    except Exception:
        return jsonify({"codes": []})


@app.route("/codes/saved")
def codes_saved():
    user_id = session_user_id() or request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT c.* FROM codes c JOIN saved_codes s ON s.code_id=c.id
               WHERE s.user_id=%s AND c.deleted=FALSE ORDER BY s.created_at DESC""",
            (user_id,),
        )
        codes = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"codes": codes})
    except Exception:
        return jsonify({"codes": []})


@app.route("/code/copy", methods=["POST"])
def code_copy():
    data = request.json or {}
    code_id = data.get("id")
    user_id = session_user_id() or data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        if user_id:
            cur.execute(
                "SELECT 1 FROM copied_codes WHERE user_id=%s AND code_id=%s",
                (user_id, code_id),
            )
            if cur.fetchone():
                cur.execute("SELECT copies FROM codes WHERE id=%s", (code_id,))
                row = cur.fetchone()
                cur.close(); conn.close()
                return jsonify({"copies": row["copies"] if row else 0})
            cur.execute(
                "INSERT INTO copied_codes (user_id, code_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (user_id, code_id),
            )
        cur.execute(
            "UPDATE codes SET copies=copies+1 WHERE id=%s RETURNING copies, user_id",
            (code_id,),
        )
        row = cur.fetchone()
        conn.commit()
        owner_id = row["user_id"] if row else None
        cur.close(); conn.close()
        if owner_id:
            log_coach_event(owner_id, "copy", user_id)
        return jsonify({"copies": row["copies"] if row else 0})
    except Exception as e:
        logging.error(e)
        return jsonify({"copies": 0})


@app.route("/code/save", methods=["POST"])
def code_save():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO saved_codes (user_id, code_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (user_id, data.get("id")),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/unsave", methods=["POST"])
def code_unsave():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM saved_codes WHERE user_id=%s AND code_id=%s",
            (user_id, data.get("id")),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/react", methods=["POST"])
def code_react():
    data = request.json or {}
    code_id = data.get("id")
    reaction = data.get("reaction")
    action = data.get("action")
    user_id = session_user_id() or data.get("user_id")
    if reaction not in ("like", "dislike") or action not in ("add", "remove"):
        return jsonify({"value": 0})
    try:
        conn = get_conn()
        cur = conn.cursor()
        col = "likes" if reaction == "like" else "dislikes"
        opposite = "dislike" if reaction == "like" else "like"
        opposite_col = "dislikes" if reaction == "like" else "likes"
        cur.execute("SELECT user_id FROM codes WHERE id=%s", (code_id,))
        owner_row = cur.fetchone()
        owner_id = owner_row["user_id"] if owner_row else None
        if action == "add" and user_id:
            cur.execute(
                "DELETE FROM reactions WHERE user_id=%s AND code_id=%s AND reaction=%s",
                (user_id, code_id, opposite),
            )
            if cur.rowcount > 0:
                cur.execute(
                    f"UPDATE codes SET {opposite_col}=GREATEST({opposite_col}-1,0) WHERE id=%s",
                    (code_id,),
                )
            cur.execute(
                "INSERT INTO reactions (user_id, code_id, reaction) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (user_id, code_id, reaction),
            )
            if cur.rowcount > 0:
                cur.execute(
                    f"UPDATE codes SET {col}={col}+1 WHERE id=%s RETURNING {col} as value",
                    (code_id,),
                )
                if reaction == "like" and owner_id:
                    log_coach_event(owner_id, "like", user_id)
            else:
                cur.execute(f"SELECT {col} as value FROM codes WHERE id=%s", (code_id,))
        else:
            if user_id:
                cur.execute(
                    "DELETE FROM reactions WHERE user_id=%s AND code_id=%s AND reaction=%s",
                    (user_id, code_id, reaction),
                )
            cur.execute(
                f"UPDATE codes SET {col}=GREATEST({col}-1,0) WHERE id=%s RETURNING {col} as value",
                (code_id,),
            )
        row = cur.fetchone()
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"value": row["value"] if row else 0})
    except Exception as e:
        logging.error(e)
        return jsonify({"value": 0})


@app.route("/code/edit", methods=["POST"])
def code_edit():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """UPDATE codes SET site=%s, code=%s, description=%s, url=%s, expires_at=%s
               WHERE id=%s AND (user_id=%s OR %s=TRUE)""",
            (
                data.get("site"), data.get("code"), data.get("description"), data.get("url"),
                data.get("expires_at") or None, data.get("id"), user_id, is_admin(user_id),
            ),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/delete", methods=["POST"])
def code_delete():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE codes SET deleted=TRUE WHERE id=%s AND (user_id=%s OR %s=TRUE)",
            (data.get("id"), user_id, is_admin(user_id)),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/restore", methods=["POST"])
def code_restore():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE codes SET deleted=FALSE WHERE id=%s AND (user_id=%s OR %s=TRUE)",
            (data.get("id"), user_id, is_admin(user_id)),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/hard-delete", methods=["POST"])
def code_hard_delete():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    if not is_admin(user_id):
        return jsonify({"error": "unauthorized"}), 403
    code_id = data.get("id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM codes WHERE id=%s", (code_id,))
        cur.execute("DELETE FROM reactions WHERE code_id=%s", (code_id,))
        cur.execute("DELETE FROM saved_codes WHERE code_id=%s", (code_id,))
        cur.execute("DELETE FROM copied_codes WHERE code_id=%s", (code_id,))
        cur.execute("DELETE FROM hidden_codes WHERE code_id=%s", (code_id,))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/report", methods=["POST"])
def code_report():
    data = request.json or {}
    code_id = data.get("id")
    user_id = session_user_id() or data.get("user_id")
    hide = data.get("hide", True)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE codes SET reports=reports+1 WHERE id=%s RETURNING reports", (code_id,))
        row = cur.fetchone()
        reports = row["reports"] if row else 0
        auto_deleted = False
        if reports >= REPORT_THRESHOLD:
            cur.execute("UPDATE codes SET deleted=TRUE WHERE id=%s", (code_id,))
            auto_deleted = True
        if hide and user_id:
            cur.execute(
                "INSERT INTO hidden_codes (user_id, code_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (user_id, code_id),
            )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True, "auto_deleted": auto_deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/referral/generate", methods=["POST"])
def referral_generate():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    if not user_id or not is_paid(user_id):
        return jsonify({"error": "Accès réservé"}), 403
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT code FROM referral_codes WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if row:
            cur.close(); conn.close()
            return jsonify({"success": True, "code": row["code"]})
        code = "CODIA" + secrets.token_hex(3).upper()
        cur.execute(
            "INSERT INTO referral_codes (user_id, code) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING code",
            (user_id, code),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True, "code": row["code"] if row else code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/referral/integrate", methods=["POST"])
def referral_integrate():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    code = (data.get("code") or "").strip().upper()
    if not user_id or not code:
        return jsonify({"error": "Données manquantes"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT referral_used FROM user_profiles WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if row and row["referral_used"]:
            cur.close(); conn.close()
            return jsonify({"error": "Tu as déjà intégré un code"}), 400
        cur.execute("SELECT user_id FROM referral_codes WHERE code=%s", (code,))
        owner = cur.fetchone()
        if not owner:
            cur.close(); conn.close()
            return jsonify({"error": "Code invalide"}), 404
        if owner["user_id"] == user_id:
            cur.close(); conn.close()
            return jsonify({"error": "Ton propre code"}), 400
        cur.execute(
            "INSERT INTO referrals (referred_id, referrer_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (user_id, owner["user_id"]),
        )
        cur.execute(
            """INSERT INTO user_profiles (user_id, referral_used) VALUES (%s, TRUE)
               ON CONFLICT (user_id) DO UPDATE SET referral_used=TRUE""",
            (user_id,),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/referral/status")
def referral_status():
    user_id = request.args.get("user_id", type=int) or session_user_id()
    if not user_id:
        return jsonify({})
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT code FROM referral_codes WHERE user_id=%s", (user_id,))
        my_code = cur.fetchone()
        cur.execute("SELECT referral_used FROM user_profiles WHERE user_id=%s", (user_id,))
        used = cur.fetchone()
        cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=%s", (user_id,))
        count = cur.fetchone()["c"]
        cur.close(); conn.close()
        return jsonify({
            "my_code": my_code["code"] if my_code else None,
            "has_used": bool(used and used["referral_used"]),
            "referrals_count": count,
        })
    except Exception:
        return jsonify({})


@app.route("/referral/leaderboard")
def referral_leaderboard():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT r.referrer_id as user_id, COUNT(*) as referrals_count,
                   COALESCE(u.username,
                     (SELECT added_by FROM codes WHERE user_id=r.referrer_id ORDER BY created_at DESC LIMIT 1),
                     'Membre') as name
            FROM referrals r
            LEFT JOIN users u ON u.id=r.referrer_id
            GROUP BY r.referrer_id, u.username
            ORDER BY referrals_count DESC LIMIT 20
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"leaderboard": [
            {
                "rank": i,
                "user_id": r["user_id"],
                "name": ("@" + str(r["name"]).lstrip("@")) if r["name"] else "Membre",
                "referrals_count": r["referrals_count"],
            }
            for i, r in enumerate(rows, 1)
        ]})
    except Exception as e:
        logging.error(e)
        return jsonify({"leaderboard": []})


@app.route("/profile/full_stats")
def profile_full_stats():
    user_id = request.args.get("user_id", type=int) or session_user_id()
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM codes WHERE user_id=%s AND deleted=FALSE", (user_id,))
        total_codes = cur.fetchone()["c"]
        cur.execute("SELECT COALESCE(SUM(likes),0) as s FROM codes WHERE user_id=%s", (user_id,))
        total_likes = cur.fetchone()["s"]
        cur.execute("SELECT COALESCE(SUM(copies),0) as s FROM codes WHERE user_id=%s", (user_id,))
        total_copies = cur.fetchone()["s"]
        cur.execute("SELECT COUNT(*) as c FROM follows WHERE followed_id=%s", (user_id,))
        followers = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM follows WHERE follower_id=%s", (user_id,))
        following = cur.fetchone()["c"]
        cur.execute("SELECT bio, COALESCE(points,0) as points FROM user_profiles WHERE user_id=%s", (user_id,))
        bio_row = cur.fetchone()
        cur.execute("SELECT username, instagram, snapchat FROM users WHERE id=%s", (user_id,))
        u = cur.fetchone()
        bio = bio_row["bio"] if bio_row else None
        points = bio_row["points"] if bio_row else 0
        score = total_codes * 2 + total_likes + total_copies + points
        badge = (
            "Ambassadeur" if score >= 100 else
            "Référent" if score >= 50 else
            "Expert" if score >= 25 else
            "Contributeur" if score >= 10 else
            "Actif" if score >= 3 else "Membre"
        )
        cur.close(); conn.close()
        return jsonify({
            "total_codes": total_codes, "total_likes": total_likes, "total_copies": total_copies,
            "followers": followers, "following": following, "bio": bio, "badge": badge, "points": points,
            "username": u["username"] if u else None,
            "instagram": u["instagram"] if u else None,
            "snapchat": u["snapchat"] if u else None,
        })
    except Exception as e:
        logging.error(e)
        return jsonify({})


@app.route("/profile/bio", methods=["POST"])
def profile_bio():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    bio = (data.get("bio") or "")[:160]
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO user_profiles (user_id, bio) VALUES (%s,%s)
               ON CONFLICT (user_id) DO UPDATE SET bio=EXCLUDED.bio""",
            (user_id, bio),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True, "bio": bio})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/profile/social", methods=["POST"])
def profile_social():
    data = request.json or {}
    user_id = session_user_id()
    if not user_id:
        return jsonify({"error": "Connecte-toi"}), 401
    ig = (data.get("instagram") or "").strip().lstrip("@")[:64] or None
    snap = (data.get("snapchat") or "").strip().lstrip("@")[:64] or None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE users SET instagram=%s, snapchat=%s WHERE id=%s", (ig, snap, user_id))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True, "instagram": ig, "snapchat": snap})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/settings/push")
def get_push():
    user_id = request.args.get("user_id", type=int) or session_user_id()
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT push_enabled FROM user_profiles WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        cur.close(); conn.close()
        return jsonify({"enabled": row["push_enabled"] if row else True})
    except Exception:
        return jsonify({"enabled": True})


@app.route("/settings/push", methods=["POST"])
def set_push():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO user_profiles (user_id, push_enabled) VALUES (%s,%s)
               ON CONFLICT (user_id) DO UPDATE SET push_enabled=EXCLUDED.push_enabled""",
            (user_id, data.get("enabled", True)),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/follow", methods=["POST"])
def follow():
    data = request.json or {}
    follower = session_user_id() or data.get("follower_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO follows (follower_id, followed_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (follower, data.get("followed_id")),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/unfollow", methods=["POST"])
def unfollow():
    data = request.json or {}
    follower = session_user_id() or data.get("follower_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM follows WHERE follower_id=%s AND followed_id=%s",
            (follower, data.get("followed_id")),
        )
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/is_following")
def is_following():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM follows WHERE follower_id=%s AND followed_id=%s",
            (
                session_user_id() or request.args.get("follower", type=int),
                request.args.get("followed", type=int),
            ),
        )
        row = cur.fetchone()
        cur.close(); conn.close()
        return jsonify({"following": bool(row)})
    except Exception:
        return jsonify({"following": False})


@app.route("/followers")
def followers():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT f.follower_id as user_id, COALESCE(u.username, 'Membre') as name
               FROM follows f LEFT JOIN users u ON u.id=f.follower_id
               WHERE f.followed_id=%s""",
            (user_id,),
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"users": [
            {"user_id": r["user_id"], "name": "@" + str(r["name"]).lstrip("@")} for r in rows
        ]})
    except Exception:
        return jsonify({"users": []})


@app.route("/following")
def following():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT f.followed_id as user_id, COALESCE(u.username, 'Membre') as name
               FROM follows f LEFT JOIN users u ON u.id=f.followed_id
               WHERE f.follower_id=%s""",
            (user_id,),
        )
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"users": [
            {"user_id": r["user_id"], "name": "@" + str(r["name"]).lstrip("@")} for r in rows
        ]})
    except Exception:
        return jsonify({"users": []})


@app.route("/notifications")
def notifications():
    user_id = session_user_id() or request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 30",
            (user_id,),
        )
        notifs = cur.fetchall()
        cur.execute(
            "SELECT COUNT(*) as c FROM notifications WHERE user_id=%s AND is_read=FALSE",
            (user_id,),
        )
        unread = cur.fetchone()["c"]
        cur.close(); conn.close()
        return jsonify({"notifications": notifs, "unread": unread})
    except Exception:
        return jsonify({"notifications": [], "unread": 0})


@app.route("/notifications/read", methods=["POST"])
def notifications_read():
    data = request.json or {}
    user_id = session_user_id() or data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE notifications SET is_read=TRUE WHERE user_id=%s", (user_id,))
        conn.commit()
        cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/leaderboard")
def leaderboard():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT c.user_id,
                   COALESCE(u.username, c.added_by) as name,
                   COUNT(*) as codes_count, SUM(c.likes) as total_likes
            FROM codes c
            LEFT JOIN users u ON u.id=c.user_id
            WHERE c.deleted=FALSE AND c.user_id IS NOT NULL
              AND c.created_at > NOW() - INTERVAL '7 days'
            GROUP BY c.user_id, u.username, c.added_by
            ORDER BY codes_count DESC, total_likes DESC LIMIT 10
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        result = []
        for i, r in enumerate(rows, 1):
            result.append({
                "rank": i,
                "user_id": r["user_id"],
                "name": "@" + str(r["name"] or "Membre").lstrip("@"),
                "codes_count": r["codes_count"],
            })
        return jsonify({"leaderboard": result})
    except Exception as e:
        logging.error(e)
        return jsonify({"leaderboard": []})


@app.route("/search/log", methods=["POST"])
def search_log():
    q = ((request.json or {}).get("q") or "").strip()
    if q:
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO search_logs (query) VALUES (%s)", (q,))
            conn.commit()
            cur.close(); conn.close()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/search/recent")
def search_recent():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT query, COUNT(*) as c FROM search_logs
            WHERE created_at > NOW() - INTERVAL '7 days'
            GROUP BY query ORDER BY c DESC LIMIT 8
        """)
        rows = cur.fetchall()
        cur.close(); conn.close()
        return jsonify({"queries": [r["query"] for r in rows]})
    except Exception:
        return jsonify({"queries": []})


BADGE_TIERS = [
    {"key": "rookie", "label": "Rookie", "icon": "🌱", "need": 3, "desc": "3 défis réussis"},
    {"key": "actif", "label": "Actif", "icon": "⚡", "need": 7, "desc": "7 défis réussis"},
    {"key": "warrior", "label": "Warrior", "icon": "🔥", "need": 15, "desc": "15 défis réussis"},
    {"key": "legend", "label": "Légende", "icon": "👑", "need": 30, "desc": "30 défis réussis"},
    {"key": "mythic", "label": "Mythique", "icon": "💎", "need": 60, "desc": "60 défis réussis"},
]
CHALLENGE_POOL = [
    {"key": "likes_3", "label": "Obtiens 3 J’aime aujourd’hui", "metric": "like", "target": 3},
    {"key": "likes_5", "label": "Obtiens 5 J’aime aujourd’hui", "metric": "like", "target": 5},
    {"key": "copies_3", "label": "Obtiens 3 copies sur tes codes aujourd’hui", "metric": "copy", "target": 3},
    {"key": "copies_5", "label": "Obtiens 5 copies sur tes codes aujourd’hui", "metric": "copy", "target": 5},
    {"key": "copies_10", "label": "Obtiens 10 copies sur tes codes aujourd’hui", "metric": "copy", "target": 10},
    {"key": "ref_1", "label": "Obtiens 1 nouveau filleul aujourd’hui", "metric": "referral", "target": 1},
]


def _today_challenge_for_user(user_id):
    d = date.today()
    idx = (d.toordinal() + int(user_id or 0)) % len(CHALLENGE_POOL)
    return CHALLENGE_POOL[idx], d


def _metric_today(user_id, metric):
    try:
        conn = get_conn()
        cur = conn.cursor()
        if metric == "copy":
            cur.execute(
                """SELECT COUNT(*) as c FROM coach_events
                   WHERE owner_id=%s AND event_type='copy' AND created_at::date=CURRENT_DATE""",
                (user_id,),
            )
        elif metric == "like":
            cur.execute(
                """SELECT COUNT(*) as c FROM coach_events
                   WHERE owner_id=%s AND event_type='like' AND created_at::date=CURRENT_DATE""",
                (user_id,),
            )
        else:
            cur.execute(
                """SELECT COUNT(*) as c FROM referrals
                   WHERE referrer_id=%s AND created_at::date=CURRENT_DATE""",
                (user_id,),
            )
        row = cur.fetchone()
        cur.close(); conn.close()
        return int(row["c"] or 0)
    except Exception:
        return 0


def _count_completed_challenges(user_id):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) as c FROM daily_challenges WHERE user_id=%s AND completed=TRUE",
            (user_id,),
        )
        n = cur.fetchone()["c"] or 0
        cur.close(); conn.close()
        return int(n)
    except Exception:
        return 0


def _sync_badges(user_id):
    n = _count_completed_challenges(user_id)
    try:
        conn = get_conn()
        cur = conn.cursor()
        for b in BADGE_TIERS:
            if n >= b["need"]:
                cur.execute(
                    "INSERT INTO user_badges (user_id, badge_key) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, b["key"]),
                )
        conn.commit()
        cur.close(); conn.close()
    except Exception:
        pass
    return n


def _best_badge(user_id):
    n = _count_completed_challenges(user_id)
    best = None
    for b in BADGE_TIERS:
        if n >= b["need"]:
            best = b
    return best, n


@app.route("/coach/tips")
def coach_tips():
    user_id = request.args.get("user_id", type=int) or session_user_id()
    tips = []
    if not user_id:
        return jsonify({"tips": tips})
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM codes WHERE user_id=%s AND deleted=FALSE", (user_id,))
        total_codes = cur.fetchone()["c"] or 0
        cur.execute("SELECT code FROM referral_codes WHERE user_id=%s", (user_id,))
        ref_code = cur.fetchone()
        cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=%s", (user_id,))
        refs = cur.fetchone()["c"] or 0
        cur.close(); conn.close()
        if total_codes == 0:
            tips.append({"id": "first_code", "text": "Publie ton 1er code.", "action": "share", "cta": "Publier"})
        if not ref_code:
            tips.append({"id": "gen_ref", "text": "Génère ton code de parrainage.", "action": "leaderboard", "cta": "Générer"})
        elif refs < 500:
            tips.append({"id": "refs", "text": f"{refs} parrainé(s). Objectif 500 → 500 €.", "action": "leaderboard", "cta": "Classement"})
        return jsonify({"tips": tips[:3], "referrals_count": refs})
    except Exception as e:
        logging.error(e)
        return jsonify({"tips": []})


@app.route("/coach/daily")
def coach_daily():
    user_id = request.args.get("user_id", type=int) or session_user_id()
    if not user_id:
        return jsonify({"error": "login"}), 400
    ch, d = _today_challenge_for_user(user_id)
    progress, completed = 0, False
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM daily_challenges WHERE user_id=%s AND challenge_date=%s",
            (user_id, d),
        )
        row = cur.fetchone()
        if not row:
            cur.execute(
                """INSERT INTO daily_challenges (user_id, challenge_date, challenge_key, target, completed)
                   VALUES (%s,%s,%s,%s,FALSE)""",
                (user_id, d, ch["key"], ch["target"]),
            )
            conn.commit()
        else:
            completed = bool(row["completed"])
            for c in CHALLENGE_POOL:
                if c["key"] == row["challenge_key"]:
                    ch = c
                    break
        progress = _metric_today(user_id, ch["metric"])
        if progress >= ch["target"] and not completed:
            cur.execute(
                """UPDATE daily_challenges SET completed=TRUE, completed_at=NOW()
                   WHERE user_id=%s AND challenge_date=%s""",
                (user_id, d),
            )
            conn.commit()
            completed = True
            _sync_badges(user_id)
        cur.close(); conn.close()
    except Exception as e:
        logging.error(e)
    best, total_done = _best_badge(user_id)
    return jsonify({
        "date": str(d),
        "challenge": {
            "key": ch["key"], "label": ch["label"], "target": ch["target"],
            "progress": min(progress, ch["target"]), "completed": completed,
        },
        "challenges_completed_total": total_done,
        "badge": best,
    })


@app.route("/coach/badges")
def coach_badges():
    user_id = request.args.get("user_id", type=int) or session_user_id()
    if not user_id:
        return jsonify({"all": []})
    total = _sync_badges(user_id)
    all_badges = []
    for b in BADGE_TIERS:
        all_badges.append({**b, "unlocked": total >= b["need"], "remaining": max(0, b["need"] - total)})
    best, _ = _best_badge(user_id)
    return jsonify({"total_challenges": total, "current": best, "all": all_badges})


@app.route("/coach/badge")
def coach_badge_one():
    user_id = request.args.get("user_id", type=int) or session_user_id()
    best, total = _best_badge(user_id)
    return jsonify({"badge": best, "total_challenges": total})


@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    data = request.json or {}
    message = data.get("message") or data.get("callback_query", {}).get("message")
    if not message:
        return jsonify(success=True)
    chat_id = message["chat"]["id"]
    user = message.get("from") or {}
    user_id = user.get("id")
    text = (message.get("text") or "").strip()
    text_lower = text.lower()

    if text_lower in ("/startadmin", "/admin") or text_lower.startswith("/startadmin@"):
        if not is_admin(user_id):
            send_telegram_message(chat_id, "Commande réservée aux administrateurs.")
            return jsonify(success=True)
        send_telegram_message(
            chat_id,
            "Serveur Admin COD.IA",
            reply_markup=admin_keyboard(),
        )
        return jsonify(success=True)

    if text_lower.startswith("/start"):
        paid = is_paid(user_id)
        first_name = user.get("first_name") or "toi"
        if paid:
            send_telegram_message(
                chat_id,
                f"Salut {first_name}.\n\nOuvre COD.IA ci-dessous.",
                reply_markup=discover_keyboard(True),
            )
        else:
            send_telegram_message(
                chat_id,
                f"Bienvenue {first_name}.\n\nCOD.IA — codes promo & parrainage.\nAccès 9,99 €.",
                reply_markup=discover_keyboard(False),
            )
        return jsonify(success=True)

    return jsonify(success=True)


try:
    init_db()
except Exception as e:
    logging.error(f"Init DB error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
