import os
import re
import json
import logging
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
import stripe
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI

# ================== CONFIG ==================
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8091031583"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/+ODE8T52A5yEzMTZk")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004414166682")
DATABASE_URL = os.getenv("DATABASE_URL")
XAI_API_KEY = os.getenv("XAI_API_KEY")
SERVER_URL = os.getenv("SERVER_URL", "https://codeshare-bot-production.up.railway.app")
MINIAPP_URL = os.getenv("MINIAPP_URL", f"{SERVER_URL}/miniapp")
PRICE_CENTS = int(os.getenv("PRICE_CENTS", "1000"))  # 10 €

client = None
if XAI_API_KEY:
    client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")


# ================== DB ==================
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    if not DATABASE_URL:
        logging.warning("DATABASE_URL manquant")
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS paid_users (
            telegram_id BIGINT PRIMARY KEY,
            paid_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS codes (
            id SERIAL PRIMARY KEY,
            type VARCHAR(20) DEFAULT 'promo',
            site VARCHAR(120),
            code VARCHAR(120),
            description TEXT,
            added_by VARCHAR(120),
            user_id BIGINT,
            photo_url TEXT,
            likes INT DEFAULT 0,
            dislikes INT DEFAULT 0,
            copies INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()


try:
    init_db()
except Exception as e:
    logging.error(f"init_db: {e}")


def is_paid(telegram_id: int) -> bool:
    if not telegram_id:
        return False
    if int(telegram_id) == ADMIN_ID:
        return True
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM paid_users WHERE telegram_id = %s", (int(telegram_id),))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return bool(row)
    except Exception as e:
        logging.error(f"is_paid: {e}")
        return False


def mark_paid(telegram_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO paid_users (telegram_id)
        VALUES (%s)
        ON CONFLICT (telegram_id) DO NOTHING
        """,
        (int(telegram_id),),
    )
    conn.commit()
    cur.close()
    conn.close()


# ================== TELEGRAM HELPERS ==================
def send_telegram_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=12)
        logging.info(f"Envoi à {chat_id} → {r.status_code}")
    except Exception as e:
        logging.error(f"send_telegram_message: {e}")


def discover_keyboard(paid: bool):
    btn_text = "Ouvrir Codia" if paid else "Découvrir"
    return {
        "inline_keyboard": [[{"text": btn_text, "web_app": {"url": MINIAPP_URL}}]]
    }


def grant_access_message(chat_id):
    send_telegram_message(
        chat_id,
        "✅ <b>Accès Codia activé</b>\n\n"
        "Tu peux ouvrir l’app maintenant.\n"
        f"Canal : {CHANNEL_LINK}",
        reply_markup=discover_keyboard(True),
    )


# ================== ROUTES BASE ==================
@app.route("/")
def home():
    return "Codia Server is running ✅"


@app.route("/miniapp")
def miniapp():
    return send_from_directory(".", "miniapp.html")


@app.route("/config")
def config():
    return jsonify({"stripe_pk": STRIPE_PUBLISHABLE_KEY})


@app.route("/access")
def access():
    user_id = request.args.get("user_id")
    try:
        uid = int(user_id)
    except Exception:
        return jsonify({"paid": False})
    return jsonify({"paid": is_paid(uid)})


# ================== STRIPE ==================
@app.route("/create-checkout", methods=["POST"])
def create_checkout():
    data = request.json or {}
    telegram_id = data.get("telegram_id")
    if not telegram_id:
        return jsonify({"error": "telegram_id manquant"}), 400
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": "Codia — accès à vie"},
                    "unit_amount": PRICE_CENTS,
                },
                "quantity": 1,
            }],
            success_url=f"{MINIAPP_URL}?paid=1",
            cancel_url=MINIAPP_URL,
            metadata={"telegram_id": str(telegram_id)},
        )
        return jsonify({"url": session.url})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


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
                    "product_data": {"name": "Codia — accès à vie"},
                    "unit_amount": PRICE_CENTS,
                },
                "quantity": 1,
            }],
            return_url=f"{MINIAPP_URL}?paid=1",
            metadata={"telegram_id": str(telegram_id)},
        )
        return jsonify({"clientSecret": session.client_secret})
    except Exception as e:
        logging.error(e)
        return jsonify({"error": str(e)}), 500


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = stripe.Event.construct_from(json.loads(payload), stripe.api_key)
    except Exception as e:
        logging.error(f"Webhook invalid: {e}")
        return jsonify({"error": "invalid"}), 400

    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        session = event["data"]["object"]
        telegram_id = (session.get("metadata") or {}).get("telegram_id")
        if telegram_id:
            try:
                mark_paid(int(telegram_id))
                grant_access_message(int(telegram_id))
            except Exception as e:
                logging.error(f"grant after payment: {e}")

    return jsonify(success=True)


# ================== CODES API ==================
@app.route("/codes")
def list_codes():
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM codes ORDER BY created_at DESC LIMIT 100")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": rows})
    except Exception as e:
        logging.error(e)
        return jsonify({"codes": []})


@app.route("/codes/search")
def search_codes():
    q = (request.args.get("q") or "").strip()
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM codes
            WHERE site ILIKE %s OR code ILIKE %s OR description ILIKE %s
            ORDER BY created_at DESC LIMIT 50
            """,
            (f"%{q}%", f"%{q}%", f"%{q}%"),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": rows})
    except Exception as e:
        logging.error(e)
        return jsonify({"codes": []})


@app.route("/codes/mine")
def my_codes():
    user_id = request.args.get("user_id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM codes WHERE user_id = %s ORDER BY created_at DESC LIMIT 50",
            (int(user_id),),
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify({"codes": rows})
    except Exception as e:
        logging.error(e)
        return jsonify({"codes": []})


@app.route("/codes/add", methods=["POST"])
def add_code():
    data = request.json or {}
    user_id = data.get("user_id")
    if user_id and not is_paid(int(user_id)):
        return jsonify({"success": False, "error": "not_paid"}), 403

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO codes (type, site, code, description, added_by, user_id, photo_url)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (
                data.get("type") or "promo",
                data.get("site"),
                data.get("code"),
                data.get("description"),
                data.get("added_by"),
                data.get("user_id"),
                data.get("photo_url"),
            ),
        )
        new_id = cur.fetchone()["id"]
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "id": new_id})
    except Exception as e:
        logging.error(e)
        return jsonify({"success": False}), 500


@app.route("/code/copy", methods=["POST"])
def code_copy():
    data = request.json or {}
    code_id = data.get("id")
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE codes SET copies = copies + 1 WHERE id = %s RETURNING copies", (code_id,))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"copies": row["copies"] if row else 0})
    except Exception as e:
        logging.error(e)
        return jsonify({"copies": 0})


@app.route("/code/react", methods=["POST"])
def code_react():
    data = request.json or {}
    code_id = data.get("id")
    reaction = data.get("reaction")  # like | dislike
    action = data.get("action")  # add | remove
    col = "likes" if reaction == "like" else "dislikes"
    delta = 1 if action == "add" else -1
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            f"UPDATE codes SET {col} = GREATEST({col} + %s, 0) WHERE id = %s RETURNING {col} AS value",
            (delta, code_id),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"value": row["value"] if row else 0})
    except Exception as e:
        logging.error(e)
        return jsonify({"value": 0})


