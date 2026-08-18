from flask import Flask, request, jsonify
import stripe
import requests
import os
import json
from datetime import datetime
from openai import OpenAI
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/+ODE8T52A5yEzMTZk")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8091031583"))
CHANNEL_ID = "-1004414166682"
SERVER_URL = os.getenv("SERVER_URL", "https://codeshare-bot-production.up.railway.app")
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://codeshare-bot-production.up.railway.app/miniapp")
DATABASE_URL = os.getenv("DATABASE_URL")

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

admin_mode = False

def get_conn():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL manquant")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS codes (
            id SERIAL PRIMARY KEY,
            type TEXT,
            site TEXT,
            code TEXT,
            description TEXT,
            link TEXT,
            added_by TEXT,
            user_id BIGINT,
            photo_url TEXT,
            likes INTEGER DEFAULT 0,
            dislikes INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS paid_users (
            telegram_id BIGINT PRIMARY KEY,
            paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def is_paid_user(telegram_id):
    if int(telegram_id) == ADMIN_ID:
        return True
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT 1 FROM paid_users WHERE telegram_id = %s", (int(telegram_id),))
        result = c.fetchone()
        conn.close()
        return result is not None
    except Exception as e:
        print("is_paid_user error:", e)
        return False

def add_paid_user(telegram_id):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO paid_users (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO NOTHING",
            (int(telegram_id),)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("Erreur paid_user:", e)

def save_code(code_type, site, code, description, link, added_by, user_id=None, photo_url=None):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''
            INSERT INTO codes (type, site, code, description, link, added_by, user_id, photo_url, likes, dislikes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 0)
        ''', (code_type, site, code, description, link, added_by, user_id, photo_url))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Erreur save_code:", e)

def ensure_daily_codes():
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM codes WHERE added_by = 'Codia IA' AND created_at::date = CURRENT_DATE")
        count = c.fetchone()[0]
        conn.close()
        if count >= 3:
            return

        prompt = """
Donne entre 3 et 5 codes promo ou parrainage utiles pour la France.
Réponds UNIQUEMENT avec un JSON valide:
[
  {"type":"promo","site":"Uber Eats","code":"XXXX","description":"-10€"},
  {"type":"parrainage","site":"Fortuneo","code":"XXXX","description":"+80€"}
]
"""
        response = client.chat.completions.create(
            model="grok-3",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        text = response.choices[0].message.content.strip()
        start = text.find("[")
        end = text.rfind("]") + 1
        if start == -1 or end <= 0:
            return
        items = json.loads(text[start:end])
        for item in items[:5]:
            save_code(
                item.get("type", "promo"),
                item.get("site", "Offre"),
                str(item.get("code", "CODE")).upper(),
                item.get("description", "Promo"),
                None,
                "Codia IA",
                None,
                None
            )
    except Exception as e:
        print("daily codes error:", e)

def get_best_codes_text():
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''
            SELECT type, site, code, description, likes, dislikes
            FROM codes
            ORDER BY (likes - dislikes) DESC, created_at DESC
            LIMIT 30
        ''')
        rows = c.fetchall()
        conn.close()
        if not rows:
            return "Aucun code validé pour le moment."
        text = "Meilleurs codes de la communauté :\n\n"
        for r in rows:
            score = (r[4] or 0) - (r[5] or 0)
            text += f"- {r[0]} | {r[1]} : {r[2]} | {r[3]} | Score:{score}\n"
        return text
    except:
        return "Aucun code validé pour le moment."

def ask_grok(user_message: str, city=None):
    codes_context = get_best_codes_text()
    location_info = f"Localisation approximative de l'utilisateur : {city}." if city else ""
    system_prompt = f"""
Tu es l'assistant officiel de Codia.
{location_info}

Voici les meilleurs codes de la communauté :
{codes_context}

RÈGLES ABSOLUES :
1. Toujours proposer quelque chose d'utile.
2. Interdiction de répondre négativement ou vide.
3. Priorise les codes de la liste.
4. Réponds en français, clair et positif.
"""
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            temperature=0.4
        )
        return response.choices[0].message.content
    except Exception as e:
        print("Erreur Grok:", e)
        return "Voici une bonne alternative : Booking, Uber Eats ou Getaround."

def send_telegram_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print("Erreur send:", e)

def miniapp_keyboard():
    return {"inline_keyboard": [[{"text": "Ouvrir Codia", "web_app": {"url": MINIAPP_URL}}]]}

