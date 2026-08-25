import os
import json
import logging
import secrets
from datetime import datetime, timedelta, date

from flask import Flask, request, jsonify, send_from_directory
import stripe
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/+ODE8T52A5yEzMTZk")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004414166682")
DATABASE_URL = os.getenv("DATABASE_URL")
SERVER_URL = os.getenv("SERVER_URL", "https://codeshare-bot-production.up.railway.app")
MINIAPP_URL = os.getenv("MINIAPP_URL", f"{SERVER_URL}/miniapp?v=19")
PRICE_CENTS = int(os.getenv("PRICE_CENTS", "1000"))

BASE_MEMBERS = 2345
REPORT_THRESHOLD = 10

ADMIN_IDS = set()
_raw_admins = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "8091031583"))
for _id in _raw_admins.replace(" ", "").split(","):
    if _id.isdigit():
        ADMIN_IDS.add(int(_id))
ADMIN_ID = next(iter(ADMIN_IDS)) if ADMIN_IDS else 8091031583


def is_admin(user_id):
    if not user_id:
        return False
    try:
        return int(user_id) in ADMIN_IDS
    except Exception:
        return False


XAI_API_KEY = os.getenv("XAI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
client = None
AI_MODEL = None
if XAI_API_KEY:
    client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
    AI_MODEL = os.getenv("AI_MODEL", "grok-3")
    logging.info("IA: xAI")
elif GROQ_API_KEY:
    client = OpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile")
    logging.info("IA: Groq")
elif OPENROUTER_API_KEY:
    client = OpenAI(api_key=OPENROUTER_API_KEY, base_url="https://openrouter.ai/api/v1")
    AI_MODEL = os.getenv("AI_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    logging.info("IA: OpenRouter")
else:
    logging.info("IA: non configurée")


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    if not DATABASE_URL:
        logging.warning("DATABASE_URL manquant")
        return
    conn = get_conn()
    cur = conn.cursor()

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
            user_id BIGINT,
            code_id INT,
            created_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (user_id, code_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS copied_codes (
            user_id BIGINT,
            code_id INT,
            created_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (user_id, code_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS hidden_codes (
            user_id BIGINT,
            code_id INT,
            PRIMARY KEY (user_id, code_id)
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
            id SERIAL PRIMARY KEY,
            query TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reactions (
            user_id BIGINT,
            code_id INT,
            reaction TEXT,
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
        CREATE TABLE IF NOT EXISTS ai_chats (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
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
            user_id BIGINT NOT NULL,
            badge_key TEXT NOT NULL,
            earned_at TIMESTAMP DEFAULT NOW(),
            PRIMARY KEY (user_id, badge_key)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coach_events (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            owner_id BIGINT,
            event_type TEXT,
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

    try:
        cur.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS tested_at TIMESTAMP")
        cur.execute("ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS points INT DEFAULT 0")
        cur.execute("ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS referral_used BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE paid_users ADD COLUMN IF NOT EXISTS first_name TEXT")
        cur.execute("ALTER TABLE paid_users ADD COLUMN IF NOT EXISTS username TEXT")
    except Exception as e:
        logging.warning(f"Colonnes: {e}")

    conn.commit()
    cur.close()
    conn.close()
    logging.info("DB initialisée")


def is_paid(user_id):
    if not user_id:
        return False
    if is_admin(user_id):
        return True
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM paid_users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row)
    except Exception as e:
        logging.error(e)
        return False


def send_telegram_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
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
            "web_app": {"url": f"{MINIAPP_URL}&admin=1"}
        }]]
    }


def log_coach_event(owner_id, event_type, actor_id=None):
    if not owner_id or event_type not in ("like", "copy"):
        return
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO coach_events (user_id, owner_id, event_type) VALUES (%s, %s, %s)",
            (actor_id, owner_id, event_type)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logging.error(f"coach_event: {e}")


@app.route("/")
def home():
    return "COD.IA API OK", 200


@app.route("/miniapp")
def miniapp():
    return send_from_directory(".", "miniapp.html")


@app.route("/config")
def config():
    return jsonify({"stripe_pk": STRIPE_PUBLISHABLE_KEY})


@app.route("/stats")
def stats():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM paid_users")
        paid_count = cur.fetchone()["c"] or 0
        cur.close()
        conn.close()
        total = BASE_MEMBERS + paid_count
        display = f"{total/1000:.3f}".replace(".", ",") + "k" if total >= 1000 else str(total)
        return jsonify({"members": total, "members_display": display, "paid": paid_count})
    except Exception:
        return jsonify({"members": BASE_MEMBERS, "members_display": "2,345k", "paid": 0})


@app.route("/access")
def access():
    user_id = request.args.get("user_id", type=int)
    return jsonify({"paid": is_paid(user_id), "is_admin": is_admin(user_id)})


@app.route("/create-embedded-checkout", methods=["POST"])
def create_embedded_checkout():
    data = request.json or {}
    telegram_id = data.get("telegram_id")
    if not telegram_id:
        return jsonify({"error": "telegram_id manquant"}), 400
    try:
        session = stripe.checkout.Session.create(
            ui_mode="embedded_page",
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "product_data": {
                        "name": "COD.IA – Accès complet",
                        "description": "Accès à tous les codes promo & parrainages"
                    },
                    "unit_amount": PRICE_CENTS,
                },
                "quantity": 1,
            }],
            return_url=f"{SERVER_URL}/miniapp?paid=1&v=19",
            metadata={"telegram_id": str(telegram_id)},
        )
        return jsonify({"clientSecret": session.client_secret})
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
        session = event["data"]["object"]
        telegram_id = session.get("metadata", {}).get("telegram_id")
        if telegram_id:
            try:
                conn = get_conn()
                cur = conn.cursor()
                first_name, username = "Membre", None
                try:
                    tg_info = requests.get(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getChat",
                        params={"chat_id": telegram_id}, timeout=5
                    ).json()
                    if tg_info.get("ok"):
                        first_name = tg_info["result"].get("first_name") or "Membre"
                        username = tg_info["result"].get("username")
                except Exception:
                    pass
                cur.execute(
                    """INSERT INTO paid_users (user_id, stripe_session_id, first_name, username)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET first_name = EXCLUDED.first_name, username = EXCLUDED.username""",
                    (int(telegram_id), session.get("id"), first_name, username)
                )
                cur.execute("SELECT referrer_id FROM referrals WHERE referred_id = %s", (int(telegram_id),))
                ref = cur.fetchone()
                if ref:
                    referrer = ref["referrer_id"]
                    cur.execute("""
                        INSERT INTO user_profiles (user_id, points) VALUES (%s, 1)
                        ON CONFLICT (user_id) DO UPDATE SET points = COALESCE(user_profiles.points, 0) + 1
                    """, (referrer,))
                    send_telegram_message(
                        referrer,
                        f"Félicitations ! {first_name} a utilisé ton code et a rejoint COD.IA.\n+1 point (Offre de Lancement)."
                    )
                conn.commit()
                cur.close()
                conn.close()
                send_telegram_message(
                    int(telegram_id),
                    "Paiement reçu.\n\nBienvenue sur COD.IA.\nTon accès est actif.",
                    reply_markup=discover_keyboard(paid=True)
                )
            except Exception as e:
                logging.error(e)
    return jsonify({"ok": True})


@app.route("/admin/stats")
def admin_stats():
    user_id = request.args.get("user_id", type=int)
    if not is_admin(user_id):
        return jsonify({"error": "unauthorized"}), 403
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM paid_users")
        paid = cur.fetchone()["c"] or 0
        cur.execute("SELECT COUNT(*) as c FROM codes WHERE deleted = FALSE")
        total_codes = cur.fetchone()["c"] or 0
        cur.execute("SELECT COUNT(*) as c FROM referrals")
        total_referrals = cur.fetchone()["c"] or 0
        cur.execute("SELECT COUNT(*) as c FROM support_messages WHERE status = 'open'")
        support_open = cur.fetchone()["c"] or 0
        cur.execute("SELECT first_name, username, paid_at FROM paid_users ORDER BY paid_at DESC LIMIT 30")
        recent = cur.fetchall()
        cur.execute("""
            SELECT r.referrer_id, COUNT(*) as cnt, rc.code
            FROM referrals r
            LEFT JOIN referral_codes rc ON rc.user_id = r.referrer_id
            GROUP BY r.referrer_id, rc.code ORDER BY cnt DESC LIMIT 10
        """)
        top_refs = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({
            "total_members": BASE_MEMBERS + paid,
            "paid_members": paid,
            "total_codes": total_codes,
            "total_referrals": total_referrals,
            "support_open": support_open,
            "recent_joins": [
                {"name": r["first_name"] or "Membre", "username": r["username"],
                 "paid_at": r["paid_at"].isoformat() if r["paid_at"] else None}
                for r in recent
            ],
            "top_referrers": [
                {"user_id": r["referrer_id"], "code": r["code"], "count": r["cnt"]}
                for r in top_refs
            ]
        })
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/admin/recent")
def admin_recent():
    user_id = request.args.get("user_id", type=int)
    if not is_admin(user_id):
        return jsonify({"error": "unauthorized"}), 403
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT first_name, username, paid_at FROM paid_users ORDER BY paid_at DESC LIMIT 50")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"joins": [
            {"name": r["first_name"] or "Membre", "username": r["username"],
             "paid_at": r["paid_at"].isoformat() if r["paid_at"] else None}
            for r in rows
        ]})
    except Exception:
        return jsonify({"joins": []})