# ================== IA ==================
@app.route("/ask", methods=["POST"])
def ask():
    data = request.json or {}
    question = (data.get("question") or "").strip()
    city = data.get("city")
    if not question:
        return jsonify({"answer": "Dis-moi ce que tu cherches (Uber, Booking, Zara...)"})

    if not client:
        return jsonify({"answer": "IA non configurée (XAI_API_KEY manquante)."})

    system = (
        "Tu es l'assistant Codia. Tu aides à trouver des codes promo, parrainage, remises "
        "en France. Réponds toujours de façon utile et positive. "
        "Si tu n'as pas de code exact, propose des alternatives concrètes (sites, types d'offres). "
        "Interdit: répondre vide, inutile, ou purement négatif. "
        "Réponds en français, court et actionnable."
    )
    if city:
        system += f" Localisation utilisateur: {city}."

    try:
        completion = client.chat.completions.create(
            model="grok-2-latest",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            temperature=0.6,
        )
        answer = completion.choices[0].message.content
        return jsonify({"answer": answer})
    except Exception as e:
        logging.error(e)
        return jsonify({"answer": "Je cherche une alternative: regarde Booking, Uber Eats, Zara ou les banques en ligne (Fortuneo, Boursorama)."})


# ================== TELEGRAM WEBHOOK ==================
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    update = request.json or {}
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = (message.get("text") or "").strip()
    user = message.get("from") or {}
    user_id = user.get("id")

    if not chat_id:
        return jsonify(success=True)

    # /start
    if text.startswith("/start"):
        paid = is_paid(user_id)
        if paid:
            send_telegram_message(
                chat_id,
                f"👋 Salut <b>{user.get('first_name') or ''}</b>\n\n"
                "Ton accès Codia est actif.\n"
                "Clique sur <b>Ouvrir</b> pour entrer dans l’app.",
                reply_markup=discover_keyboard(True),
            )
        else:
            send_telegram_message(
                chat_id,
                f"👋 Bienvenue sur <b>Codia</b>\n\n"
                "Codes promo, parrainage, IA Discover et communauté.\n\n"
                "Clique sur <b>Découvrir</b> pour voir l’offre.",
                reply_markup=discover_keyboard(False),
            )
        return jsonify(success=True)

    # Admin bypass
    if text.strip().lower() == "/payadmin":
        if int(user_id) != ADMIN_ID:
            send_telegram_message(chat_id, "Commande réservée à l’admin.")
            return jsonify(success=True)
        mark_paid(user_id)
        grant_access_message(chat_id)
        return jsonify(success=True)

    # Must be paid for the rest
    if not is_paid(user_id):
        send_telegram_message(
            chat_id,
            "🔒 Accès réservé.\nClique sur Découvrir pour débloquer Codia.",
            reply_markup=discover_keyboard(False),
        )
        return jsonify(success=True)

    if text.startswith("/acces"):
        send_telegram_message(chat_id, f"Lien canal :\n{CHANNEL_LINK}")
        return jsonify(success=True)

    # /promo Site 30 CODE [date]
    if text.startswith("/promo"):
        parts = text.split()
        if len(parts) >= 4:
            site = parts[1]
            montant = parts[2]
            code = parts[3]
            expire = parts[4] if len(parts) >= 5 else None
            display = f"@{user.get('username')}" if user.get("username") else user.get("first_name", "Membre")
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO codes (type, site, code, description, added_by, user_id)
                    VALUES ('promo', %s, %s, %s, %s, %s)
                    """,
                    (site, code, f"-{montant}%", display, user_id),
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                logging.error(e)

            msg = (
                f"🏷 <b>CODE PROMO</b>\n\n"
                f"De : {display}\n"
                f"Site : {site}\n"
                f"Remise : <b>{montant}%</b>\n"
                f"Code : <code>{code}</code>\n"
                f"Statut : ✅ Actif"
            )
            if expire:
                msg += f"\nExpire le : {expire}"
            send_telegram_message(CHANNEL_ID, msg)
            send_telegram_message(chat_id, f"✅ Promo publiée : {site} | {code}")
        else:
            send_telegram_message(chat_id, "Format : /promo Site 30 CODE")
        return jsonify(success=True)

    # /parrainage Site 20 CODE [date]
    if text.startswith("/parrainage"):
        parts = text.split()
        if len(parts) >= 4:
            site = parts[1]
            montant = parts[2]
            code = parts[3]
            expire = parts[4] if len(parts) >= 5 else None
            display = f"@{user.get('username')}" if user.get("username") else user.get("first_name", "Membre")
            try:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO codes (type, site, code, description, added_by, user_id)
                    VALUES ('parrainage', %s, %s, %s, %s, %s)
                    """,
                    (site, code, f"+{montant}€", display, user_id),
                )
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                logging.error(e)

            msg = (
                f"🔗 <b>CODE DE PARRAINAGE</b>\n\n"
                f"De : {display}\n"
                f"Site : {site}\n"
                f"Bonus : <b>+{montant}€</b>\n"
                f"Code : <code>{code}</code>\n"
                f"Statut : ✅ Actif"
            )
            if expire:
                msg += f"\nExpire le : {expire}"
            send_telegram_message(CHANNEL_ID, msg)
            send_telegram_message(chat_id, f"✅ Parrainage publié : {site} | {code}")
        else:
            send_telegram_message(chat_id, "Format : /parrainage Site 20 CODE")
        return jsonify(success=True)

    return jsonify(success=True)


# ================== DAILY (optionnel cron) ==================
@app.route("/notify/daily", methods=["POST"])
def notify_daily():
    data = request.json or {}
    if int(data.get("admin_id") or 0) != ADMIN_ID:
        return jsonify({"error": "unauthorized"}), 403

    # Ici tu peux brancher une vraie recherche de remises.
    # Pour l’instant: message admin uniquement si rien de fiable.
    send_telegram_message(ADMIN_ID, "Cron daily reçu ✅ (aucune fausse remise envoyée).")
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