def access_message():
    return "✅ <b>Accès activé</b>\n\nBienvenue sur Codia.\nClique ci-dessous pour ouvrir l’application :"

@app.route("/")
def home():
    return "Codia Server 24/7 is running ✅"

@app.route("/miniapp")
def miniapp():
    try:
        with open("miniapp.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Erreur Mini App : {e}", 500

@app.route("/ask", methods=["POST"])
def ask():
    data = request.json or {}
    question = data.get("question", "").strip()
    city = data.get("city")
    if not question:
        return jsonify({"answer": "Dis-moi ce que tu recherches."})
    return jsonify({"answer": ask_grok(question, city)})

@app.route("/codes", methods=["GET"])
def get_codes():
    ensure_daily_codes()
    try:
        conn = get_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''
            SELECT id, type, site, code, description, added_by, user_id, photo_url, likes, dislikes, created_at
            FROM codes
            ORDER BY (likes - dislikes) DESC, created_at DESC
            LIMIT 50
        ''')
        rows = c.fetchall()
        conn.close()
        codes = []
        for r in rows:
            codes.append({
                "id": r["id"],
                "type": r["type"],
                "site": r["site"],
                "code": r["code"],
                "description": r["description"],
                "added_by": r["added_by"] or "Membre Codia",
                "user_id": r["user_id"],
                "photo_url": r["photo_url"],
                "likes": r["likes"] or 0,
                "dislikes": r["dislikes"] or 0,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None
            })
        return jsonify({"codes": codes})
    except Exception as e:
        return jsonify({"codes": [], "error": str(e)})

@app.route("/codes/add", methods=["POST"])
def add_code_from_app():
    data = request.json or {}
    code_type = data.get("type", "promo")
    site = (data.get("site") or "").strip()
    code = (data.get("code") or "").strip().upper()
    description = (data.get("description") or "").strip()
    added_by = data.get("added_by") or "Membre Codia"
    user_id = data.get("user_id")
    photo_url = data.get("photo_url")
    if not site or not code:
        return jsonify({"error": "site et code requis"}), 400
    if not description:
        description = "Promo" if code_type == "promo" else "Parrainage"
    save_code(code_type, site, code, description, None, added_by, user_id, photo_url)
    return jsonify({"success": True})

@app.route("/code/react", methods=["POST"])
def code_react():
    data = request.json or {}
    code_id = data.get("id")
    reaction = data.get("reaction")
    action = data.get("action")
    if not code_id or reaction not in ("like", "dislike") or action not in ("add", "remove"):
        return jsonify({"error": "paramètres invalides"}), 400
    column = "likes" if reaction == "like" else "dislikes"
    delta = 1 if action == "add" else -1
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            f"UPDATE codes SET {column} = GREATEST(COALESCE({column},0) + %s, 0) WHERE id = %s",
            (delta, code_id)
        )
        conn.commit()
        c.execute(f"SELECT COALESCE({column},0) FROM codes WHERE id = %s", (code_id,))
        value = c.fetchone()[0]
        conn.close()
        return jsonify({"success": True, "value": value})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/create-checkout", methods=["POST"])
def create_checkout():
    data = request.json or {}
    telegram_id = data.get("telegram_id")
    if not telegram_id:
        return jsonify({"error": "telegram_id manquant"}), 400
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": "Accès Codia - À vie", "description": "Codes + IA + Communauté"},
                    "unit_amount": 1000,
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url="https://t.me/",
            cancel_url="https://t.me/",
            client_reference_id=str(telegram_id),
            metadata={"telegram_id": str(telegram_id)}
        )
        return jsonify({"url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not sig_header or not endpoint_secret:
        return jsonify({"error": "Unauthorized"}), 400
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception:
        return jsonify({"error": "Invalid"}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        telegram_id = session.get("client_reference_id")
        if not telegram_id and session.get("metadata"):
            telegram_id = session["metadata"].get("telegram_id")
        if telegram_id:
            add_paid_user(telegram_id)
            send_telegram_message(telegram_id, access_message(), reply_markup=miniapp_keyboard())
            if ADMIN_ID:
                send_telegram_message(ADMIN_ID, f"✅ Nouveau paiement\nID : <code>{telegram_id}</code>")
    return jsonify(success=True), 200

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    global admin_mode
    data = request.get_json()

    if "callback_query" in data:
        query = data["callback_query"]
        chat_id = query["from"]["id"]
        data_btn = query.get("data", "")
        if str(chat_id) != str(ADMIN_ID):
            return jsonify(success=True)
        if data_btn == "admin_start":
            admin_mode = True
            send_telegram_message(chat_id, "✅ Mode Admin activé")
        elif data_btn == "admin_stop":
            admin_mode = False
            send_telegram_message(chat_id, "🛑 Mode Admin désactivé")
        return jsonify(success=True)

    if "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()
        user = message.get("from", {})
        first_name = user.get("first_name", "Utilisateur")
        username = user.get("username")
        display_name = f"@{username}" if username else first_name

        if not is_paid_user(chat_id) and not text.startswith("/start"):
            send_telegram_message(chat_id, "🔒 Accès refusé.\nFais /start pour obtenir l'accès.")
            return jsonify(success=True)

        if str(chat_id) == str(ADMIN_ID) and admin_mode and not text.startswith("/"):
            send_telegram_message(CHANNEL_ID, text)
            send_telegram_message(chat_id, "✅ Publié anonymement")
            return jsonify(success=True)

        if text.startswith("/start"):
            if int(chat_id) == ADMIN_ID or is_paid_user(chat_id):
                send_telegram_message(chat_id, access_message(), reply_markup=miniapp_keyboard())
                return jsonify(success=True)
            try:
                response = requests.post(f"{SERVER_URL}/create-checkout", json={"telegram_id": chat_id}, timeout=10)
                result = response.json()
                if "url" in result:
                    keyboard = {"inline_keyboard": [[{"text": "Payer 10 € – Accès à vie", "url": result["url"]}]]}
                    send_telegram_message(chat_id, f"👋 Salut <b>{first_name}</b> !\n\nBienvenue sur <b>Codia</b>.\n\nPrix : <b>10 €</b>", reply_markup=keyboard)
                else:
                    send_telegram_message(chat_id, "Erreur paiement.")
            except Exception as e:
                send_telegram_message(chat_id, "Erreur de connexion.")
                print(e)

        elif text.lower() == "/payadmin":
            if int(chat_id) != ADMIN_ID:
                send_telegram_message(chat_id, "⛔ Commande réservée à l'admin.")
                return jsonify(success=True)
            send_telegram_message(chat_id, access_message(), reply_markup=miniapp_keyboard())

        elif text == "/admin1" and str(chat_id) == str(ADMIN_ID):
            keyboard = {"inline_keyboard": [[{"text": "▶️ Démarrer", "callback_data": "admin_start"}], [{"text": "⏹️ Arrêter", "callback_data": "admin_stop"}]]}
            send_telegram_message(chat_id, "🔐 Mode Administrateur", reply_markup=keyboard)

        elif text.lower().startswith("/promo "):
            parts = text[7:].strip().split()
            if len(parts) >= 3:
                try:
                    site = parts[0]
                    percent = int(parts[1])
                    code = parts[2].upper()
                    description = f"-{percent}%"
                    save_code("promo", site, code, description, None, display_name, chat_id, None)
                    send_telegram_message(CHANNEL_ID, f"🏷️ <b>CODE PROMO</b>\n\nDe : {display_name}\nSite : {site}\nCode : <code>{code}</code>")
                    send_telegram_message(chat_id, "✅ Code promo publié !")
                except:
                    send_telegram_message(chat_id, "Format : /promo Site 30 CODE")
            else:
                send_telegram_message(chat_id, "Format : /promo Site 30 CODE")

        elif text.lower().startswith("/parrainage "):
            parts = text[12:].strip().split()
            if len(parts) >= 3:
                try:
                    site = parts[0]
                    montant = int(parts[1])
                    code = parts[2].upper()
                    description = f"+{montant}€"
                    save_code("parrainage", site, code, description, None, display_name, chat_id, None)
                    send_telegram_message(CHANNEL_ID, f"🔗 <b>CODE PARRAINAGE</b>\n\nDe : {display_name}\nSite : {site}\nCode : <code>{code}</code>")
                    send_telegram_message(chat_id, "✅ Code parrainage publié !")
                except:
                    send_telegram_message(chat_id, "Format : /parrainage Site 20 CODE")
            else:
                send_telegram_message(chat_id, "Format : /parrainage Site 20 CODE")

        elif text.startswith("/acces"):
            send_telegram_message(chat_id, access_message(), reply_markup=miniapp_keyboard())

        elif text and not text.startswith("/"):
            send_telegram_message(chat_id, "🔍 Recherche en cours...")
            send_telegram_message(chat_id, ask_grok(text))

    return jsonify(success=True)

# init DB au démarrage
try:
    init_db()
except Exception as e:
    print("Init DB error:", e)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