# ==================== SUPPORT CLIENT ====================

@app.route("/support/send", methods=["POST"])
def support_send():
    data = request.json or {}
    user_id = data.get("user_id")
    message = (data.get("message") or "").strip()
    user_name = (data.get("user_name") or "Membre").strip()
    if not user_id or not message:
        return jsonify({"error": "Message vide"}), 400
    if len(message) > 1000:
        return jsonify({"error": "Max 1000 caractères"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO support_messages (user_id, user_name, message, status)
            VALUES (%s, %s, %s, 'open') RETURNING id
        """, (int(user_id), user_name, message))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        for admin in ADMIN_IDS:
            send_telegram_message(
                admin,
                f"📩 Nouveau Support\n\nDe : {user_name} ({user_id})\n\n{message[:400]}"
            )
        return jsonify({"success": True, "id": row["id"]})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/support/list")
def support_list():
    user_id = request.args.get("user_id", type=int)
    if not is_admin(user_id):
        return jsonify({"error": "unauthorized"}), 403
    status = request.args.get("status", "open")
    try:
        conn = get_conn()
        cur = conn.cursor()
        if status == "all":
            cur.execute("SELECT * FROM support_messages ORDER BY created_at DESC LIMIT 100")
        elif status == "replied":
            cur.execute("""
                SELECT * FROM support_messages WHERE status = 'replied'
                ORDER BY replied_at DESC NULLS LAST LIMIT 100
            """)
        else:
            cur.execute("""
                SELECT * FROM support_messages WHERE status = 'open'
                ORDER BY created_at ASC LIMIT 100
            """)
        rows = cur.fetchall()
        cur.execute("SELECT COUNT(*) as c FROM support_messages WHERE status = 'open'")
        open_count = cur.fetchone()["c"] or 0
        cur.close()
        conn.close()
        return jsonify({
            "messages": [{
                "id": r["id"],
                "user_id": r["user_id"],
                "user_name": r["user_name"],
                "message": r["message"],
                "admin_reply": r["admin_reply"],
                "status": r["status"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "replied_at": r["replied_at"].isoformat() if r["replied_at"] else None,
            } for r in rows],
            "open_count": open_count
        })
    except Exception as e:
        logging.error(e)
        return jsonify({"messages": [], "open_count": 0})


@app.route("/support/reply", methods=["POST"])
def support_reply():
    data = request.json or {}
    admin_id = data.get("admin_id")
    msg_id = data.get("id")
    reply = (data.get("reply") or "").strip()
    if not is_admin(admin_id):
        return jsonify({"error": "unauthorized"}), 403
    if not msg_id or not reply:
        return jsonify({"error": "Réponse vide"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM support_messages WHERE id = %s", (msg_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return jsonify({"error": "Introuvable"}), 404
        cur.execute("""
            UPDATE support_messages
            SET admin_reply = %s, status = 'replied', replied_at = NOW(), replied_by = %s
            WHERE id = %s
        """, (reply, int(admin_id), msg_id))
        notif_text = f"Réponse Support : {reply}"
        if len(notif_text) > 500:
            notif_text = notif_text[:497] + "..."
        cur.execute(
            "INSERT INTO notifications (user_id, message, is_read) VALUES (%s, %s, FALSE)",
            (row["user_id"], notif_text)
        )
        conn.commit()
        cur.close()
        conn.close()
        send_telegram_message(row["user_id"], f"💬 <b>Réponse du support COD.IA</b>\n\n{reply}")
        return jsonify({"success": True})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


# ==================== CODES ====================

@app.route("/codes")
def get_codes():
    type_filter = request.args.get("type")
    expiring = request.args.get("expiring")
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        query = """
            SELECT * FROM codes WHERE deleted = FALSE
            AND (expires_at IS NULL OR expires_at > NOW() - INTERVAL '4 days')
        """
        params = []
        if type_filter in ("promo", "parrainage"):
            query += " AND type = %s"
            params.append(type_filter)
        if expiring == "1":
            query += " AND expires_at IS NOT NULL AND expires_at > NOW() AND expires_at < NOW() + INTERVAL '7 days'"
        if user_id:
            query += " AND id NOT IN (SELECT code_id FROM hidden_codes WHERE user_id = %s)"
            params.append(user_id)
        query += " ORDER BY created_at DESC LIMIT 100"
        cur.execute(query, params)
        codes = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": codes})
    except Exception as e:
        logging.error(e)
        return jsonify({"codes": []})


@app.route("/codes/top")
def codes_top():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        base = """
            SELECT * FROM codes WHERE deleted = FALSE
            AND (expires_at IS NULL OR expires_at > NOW())
            AND (likes >= 100 OR copies >= 100)
        """
        if user_id:
            cur.execute(base + " AND id NOT IN (SELECT code_id FROM hidden_codes WHERE user_id = %s) ORDER BY (likes + copies) DESC LIMIT 5", (user_id,))
        else:
            cur.execute(base + " ORDER BY (likes + copies) DESC LIMIT 5")
        codes = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": codes})
    except Exception as e:
        logging.error(e)
        return jsonify({"codes": []})


@app.route("/codes/search")
def codes_search():
    q = request.args.get("q", "").strip()
    user_id = request.args.get("user_id", type=int)
    if not q:
        return jsonify({"codes": []})
    try:
        conn = get_conn()
        cur = conn.cursor()
        query = """
            SELECT * FROM codes WHERE deleted = FALSE
            AND (expires_at IS NULL OR expires_at > NOW() - INTERVAL '4 days')
            AND (site ILIKE %s OR code ILIKE %s OR description ILIKE %s)
        """
        params = [f"%{q}%", f"%{q}%", f"%{q}%"]
        if user_id:
            query += " AND id NOT IN (SELECT code_id FROM hidden_codes WHERE user_id = %s)"
            params.append(user_id)
        query += " ORDER BY created_at DESC LIMIT 50"
        cur.execute(query, params)
        codes = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": codes})
    except Exception as e:
        logging.error(e)
        return jsonify({"codes": []})


@app.route("/codes/add", methods=["POST"])
def codes_add():
    data = request.json or {}
    user_id = data.get("user_id")
    if not is_paid(user_id):
        return jsonify({"error": "Accès réservé"}), 403
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO codes (type, site, code, description, url, expires_at, added_by, user_id, photo_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (
            data.get("type", "promo"), data.get("site"), data.get("code"),
            data.get("description"), data.get("url"), data.get("expires_at") or None,
            data.get("added_by"), user_id, data.get("photo_url")
        ))
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/codes/mine")
def codes_mine():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM codes WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
        codes = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": codes})
    except Exception:
        return jsonify({"codes": []})


@app.route("/codes/user")
def codes_user():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM codes WHERE user_id = %s AND deleted = FALSE ORDER BY created_at DESC", (user_id,))
        codes = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": codes})
    except Exception:
        return jsonify({"codes": []})


@app.route("/codes/saved")
def codes_saved():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT c.* FROM codes c JOIN saved_codes s ON s.code_id = c.id
            WHERE s.user_id = %s AND c.deleted = FALSE ORDER BY s.created_at DESC
        """, (user_id,))
        codes = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": codes})
    except Exception:
        return jsonify({"codes": []})


@app.route("/code/copy", methods=["POST"])
def code_copy():
    data = request.json or {}
    code_id = data.get("id")
    user_id = data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        if user_id:
            cur.execute("SELECT 1 FROM copied_codes WHERE user_id = %s AND code_id = %s", (user_id, code_id))
            if cur.fetchone():
                cur.execute("SELECT copies FROM codes WHERE id = %s", (code_id,))
                row = cur.fetchone()
                cur.close()
                conn.close()
                return jsonify({"copies": row["copies"] if row else 0})
            cur.execute("INSERT INTO copied_codes (user_id, code_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (user_id, code_id))
        cur.execute("UPDATE codes SET copies = copies + 1 WHERE id = %s RETURNING copies, user_id", (code_id,))
        row = cur.fetchone()
        conn.commit()
        owner_id = row["user_id"] if row else None
        cur.close()
        conn.close()
        if owner_id:
            log_coach_event(owner_id, "copy", user_id)
        return jsonify({"copies": row["copies"] if row else 0})
    except Exception as e:
        logging.error(e)
        return jsonify({"copies": 0})


@app.route("/code/save", methods=["POST"])
def code_save():
    data = request.json or {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO saved_codes (user_id, code_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (data.get("user_id"), data.get("id")))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/unsave", methods=["POST"])
def code_unsave():
    data = request.json or {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM saved_codes WHERE user_id = %s AND code_id = %s",
                    (data.get("user_id"), data.get("id")))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/react", methods=["POST"])
def code_react():
    data = request.json or {}
    code_id = data.get("id")
    reaction = data.get("reaction")
    action = data.get("action")
    user_id = data.get("user_id")
    if reaction not in ("like", "dislike") or action not in ("add", "remove"):
        return jsonify({"value": 0})
    try:
        conn = get_conn()
        cur = conn.cursor()
        col = "likes" if reaction == "like" else "dislikes"
        opposite = "dislike" if reaction == "like" else "like"
        opposite_col = "dislikes" if reaction == "like" else "likes"
        cur.execute("SELECT user_id FROM codes WHERE id = %s", (code_id,))
        owner_row = cur.fetchone()
        owner_id = owner_row["user_id"] if owner_row else None
        if action == "add" and user_id:
            cur.execute("DELETE FROM reactions WHERE user_id = %s AND code_id = %s AND reaction = %s",
                        (user_id, code_id, opposite))
            if cur.rowcount > 0:
                cur.execute(f"UPDATE codes SET {opposite_col} = GREATEST({opposite_col} - 1, 0) WHERE id = %s", (code_id,))
            cur.execute("INSERT INTO reactions (user_id, code_id, reaction) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (user_id, code_id, reaction))
            if cur.rowcount > 0:
                cur.execute(f"UPDATE codes SET {col} = {col} + 1 WHERE id = %s RETURNING {col} as value", (code_id,))
                if reaction == "like" and owner_id:
                    log_coach_event(owner_id, "like", user_id)
            else:
                cur.execute(f"SELECT {col} as value FROM codes WHERE id = %s", (code_id,))
        else:
            if user_id:
                cur.execute("DELETE FROM reactions WHERE user_id = %s AND code_id = %s AND reaction = %s",
                            (user_id, code_id, reaction))
            cur.execute(f"UPDATE codes SET {col} = GREATEST({col} - 1, 0) WHERE id = %s RETURNING {col} as value", (code_id,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"value": row["value"] if row else 0})
    except Exception as e:
        logging.error(e)
        return jsonify({"value": 0})


@app.route("/code/edit", methods=["POST"])
def code_edit():
    data = request.json or {}
    user_id = data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE codes SET site=%s, code=%s, description=%s, url=%s, expires_at=%s
            WHERE id=%s AND (user_id=%s OR %s=TRUE)
        """, (data.get("site"), data.get("code"), data.get("description"), data.get("url"),
              data.get("expires_at") or None, data.get("id"), user_id, is_admin(user_id)))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/delete", methods=["POST"])
def code_delete():
    data = request.json or {}
    user_id = data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE codes SET deleted=TRUE WHERE id=%s AND (user_id=%s OR %s=TRUE)",
                    (data.get("id"), user_id, is_admin(user_id)))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/restore", methods=["POST"])
def code_restore():
    data = request.json or {}
    user_id = data.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE codes SET deleted=FALSE WHERE id=%s AND (user_id=%s OR %s=TRUE)",
                    (data.get("id"), user_id, is_admin(user_id)))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/hard-delete", methods=["POST"])
def code_hard_delete():
    data = request.json or {}
    if not is_admin(data.get("user_id")):
        return jsonify({"error": "unauthorized"}), 403
    code_id = data.get("id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        for t in ("codes",):
            pass
        cur.execute("DELETE FROM codes WHERE id=%s", (code_id,))
        cur.execute("DELETE FROM reactions WHERE code_id=%s", (code_id,))
        cur.execute("DELETE FROM saved_codes WHERE code_id=%s", (code_id,))
        cur.execute("DELETE FROM copied_codes WHERE code_id=%s", (code_id,))
        cur.execute("DELETE FROM hidden_codes WHERE code_id=%s", (code_id,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/code/report", methods=["POST"])
def code_report():
    data = request.json or {}
    code_id = data.get("id")
    user_id = data.get("user_id")
    hide = data.get("hide", True)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE codes SET reports = reports + 1 WHERE id=%s RETURNING reports", (code_id,))
        row = cur.fetchone()
        reports = row["reports"] if row else 0
        auto_deleted = False
        if reports >= REPORT_THRESHOLD:
            cur.execute("UPDATE codes SET deleted=TRUE WHERE id=%s", (code_id,))
            auto_deleted = True
        if hide and user_id:
            cur.execute("INSERT INTO hidden_codes (user_id, code_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (user_id, code_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "auto_deleted": auto_deleted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== PARRAINAGE ====================

@app.route("/referral/generate", methods=["POST"])
def referral_generate():
    data = request.json or {}
    user_id = data.get("user_id")
    if not user_id or not is_paid(user_id):
        return jsonify({"error": "Accès réservé"}), 403
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT code FROM referral_codes WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if row:
            cur.close()
            conn.close()
            return jsonify({"success": True, "code": row["code"]})
        code = "CODIA" + secrets.token_hex(3).upper()
        cur.execute("INSERT INTO referral_codes (user_id, code) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING code",
                    (user_id, code))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "code": row["code"] if row else code})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/referral/integrate", methods=["POST"])
def referral_integrate():
    data = request.json or {}
    user_id = data.get("user_id")
    code = (data.get("code") or "").strip().upper()
    if not user_id or not code:
        return jsonify({"error": "Données manquantes"}), 400
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT referral_used FROM user_profiles WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        if row and row["referral_used"]:
            cur.close()
            conn.close()
            return jsonify({"error": "Tu as déjà intégré un code"}), 400
        cur.execute("SELECT user_id FROM referral_codes WHERE code=%s", (code,))
        owner = cur.fetchone()
        if not owner:
            cur.close()
            conn.close()
            return jsonify({"error": "Code invalide"}), 404
        if owner["user_id"] == user_id:
            cur.close()
            conn.close()
            return jsonify({"error": "Ton propre code"}), 400
        cur.execute("INSERT INTO referrals (referred_id, referrer_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, owner["user_id"]))
        cur.execute("""
            INSERT INTO user_profiles (user_id, referral_used) VALUES (%s, TRUE)
            ON CONFLICT (user_id) DO UPDATE SET referral_used = TRUE
        """, (user_id,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "message": "Code intégré !"})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/referral/status")
def referral_status():
    user_id = request.args.get("user_id", type=int)
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
        cur.close()
        conn.close()
        return jsonify({
            "my_code": my_code["code"] if my_code else None,
            "has_used": bool(used and used["referral_used"]),
            "referrals_count": count
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
                   COALESCE((SELECT added_by FROM codes WHERE user_id=r.referrer_id ORDER BY created_at DESC LIMIT 1),'Membre') as name,
                   COALESCE((SELECT photo_url FROM codes WHERE user_id=r.referrer_id AND photo_url IS NOT NULL ORDER BY created_at DESC LIMIT 1),NULL) as photo_url
            FROM referrals r GROUP BY r.referrer_id ORDER BY referrals_count DESC LIMIT 20
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"leaderboard": [
            {"rank": i, "user_id": r["user_id"], "name": r["name"] or "Membre",
             "photo_url": r["photo_url"], "referrals_count": r["referrals_count"]}
            for i, r in enumerate(rows, 1)
        ]})
    except Exception as e:
        logging.error(e)
        return jsonify({"leaderboard": []})


@app.route("/profile/full_stats")
def profile_full_stats():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM codes WHERE user_id=%s AND deleted=FALSE", (user_id,))
        total_codes = cur.fetchone()["c"]
        cur.execute("SELECT COALESCE(SUM(likes),0) as s FROM codes WHERE user_id=%s", (user_id,))
        total_likes = cur.fetchone()["s"]
        cur.execute("SELECT COALESCE(SUM(copies),0) as s FROM codes WHERE user_id=%s", (user_id,))
        total_copies = cur.fetchone()["s"]
        cur.execute("SELECT COUNT(*) as c FROM copied_codes WHERE user_id=%s", (user_id,))
        copied_by_me = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM follows WHERE followed_id=%s", (user_id,))
        followers = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM follows WHERE follower_id=%s", (user_id,))
        following = cur.fetchone()["c"]
        cur.execute("SELECT bio, COALESCE(points,0) as points FROM user_profiles WHERE user_id=%s", (user_id,))
        bio_row = cur.fetchone()
        bio = bio_row["bio"] if bio_row else None
        points = bio_row["points"] if bio_row else 0
        score = total_codes * 2 + total_likes + total_copies + points
        badge = "Ambassadeur" if score >= 100 else "Référent" if score >= 50 else "Expert" if score >= 25 else "Contributeur" if score >= 10 else "Actif" if score >= 3 else "Membre"
        cur.close()
        conn.close()
        return jsonify({
            "total_codes": total_codes, "total_likes": total_likes, "total_copies": total_copies,
            "copied_by_me": copied_by_me, "followers": followers, "following": following,
            "bio": bio, "badge": badge, "points": points
        })
    except Exception as e:
        logging.error(e)
        return jsonify({})


@app.route("/profile/bio", methods=["POST"])
def profile_bio():
    data = request.json or {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_profiles (user_id, bio) VALUES (%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET bio=EXCLUDED.bio
        """, (data.get("user_id"), (data.get("bio") or "")[:160]))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "bio": (data.get("bio") or "")[:160]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/settings/push")
def get_push():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT push_enabled FROM user_profiles WHERE user_id=%s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return jsonify({"enabled": row["push_enabled"] if row else True})
    except Exception:
        return jsonify({"enabled": True})


@app.route("/settings/push", methods=["POST"])
def set_push():
    data = request.json or {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_profiles (user_id, push_enabled) VALUES (%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET push_enabled=EXCLUDED.push_enabled
        """, (data.get("user_id"), data.get("enabled", True)))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/follow", methods=["POST"])
def follow():
    data = request.json or {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO follows (follower_id, followed_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (data.get("follower_id"), data.get("followed_id")))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/unfollow", methods=["POST"])
def unfollow():
    data = request.json or {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM follows WHERE follower_id=%s AND followed_id=%s",
                    (data.get("follower_id"), data.get("followed_id")))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/is_following")
def is_following():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM follows WHERE follower_id=%s AND followed_id=%s",
                    (request.args.get("follower", type=int), request.args.get("followed", type=int)))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return jsonify({"following": bool(row)})
    except Exception:
        return jsonify({"following": False})


@app.route("/followers")
def followers():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT follower_id as user_id FROM follows WHERE followed_id=%s", (user_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"users": [{"user_id": r["user_id"], "name": f"Membre {r['user_id']}"} for r in rows]})
    except Exception:
        return jsonify({"users": []})


@app.route("/following")
def following():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT followed_id as user_id FROM follows WHERE follower_id=%s", (user_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"users": [{"user_id": r["user_id"], "name": f"Membre {r['user_id']}"} for r in rows]})
    except Exception:
        return jsonify({"users": []})


@app.route("/notifications")
def notifications():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 30", (user_id,))
        notifs = cur.fetchall()
        cur.execute("SELECT COUNT(*) as c FROM notifications WHERE user_id=%s AND is_read=FALSE", (user_id,))
        unread = cur.fetchone()["c"]
        cur.close()
        conn.close()
        return jsonify({"notifications": notifs, "unread": unread})
    except Exception:
        return jsonify({"notifications": [], "unread": 0})


@app.route("/notifications/read", methods=["POST"])
def notifications_read():
    data = request.json or {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE notifications SET is_read=TRUE WHERE user_id=%s", (data.get("user_id"),))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/leaderboard")
def leaderboard():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, added_by as name, photo_url, COUNT(*) as codes_count, SUM(likes) as total_likes
            FROM codes WHERE deleted=FALSE AND user_id IS NOT NULL AND created_at > NOW() - INTERVAL '7 days'
            GROUP BY user_id, added_by, photo_url ORDER BY codes_count DESC, total_likes DESC LIMIT 10
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        result = []
        for i, r in enumerate(rows, 1):
            score = (r["codes_count"] or 0) * 2 + (r["total_likes"] or 0)
            badge = "Ambassadeur" if score >= 50 else "Référent" if score >= 25 else "Expert" if score >= 10 else "Contributeur"
            result.append({"rank": i, "user_id": r["user_id"], "name": r["name"] or "Membre",
                           "photo_url": r["photo_url"], "codes_count": r["codes_count"], "badge": badge})
        return jsonify({"leaderboard": result})
    except Exception as e:
        logging.error(e)
        return jsonify({"leaderboard": []})


@app.route("/search/log", methods=["POST"])
def search_log():
    data = request.json or {}
    q = data.get("q", "").strip()
    if q:
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("INSERT INTO search_logs (query) VALUES (%s)", (q,))
            conn.commit()
            cur.close()
            conn.close()
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
        cur.close()
        conn.close()
        return jsonify({"queries": [r["query"] for r in rows]})
    except Exception:
        return jsonify({"queries": []})


# ==================== COD.IA (tips / défis / badges) ====================

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
            cur.execute("""
                SELECT COUNT(*) as c FROM coach_events
                WHERE owner_id=%s AND event_type='copy' AND created_at::date=CURRENT_DATE
            """, (user_id,))
            row = cur.fetchone()
            if row and row["c"]:
                cur.close()
                conn.close()
                return int(row["c"])
            cur.execute("""
                SELECT COUNT(*) as c FROM copied_codes cc
                JOIN codes c ON c.id=cc.code_id
                WHERE c.user_id=%s AND cc.created_at::date=CURRENT_DATE
            """, (user_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            return int(row["c"] or 0)
        if metric == "like":
            cur.execute("""
                SELECT COUNT(*) as c FROM coach_events
                WHERE owner_id=%s AND event_type='like' AND created_at::date=CURRENT_DATE
            """, (user_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            return int(row["c"] or 0)
        if metric == "referral":
            cur.execute("""
                SELECT COUNT(*) as c FROM referrals
                WHERE referrer_id=%s AND created_at::date=CURRENT_DATE
            """, (user_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            return int(row["c"] or 0)
        cur.close()
        conn.close()
    except Exception as e:
        logging.error(e)
    return 0


def _count_completed_challenges(user_id):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM daily_challenges WHERE user_id=%s AND completed=TRUE", (user_id,))
        n = cur.fetchone()["c"] or 0
        cur.close()
        conn.close()
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
                cur.execute("INSERT INTO user_badges (user_id, badge_key) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                            (user_id, b["key"]))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logging.error(e)
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
    user_id = request.args.get("user_id", type=int)
    tips = []
    if not user_id:
        return jsonify({"tips": tips})
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM codes WHERE user_id=%s AND deleted=FALSE", (user_id,))
        total_codes = cur.fetchone()["c"] or 0
        cur.execute("SELECT id, site, copies FROM codes WHERE user_id=%s AND deleted=FALSE ORDER BY copies DESC LIMIT 1", (user_id,))
        top_code = cur.fetchone()
        cur.execute("SELECT code FROM referral_codes WHERE user_id=%s", (user_id,))
        ref_code = cur.fetchone()
        cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id=%s", (user_id,))
        refs = cur.fetchone()["c"] or 0
        cur.execute("SELECT bio FROM user_profiles WHERE user_id=%s", (user_id,))
        bio_row = cur.fetchone()
        cur.close()
        conn.close()
        if total_codes == 0:
            tips.append({"id": "first_code", "text": "Publie ton 1er code pour apparaître dans le feed.", "action": "share", "cta": "Publier"})
        if not ref_code:
            tips.append({"id": "gen_ref", "text": "Génère ton code de parrainage (Offre de Lancement).", "action": "leaderboard", "cta": "Générer"})
        elif refs == 0:
            tips.append({"id": "share_ref", "text": "Partage ton code : 1 filleul validé = 1 membre parrainé.", "action": "leaderboard", "cta": "Mon code"})
        elif refs < 500:
            tips.append({"id": "refs_prog", "text": f"Tu as {refs} parrainé(s). Objectif 500 → 500 €.", "action": "leaderboard", "cta": "Classement"})
        elif refs < 1000:
            tips.append({"id": "refs_1k", "text": f"{refs} parrainés. Objectif 1 000 → 1 000 €.", "action": "leaderboard", "cta": "Classement"})
        elif refs < 1500:
            tips.append({"id": "refs_15k", "text": f"{refs} parrainés. Objectif 1 500 → 1 500 €.", "action": "leaderboard", "cta": "Classement"})
        if top_code and (top_code["copies"] or 0) < 250:
            left = 250 - (top_code["copies"] or 0)
            tips.append({"id": "copies", "text": f"Plus que {left} copies sur « {top_code['site']} » pour 100 €.", "action": "profile", "cta": "Mes codes"})
        elif top_code and (top_code["copies"] or 0) >= 250:
            tips.append({"id": "copies_ok", "text": "250 copies atteintes. Contacte le support pour 100 €.", "action": "support", "cta": "Support"})
        if not bio_row or not (bio_row.get("bio") or "").strip():
            tips.append({"id": "bio", "text": "Ajoute une bio sur ton profil.", "action": "profile", "cta": "Profil"})
        return jsonify({"tips": tips[:3], "referrals_count": refs})
    except Exception as e:
        logging.error(e)
        return jsonify({"tips": []})


@app.route("/coach/daily")
def coach_daily():
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    ch, d = _today_challenge_for_user(user_id)
    progress, completed = 0, False
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM daily_challenges WHERE user_id=%s AND challenge_date=%s", (user_id, d))
        row = cur.fetchone()
        if not row:
            cur.execute("""
                INSERT INTO daily_challenges (user_id, challenge_date, challenge_key, target, completed)
                VALUES (%s,%s,%s,%s,FALSE)
            """, (user_id, d, ch["key"], ch["target"]))
            conn.commit()
        else:
            completed = bool(row["completed"])
            for c in CHALLENGE_POOL:
                if c["key"] == row["challenge_key"]:
                    ch = c
                    break
        progress = _metric_today(user_id, ch["metric"])
        if progress >= ch["target"] and not completed:
            cur.execute("""
                UPDATE daily_challenges SET completed=TRUE, completed_at=NOW()
                WHERE user_id=%s AND challenge_date=%s
            """, (user_id, d))
            conn.commit()
            completed = True
            _sync_badges(user_id)
        cur.close()
        conn.close()
    except Exception as e:
        logging.error(e)
    best, total_done = _best_badge(user_id)
    return jsonify({
        "date": str(d),
        "challenge": {
            "key": ch["key"], "label": ch["label"], "target": ch["target"],
            "progress": min(progress, ch["target"]), "completed": completed
        },
        "challenges_completed_total": total_done,
        "badge": best
    })


@app.route("/coach/event", methods=["POST"])
def coach_event():
    data = request.json or {}
    owner_id = data.get("owner_id")
    event_type = data.get("event_type")
    if not owner_id or event_type not in ("like", "copy"):
        return jsonify({"ok": False}), 400
    log_coach_event(owner_id, event_type, data.get("user_id"))
    return jsonify({"ok": True})


@app.route("/coach/badges")
def coach_badges():
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"all": [], "next": None})
    total = _sync_badges(user_id)
    all_badges, unlocked, next_badge = [], [], None
    for b in BADGE_TIERS:
        item = {**b, "unlocked": total >= b["need"], "progress": total, "remaining": max(0, b["need"] - total)}
        all_badges.append(item)
        if total >= b["need"]:
            unlocked.append(item)
        elif next_badge is None:
            next_badge = item
    best, _ = _best_badge(user_id)
    return jsonify({"total_challenges": total, "current": best, "unlocked": unlocked, "all": all_badges, "next": next_badge})


@app.route("/coach/badge")
def coach_badge_one():
    user_id = request.args.get("user_id", type=int)
    best, total = _best_badge(user_id)
    return jsonify({"badge": best, "total_challenges": total})


@app.route("/ask", methods=["POST"])
def ask():
    return jsonify({"answer": "Utilise l’onglet COD.IA pour l’aide, les défis et le support."})


@app.route("/ai/history")
def ai_history():
    return jsonify({"history": []})


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

    if text_lower in ("/startadmin", "/admin") or text_lower.startswith("/startadmin@") or text_lower.startswith("/admin@"):
        if not is_admin(user_id):
            send_telegram_message(chat_id, "Commande réservée aux administrateurs.")
            return jsonify(success=True)
        send_telegram_message(
            chat_id,
            "Serveur Admin COD.IA\n\nTableau de bord + Support client.",
            reply_markup=admin_keyboard()
        )
        return jsonify(success=True)

    if text_lower.startswith("/start"):
        paid = is_paid(user_id)
        first_name = user.get("first_name") or "toi"
        if paid:
            send_telegram_message(chat_id, f"Salut {first_name}.\n\nOuvre COD.IA ci-dessous.", reply_markup=discover_keyboard(True))
        else:
            send_telegram_message(
                chat_id,
                f"Bienvenue {first_name}.\n\nCOD.IA — codes promo & parrainage.\nAccès 10 €.\nOffre de Lancement jusqu’à 1 500 €.",
                reply_markup=discover_keyboard(False)
            )
        return jsonify(success=True)

    if text_lower in ("/free", "/gratuit"):
        send_telegram_message(chat_id, "Version gratuite.", reply_markup={
            "inline_keyboard": [[{"text": "Version Gratuite", "web_app": {"url": f"{SERVER_URL}/miniapp?force_free=1&v=19"}}]]
        })
        return jsonify(success=True)

    if text_lower in ("/stat", "/stats"):
        if not is_admin(user_id):
            send_telegram_message(chat_id, "Admins only.")
            return jsonify(success=True)
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("""
                SELECT r.referrer_id, COUNT(*) as cnt, COALESCE(rc.code,'N/A') as code
                FROM referrals r LEFT JOIN referral_codes rc ON rc.user_id=r.referrer_id
                GROUP BY r.referrer_id, rc.code ORDER BY cnt DESC LIMIT 10
            """)
            refs = cur.fetchall()
            cur.execute("SELECT COUNT(*) as c FROM support_messages WHERE status='open'")
            so = cur.fetchone()["c"]
            cur.close()
            conn.close()
            msg = f"📊 STATS\nSupport ouverts : {so}\n\n🏆 PARRAINS\n"
            msg += "\n".join([f"{i}. {r['referrer_id']} ({r['code']}) → {r['cnt']}" for i, r in enumerate(refs, 1)]) or "Aucun"
            send_telegram_message(chat_id, msg)
        except Exception as e:
            logging.error(e)
            send_telegram_message(chat_id, "Erreur stats.")
        return jsonify(success=True)

    if text_lower.startswith("/parrainage"):
        if not is_paid(user_id):
            send_telegram_message(chat_id, "Réservé aux membres.")
            return jsonify(success=True)
        parts = text.split()
        if len(parts) >= 4:
            site, montant, code = parts[1], parts[2], parts[3]
            display = f"@{user.get('username')}" if user.get("username") else user.get("first_name", "Membre")
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO codes (type, site, code, description, added_by, user_id) VALUES ('parrainage',%s,%s,%s,%s,%s)",
                    (site, code, f"+{montant}€", display, user_id)
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                logging.error(e)
            send_telegram_message(CHANNEL_ID, f"PARRAINAGE\n{display}\n{site} +{montant}€\n{code}")
            send_telegram_message(chat_id, f"Publié : {site} | {code}")
        else:
            send_telegram_message(chat_id, "Format : /parrainage Site 20 CODE")
        return jsonify(success=True)

    return jsonify(success=True)


@app.route("/notify/daily", methods=["POST"])
def notify_daily():
    data = request.json or {}
    if not is_admin(data.get("admin_id")):
        return jsonify({"error": "unauthorized"}), 403
    send_telegram_message(ADMIN_ID, "Cron daily reçu")
    return jsonify({"ok": True})


try:
    init_db()
except Exception as e:
    logging.error(f"Init DB error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
