import os
import json
import logging
import secrets
from datetime import datetime, timedelta

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
XAI_API_KEY = os.getenv("XAI_API_KEY")
SERVER_URL = os.getenv("SERVER_URL", "https://codeshare-bot-production.up.railway.app")
MINIAPP_URL = os.getenv("MINIAPP_URL", f"{SERVER_URL}/miniapp?v=17")
PRICE_CENTS = int(os.getenv("PRICE_CENTS", "1000"))

BASE_MEMBERS = 2345
REPORT_THRESHOLD = 10

# ==================== MULTI-ADMIN ====================
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


client = None
if XAI_API_KEY:
    client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")


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
            stripe_session_id TEXT
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

    try:
        cur.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS verified BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS tested_at TIMESTAMP")
        cur.execute("ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS points INT DEFAULT 0")
        cur.execute("ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS referral_used BOOLEAN DEFAULT FALSE")
    except Exception as e:
        logging.warning(f"Colonnes déjà présentes: {e}")

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
    return {
        "inline_keyboard": [[{
            "text": btn_text,
            "web_app": {"url": MINIAPP_URL}
        }]]
    }


def channel_keyboard():
    return {
        "inline_keyboard": [[{
            "text": "Rejoindre le canal",
            "url": CHANNEL_LINK
        }]]
    }


# ==================== ROUTES STATIQUES ====================

@app.route("/")
def home():
    return "COD.IA API OK", 200


@app.route("/miniapp")
def miniapp():
    return send_from_directory(".", "miniapp.html")


@app.route("/config")
def config():
    return jsonify({
        "stripe_pk": STRIPE_PUBLISHABLE_KEY
    })


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
        if total >= 1000:
            display = f"{total/1000:.3f}".replace(".", ",") + "k"
        else:
            display = str(total)
        return jsonify({"members": total, "members_display": display})
    except Exception:
        return jsonify({"members": BASE_MEMBERS, "members_display": "2,345k"})


@app.route("/access")
def access():
    user_id = request.args.get("user_id", type=int)
    paid = is_paid(user_id)
    return jsonify({
        "paid": paid,
        "is_admin": is_admin(user_id)
    })


# ==================== STRIPE ====================

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
            return_url=f"{SERVER_URL}/miniapp?paid=1&v=17",
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
                cur.execute(
                    "INSERT INTO paid_users (user_id, stripe_session_id) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
                    (int(telegram_id), session.get("id"))
                )

                # Crédite le parrain
                cur.execute("SELECT referrer_id FROM referrals WHERE referred_id = %s", (int(telegram_id),))
                ref = cur.fetchone()
                if ref:
                    referrer = ref["referrer_id"]
                    cur.execute("""
                        INSERT INTO user_profiles (user_id, points) VALUES (%s, 100)
                        ON CONFLICT (user_id) DO UPDATE SET points = COALESCE(user_profiles.points, 0) + 100
                    """, (referrer,))
                    send_telegram_message(
                        referrer,
                        "Félicitations ! Quelqu'un a utilisé ton code de parrainage et a rejoint COD.IA.\nTu as gagné 100 points."
                    )

                conn.commit()
                cur.close()
                conn.close()

                send_telegram_message(
                    int(telegram_id),
                    "Paiement reçu.\n\nBienvenue sur COD.IA.\nTon accès est maintenant actif.\n\nTu peux ouvrir l'application et découvrir tous les codes.",
                    reply_markup=discover_keyboard(paid=True)
                )
            except Exception as e:
                logging.error(e)

    return jsonify({"ok": True})


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
            SELECT * FROM codes
            WHERE deleted = FALSE
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
            SELECT * FROM codes
            WHERE deleted = FALSE
            AND (expires_at IS NULL OR expires_at > NOW())
            AND (likes >= 100 OR copies >= 100)
        """
        if user_id:
            cur.execute(base + """
                AND id NOT IN (SELECT code_id FROM hidden_codes WHERE user_id = %s)
                ORDER BY (likes + copies) DESC, created_at DESC
                LIMIT 5
            """, (user_id,))
        else:
            cur.execute(base + """
                ORDER BY (likes + copies) DESC, created_at DESC
                LIMIT 5
            """)
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
            SELECT * FROM codes
            WHERE deleted = FALSE
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
        expires = data.get("expires_at") or None
        cur.execute("""
            INSERT INTO codes (type, site, code, description, url, expires_at, added_by, user_id, photo_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            data.get("type", "promo"),
            data.get("site"),
            data.get("code"),
            data.get("description"),
            data.get("url"),
            expires,
            data.get("added_by"),
            user_id,
            data.get("photo_url")
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
            SELECT c.* FROM codes c
            JOIN saved_codes s ON s.code_id = c.id
            WHERE s.user_id = %s AND c.deleted = FALSE
            ORDER BY s.created_at DESC
        """, (user_id,))
        codes = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": codes})
    except Exception:
        return jsonify({"codes": []})


@app.route("/codes/copied")
def codes_copied():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT c.* FROM codes c
            JOIN copied_codes cc ON cc.code_id = c.id
            WHERE cc.user_id = %s AND c.deleted = FALSE
            ORDER BY cc.created_at DESC
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

        cur.execute("UPDATE codes SET copies = copies + 1 WHERE id = %s RETURNING copies", (code_id,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
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

        if action == "add" and user_id:
            cur.execute("DELETE FROM reactions WHERE user_id = %s AND code_id = %s AND reaction = %s",
                        (user_id, code_id, opposite))
            if cur.rowcount > 0:
                cur.execute(f"UPDATE codes SET {opposite_col} = GREATEST({opposite_col} - 1, 0) WHERE id = %s", (code_id,))

            cur.execute("INSERT INTO reactions (user_id, code_id, reaction) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (user_id, code_id, reaction))
            if cur.rowcount > 0:
                cur.execute(f"UPDATE codes SET {col} = {col} + 1 WHERE id = %s RETURNING {col} as value", (code_id,))
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
            UPDATE codes SET site = %s, code = %s, description = %s, url = %s, expires_at = %s
            WHERE id = %s AND (user_id = %s OR %s = TRUE)
        """, (
            data.get("site"), data.get("code"), data.get("description"),
            data.get("url"), data.get("expires_at") or None,
            data.get("id"), user_id, is_admin(user_id)
        ))
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
        cur.execute("""
            UPDATE codes SET deleted = TRUE
            WHERE id = %s AND (user_id = %s OR %s = TRUE)
        """, (data.get("id"), user_id, is_admin(user_id)))
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
        cur.execute("""
            UPDATE codes SET deleted = FALSE
            WHERE id = %s AND (user_id = %s OR %s = TRUE)
        """, (data.get("id"), user_id, is_admin(user_id)))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/code/hard-delete", methods=["POST"])
def code_hard_delete():
    data = request.json or {}
    user_id = data.get("user_id")
    code_id = data.get("id")

    if not is_admin(user_id):
        return jsonify({"error": "unauthorized"}), 403

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM codes WHERE id = %s", (code_id,))
        cur.execute("DELETE FROM reactions WHERE code_id = %s", (code_id,))
        cur.execute("DELETE FROM saved_codes WHERE code_id = %s", (code_id,))
        cur.execute("DELETE FROM copied_codes WHERE code_id = %s", (code_id,))
        cur.execute("DELETE FROM hidden_codes WHERE code_id = %s", (code_id,))
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
        cur.execute("UPDATE codes SET reports = reports + 1 WHERE id = %s RETURNING reports", (code_id,))
        row = cur.fetchone()
        reports = row["reports"] if row else 0
        auto_deleted = False
        if reports >= REPORT_THRESHOLD:
            cur.execute("UPDATE codes SET deleted = TRUE WHERE id = %s", (code_id,))
            auto_deleted = True
        if hide and user_id:
            cur.execute("INSERT INTO hidden_codes (user_id, code_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
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
        cur.execute("SELECT code FROM referral_codes WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row:
            cur.close()
            conn.close()
            return jsonify({"success": True, "code": row["code"]})

        code = "CODIA" + secrets.token_hex(3).upper()
        cur.execute(
            "INSERT INTO referral_codes (user_id, code) VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING code",
            (user_id, code)
        )
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

        cur.execute("SELECT referral_used FROM user_profiles WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        if row and row["referral_used"]:
            cur.close()
            conn.close()
            return jsonify({"error": "Tu as déjà intégré un code de parrainage"}), 400

        cur.execute("SELECT user_id FROM referral_codes WHERE code = %s", (code,))
        owner = cur.fetchone()
        if not owner:
            cur.close()
            conn.close()
            return jsonify({"error": "Code invalide"}), 404

        referrer_id = owner["user_id"]
        if referrer_id == user_id:
            cur.close()
            conn.close()
            return jsonify({"error": "Tu ne peux pas utiliser ton propre code"}), 400

        cur.execute("INSERT INTO referrals (referred_id, referrer_id) VALUES (%s, %s) ON CONFLICT (referred_id) DO NOTHING",
                    (user_id, referrer_id))
        cur.execute("""
            INSERT INTO user_profiles (user_id, referral_used) VALUES (%s, TRUE)
            ON CONFLICT (user_id) DO UPDATE SET referral_used = TRUE
        """, (user_id,))

        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "message": "Code intégré avec succès !"})
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
        cur.execute("SELECT code FROM referral_codes WHERE user_id = %s", (user_id,))
        my_code = cur.fetchone()
        cur.execute("SELECT referral_used FROM user_profiles WHERE user_id = %s", (user_id,))
        used = cur.fetchone()
        cur.execute("SELECT COUNT(*) as c FROM referrals WHERE referrer_id = %s", (user_id,))
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
            SELECT 
                r.referrer_id as user_id,
                COUNT(*) as referrals_count,
                COALESCE(
                    (SELECT added_by FROM codes WHERE user_id = r.referrer_id ORDER BY created_at DESC LIMIT 1),
                    'Membre'
                ) as name,
                COALESCE(
                    (SELECT photo_url FROM codes WHERE user_id = r.referrer_id AND photo_url IS NOT NULL ORDER BY created_at DESC LIMIT 1),
                    NULL
                ) as photo_url
            FROM referrals r
            GROUP BY r.referrer_id
            ORDER BY referrals_count DESC
            LIMIT 20
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        result = []
        for i, r in enumerate(rows, 1):
            result.append({
                "rank": i,
                "user_id": r["user_id"],
                "name": r["name"] or "Membre",
                "photo_url": r["photo_url"],
                "referrals_count": r["referrals_count"]
            })
        return jsonify({"leaderboard": result})
    except Exception as e:
        logging.error(e)
        return jsonify({"leaderboard": []})


# ==================== PROFIL ====================

@app.route("/profile/full_stats")
def profile_full_stats():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) as c FROM codes WHERE user_id = %s AND deleted = FALSE", (user_id,))
        total_codes = cur.fetchone()["c"]
        cur.execute("SELECT COALESCE(SUM(likes),0) as s FROM codes WHERE user_id = %s", (user_id,))
        total_likes = cur.fetchone()["s"]
        cur.execute("SELECT COALESCE(SUM(copies),0) as s FROM codes WHERE user_id = %s", (user_id,))
        total_copies = cur.fetchone()["s"]
        cur.execute("SELECT COUNT(*) as c FROM copied_codes WHERE user_id = %s", (user_id,))
        copied_by_me = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM follows WHERE followed_id = %s", (user_id,))
        followers = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) as c FROM follows WHERE follower_id = %s", (user_id,))
        following = cur.fetchone()["c"]
        cur.execute("SELECT bio, COALESCE(points, 0) as points FROM user_profiles WHERE user_id = %s", (user_id,))
        bio_row = cur.fetchone()
        bio = bio_row["bio"] if bio_row else None
        points = bio_row["points"] if bio_row else 0

        score = total_codes * 2 + total_likes + total_copies + points
        if score >= 100:
            badge = "Ambassadeur"
        elif score >= 50:
            badge = "Référent"
        elif score >= 25:
            badge = "Expert"
        elif score >= 10:
            badge = "Contributeur"
        elif score >= 3:
            badge = "Actif"
        else:
            badge = "Membre"

        cur.close()
        conn.close()
        return jsonify({
            "total_codes": total_codes,
            "total_likes": total_likes,
            "total_copies": total_copies,
            "copied_by_me": copied_by_me,
            "followers": followers,
            "following": following,
            "bio": bio,
            "badge": badge,
            "points": points
        })
    except Exception as e:
        logging.error(e)
        return jsonify({})


@app.route("/profile/bio", methods=["POST"])
def profile_bio():
    data = request.json or {}
    user_id = data.get("user_id")
    bio = (data.get("bio") or "")[:160]
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_profiles (user_id, bio) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET bio = EXCLUDED.bio
        """, (user_id, bio))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "bio": bio})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/settings/push")
def get_push():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT push_enabled FROM user_profiles WHERE user_id = %s", (user_id,))
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
            INSERT INTO user_profiles (user_id, push_enabled) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET push_enabled = EXCLUDED.push_enabled
        """, (data.get("user_id"), data.get("enabled", True)))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== FOLLOWS ====================

@app.route("/follow", methods=["POST"])
def follow():
    data = request.json or {}
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO follows (follower_id, followed_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
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
        cur.execute("DELETE FROM follows WHERE follower_id = %s AND followed_id = %s",
                    (data.get("follower_id"), data.get("followed_id")))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/is_following")
def is_following():
    follower = request.args.get("follower", type=int)
    followed = request.args.get("followed", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM follows WHERE follower_id = %s AND followed_id = %s", (follower, followed))
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
        cur.execute("SELECT follower_id as user_id FROM follows WHERE followed_id = %s", (user_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        users = [{"user_id": r["user_id"], "name": f"Membre {r['user_id']}"} for r in rows]
        return jsonify({"users": users})
    except Exception:
        return jsonify({"users": []})


@app.route("/following")
def following():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT followed_id as user_id FROM follows WHERE follower_id = %s", (user_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        users = [{"user_id": r["user_id"], "name": f"Membre {r['user_id']}"} for r in rows]
        return jsonify({"users": users})
    except Exception:
        return jsonify({"users": []})


# ==================== NOTIFICATIONS ====================

@app.route("/notifications")
def notifications():
    user_id = request.args.get("user_id", type=int)
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM notifications
            WHERE user_id = %s
            ORDER BY created_at DESC LIMIT 30
        """, (user_id,))
        notifs = cur.fetchall()
        cur.execute("SELECT COUNT(*) as c FROM notifications WHERE user_id = %s AND is_read = FALSE", (user_id,))
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
        cur.execute("UPDATE notifications SET is_read = TRUE WHERE user_id = %s", (data.get("user_id"),))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==================== LEADERBOARD ====================

@app.route("/leaderboard")
def leaderboard():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, added_by as name, photo_url,
                   COUNT(*) as codes_count,
                   SUM(likes) as total_likes
            FROM codes
            WHERE deleted = FALSE AND user_id IS NOT NULL
              AND created_at > NOW() - INTERVAL '7 days'
            GROUP BY user_id, added_by, photo_url
            ORDER BY codes_count DESC, total_likes DESC
            LIMIT 10
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        result = []
        for i, r in enumerate(rows, 1):
            score = (r["codes_count"] or 0) * 2 + (r["total_likes"] or 0)
            if score >= 50:
                badge = "Ambassadeur"
            elif score >= 25:
                badge = "Référent"
            elif score >= 10:
                badge = "Expert"
            else:
                badge = "Contributeur"
            result.append({
                "rank": i,
                "user_id": r["user_id"],
                "name": r["name"] or "Membre",
                "photo_url": r["photo_url"],
                "codes_count": r["codes_count"],
                "badge": badge
            })
        return jsonify({"leaderboard": result})
    except Exception as e:
        logging.error(e)
        return jsonify({"leaderboard": []})


# ==================== SEARCH LOG ====================

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


# ==================== IA ====================

@app.route("/ask", methods=["POST"])
def ask():
    data = request.json or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"answer": "Pose-moi une question sur un code !"})
    if not client:
        return jsonify({"answer": "L'assistant IA n'est pas configure pour le moment."})

    codes_context = ""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT site, code, description, type FROM codes
            WHERE deleted = FALSE AND (expires_at IS NULL OR expires_at > NOW())
            ORDER BY created_at DESC LIMIT 40
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        codes_context = "\n".join([
            f"- {r['site']} | {r['code']} | {r['description'] or ''} ({r['type']})"
            for r in rows
        ])
    except Exception:
        pass

    system_prompt = f"""Tu es l'assistant de COD.IA.
Tu sers UNIQUEMENT a aider les utilisateurs a trouver des codes promo ou de parrainage partages par la communaute.

Regles strictes :
- Reponds uniquement en francais.
- Si la question n'est pas liee a la recherche d'un code, reponds poliment : "Je peux uniquement t'aider a trouver des codes partages par la communaute COD.IA."
- Propose uniquement les codes qui existent dans la liste ci-dessous.
- Sois concis et utile.

Codes actuellement actifs :
{codes_context}"""

    try:
        resp = client.chat.completions.create(
            model="grok-3",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question}
            ],
            max_tokens=400
        )
        answer = resp.choices[0].message.content
        return jsonify({"answer": answer})
    except Exception as e:
        logging.error(e)
        return jsonify({"answer": "Desole, je n'ai pas pu repondre pour le moment."})


# ==================== TELEGRAM WEBHOOK ====================

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

    if text.startswith("/start"):
        paid = is_paid(user_id)
        first_name = user.get("first_name") or "toi"

        if paid:
            welcome = (
                f"Salut {first_name}.\n\n"
                f"Tu as déjà accès à COD.IA.\n\n"
                f"Clique sur le bouton ci-dessous pour ouvrir l'application et retrouver tous les codes promo & parrainages."
            )
            send_telegram_message(chat_id, welcome, reply_markup=discover_keyboard(paid=True))
        else:
            welcome = (
                f"Bienvenue {first_name}.\n\n"
                f"COD.IA est la communauté des codes promo et parrainages mis à jour en temps réel.\n\n"
                f"• Feed complet\n"
                f"• Favoris & notifications\n"
                f"• Assistant IA pour trouver un code rapidement\n"
                f"• Publie tes propres codes\n\n"
                f"Accès complet : 10 euros (paiement unique).\n\n"
                f"Offre de lancement en cours : jusqu'à 1500 € à gagner.\n\n"
                f"Clique sur « Decouvrir COD.IA » pour voir l'aperçu gratuit."
            )
            send_telegram_message(chat_id, welcome, reply_markup=discover_keyboard(paid=False))
        return jsonify(success=True)

    if text.lower() in ("/free", "/gratuit"):
        free_url = f"{SERVER_URL}/miniapp?force_free=1&v=17"
        keyboard = {
            "inline_keyboard": [[{
                "text": "Voir la version Gratuite",
                "web_app": {"url": free_url}
            }]]
        }
        send_telegram_message(chat_id, "Voici la version gratuite (aperçu verrouillé).", reply_markup=keyboard)
        return jsonify(success=True)

    if text.lower() == "/payadmin":
        if not is_admin(user_id):
            send_telegram_message(chat_id, "Commande réservée aux admins.")
            return jsonify(success=True)
        send_telegram_message(chat_id, "Admin OK. Tu as déjà l'accès.")
        return jsonify(success=True)

    if text.lower() in ("/stat", "/stats"):
        if not is_admin(user_id):
            send_telegram_message(chat_id, "Commande réservée aux admins.")
            return jsonify(success=True)
        try:
            conn = get_conn()
            cur = conn.cursor()

            # Top parrains (offre de lancement)
            cur.execute("""
                SELECT r.referrer_id, COUNT(*) as cnt,
                       COALESCE(rc.code, 'N/A') as code
                FROM referrals r
                LEFT JOIN referral_codes rc ON rc.user_id = r.referrer_id
                GROUP BY r.referrer_id, rc.code
                ORDER BY cnt DESC
                LIMIT 10
            """)
            refs = cur.fetchall()

            # Offre Codes/Parrainage (≥ 250 copies)
            cur.execute("""
                SELECT id, site, code, copies, added_by, user_id
                FROM codes
                WHERE copies >= 250 AND deleted = FALSE
                ORDER BY copies DESC
                LIMIT 5
            """)
            big_codes = cur.fetchall()

            cur.close()
            conn.close()

            msg = "📊 STATS OFFRES\n\n"
            msg += "🏆 OFFRE DE LANCEMENT (Top parrains)\n"
            if refs:
                for i, r in enumerate(refs, 1):
                    msg += f"{i}. User {r['referrer_id']} ({r['code']}) → {r['cnt']} filleuls\n"
            else:
                msg += "Aucun encore\n"

            msg += "\n💰 OFFRE CODES/PARRAINAGE (≥ 250 copies = 100 €)\n"
            if big_codes:
                for c in big_codes:
                    msg += f"• {c['site']} | {c['code']} → {c['copies']} copies ({c['added_by']})\n"
            else:
                msg += "Personne n’a encore atteint 250 copies\n"

            send_telegram_message(chat_id, msg)
        except Exception as e:
            logging.error(e)
            send_telegram_message(chat_id, "Erreur stats.")
        return jsonify(success=True)

    if text.lower().startswith("/parrainage"):
        if not is_paid(user_id):
            send_telegram_message(chat_id, "Cette commande est réservée aux membres COD.IA.")
            return jsonify(success=True)
        parts = text.split()
        if len(parts) >= 4:
            site = parts[1]
            montant = parts[2]
            code = parts[3]
            display = f"@{user.get('username')}" if user.get("username") else user.get("first_name", "Membre")
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO codes (type, site, code, description, added_by, user_id) VALUES ('parrainage', %s, %s, %s, %s, %s)",
                    (site, code, f"+{montant}€", display, user_id)
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                logging.error(e)
            send_telegram_message(CHANNEL_ID, f"CODE DE PARRAINAGE\n\nDe : {display}\nSite : {site}\nBonus : +{montant}€\nCode : {code}")
            send_telegram_message(chat_id, f"Parrainage publié : {site} | {code}")
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


# ==================== INIT ====================

try:
    init_db()
except Exception as e:
    logging.error(f"Init DB error: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
