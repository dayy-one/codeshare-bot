from flask import Flask, request, jsonify
import stripe
import requests
import os
import sqlite3
from openai import OpenAI
from datetime import datetime

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/+ODE8T52A5yEzMTZk")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8091031583"))
CHANNEL_ID = "-1004414166682"
SERVER_URL = "https://codeshare-bot-production.up.railway.app"
MINIAPP_URL = "https://codeshare-bot-production.up.railway.app/miniapp"

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

admin_mode = False

def init_db():
    conn = sqlite3.connect("codes.db")
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            site TEXT,
            code TEXT,
            description TEXT,
            link TEXT,
            added_by TEXT,
            working INTEGER DEFAULT 0,
            dead INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS paid_users (
            telegram_id INTEGER PRIMARY KEY,
            paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def is_paid_user(telegram_id):
    if int(telegram_id) == ADMIN_ID:
        return True
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute("SELECT 1 FROM paid_users WHERE telegram_id = ?", (int(telegram_id),))
        result = c.fetchone()
        conn.close()
        return result is not None
    except:
        return False

def add_paid_user(telegram_id):
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO paid_users (telegram_id) VALUES (?)", (int(telegram_id),))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Erreur paid_user:", e)

def save_code(code_type, site, code, description, link, added_by):
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute('''
            INSERT INTO codes (type, site, code, description, link, added_by, working, dead, likes)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)
        ''', (code_type, site, code, description, link, added_by))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Erreur save_code:", e)

def get_best_codes_text():
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute('''
            SELECT type, site, code, description, working, dead, likes
            FROM codes
            ORDER BY (working - dead + likes) DESC, created_at DESC
            LIMIT 30
        ''')
        rows = c.fetchall()
        conn.close()
        if not rows:
            return "Aucun code validé pour le moment."
        text = "Meilleurs codes de la communauté :\n\n"
        for r in rows:
            score = r[4] - r[5] + r[6]
            text += f"- {r[0]} | {r[1]} : {r[2]} | {r[3]} | Score:{score}\n"
        return text
    except:
        return "Aucun code validé pour le moment."

def ask_grok(user_message: str, city=None):
    codes_context = get_best_codes_text()
    location_info = f"Localisation approximative de l'utilisateur : {city}." if city else ""

    system_prompt = f"""
Tu es l'assistant officiel de CODE IA.
{location_info}

Voici les meilleurs codes de la communauté :
{codes_context}

RÈGLES ABSOLUES :
1. Tu dois TOUJOURS proposer quelque chose d'utile.
2. Interdiction totale de dire : aucun code, je n'ai rien trouvé, pas disponible, je ne sais pas.
3. Interdiction de réponse vide ou négative.
4. Priorise les codes de la liste s'ils correspondent.
5. Favorise voyages, hôtels, nourriture, location de véhicule si pertinent.
6. Si pas de code exact, propose une alternative concrète.
7. Réponds en français, clair et positif.
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
        return "Voici une bonne alternative : regarde Booking, Uber Eats ou Getaround selon ton besoin. Je continue de chercher les meilleurs codes pour toi."

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
    return {
        "inline_keyboard": [[
            {"text": "🚀 Ouvrir CODE IA", "web_app": {"url": MINIAPP_URL}}
        ]]
    }

# ================== ROUTES ==================
@app.route("/")
def home():
    return "CODE IA Server 24/7 is running ✅"

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
        return jsonify({"answer": "Dis-moi ce que tu recherches (ex: code Uber Eats, hôtel Paris...)."})
    return jsonify({"answer": ask_grok(question, city)})

@app.route("/codes", methods=["GET"])
def get_codes():
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute('''
            SELECT id, type, site, code, description, added_by, working, dead, likes, created_at
            FROM codes
            ORDER BY (working - dead + likes) DESC, created_at DESC
            LIMIT 50
        ''')
        rows = c.fetchall()
        conn.close()
        codes = []
        for r in rows:
            codes.append({
                "id": r[0],
                "type": r[1],
                "site": r[2],
                "code": r[3],
                "description": r[4],
                "added_by": r[5] or "Anonyme",
                "working": r[6],
                "dead": r[7],
                "likes": r[8],
                "views": 2000 + (r[0] * 137) % 4000
            })
        return jsonify({"codes": codes})
    except Exception as e:
        return jsonify({"codes": [], "error": str(e)})

@app.route("/code/like", methods=["POST"])
def code_like():
    data = request.json or {}
    code_id = data.get("id")
    if not code_id:
        return jsonify({"error": "id manquant"}), 400
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute("UPDATE codes SET likes = likes + 1 WHERE id = ?", (code_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/code/working", methods=["POST"])
def code_working():
    data = request.json or {}
    code_id = data.get("id")
    if not code_id:
        return jsonify({"error": "id manquant"}), 400
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute("UPDATE codes SET working = working + 1 WHERE id = ?", (code_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/code/dead", methods=["POST"])
def code_dead():
    data = request.json or {}
    code_id = data.get("id")
    if not code_id:
        return jsonify({"error": "id manquant"}), 400
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute("UPDATE codes SET dead = dead + 1 WHERE id = ?", (code_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
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
                    "product_data": {
                        "name": "Accès CODE IA - À vie",
                        "description": "Codes promo + IA + Communauté"
                    },
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
    except Exception as e:
        return jsonify({"error": "Invalid"}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        telegram_id = session.get("client_reference_id")
        if not telegram_id and session.get("metadata"):
            telegram_id = session["metadata"].get("telegram_id")
        if telegram_id:
            add_paid_user(telegram_id)
            message = (
                "🎉 <b>Paiement confirmé !</b>\n\n"
                "Ton accès est activé.\n\n"
                "Clique sur le bouton pour ouvrir la plateforme :"
            )
            send_telegram_message(telegram_id, message, reply_markup=miniapp_keyboard())
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
            send_telegram_message(chat_id, "🔒 <b>Accès refusé</b>\n\nFais /start pour obtenir l'accès.")
            return jsonify(success=True)

        if str(chat_id) == str(ADMIN_ID) and admin_mode and not text.startswith("/"):
            send_telegram_message(CHANNEL_ID, text)
            send_telegram_message(chat_id, "✅ Publié anonymement")
            return jsonify(success=True)

        if text.startswith("/start"):
            if int(chat_id) == ADMIN_ID or is_paid_user(chat_id):
                send_telegram_message(
                    chat_id,
                    f"👋 Salut <b>{first_name}</b> !\n\nAccès activé.",
                    reply_markup=miniapp_keyboard()
                )
                return jsonify(success=True)
            try:
                response = requests.post(f"{SERVER_URL}/create-checkout", json={"telegram_id": chat_id}, timeout=10)
                result = response.json()
                if "url" in result:
                    keyboard = {"inline_keyboard": [[{"text": "💳 Payer 10 € – Accès à vie", "url": result["url"]}]]}
                    send_telegram_message(
                        chat_id,
                        f"👋 Salut <b>{first_name}</b> !\n\nBienvenue sur <b>CODE IA</b>.\n\nPrix : <b>10 €</b>",
                        reply_markup=keyboard
                    )
                else:
                    send_telegram_message(chat_id, "Erreur paiement.")
            except Exception as e:
                send_telegram_message(chat_id, "Erreur de connexion.")
                print(e)

               elif text.strip().lower() == "/payadmin":
            if int(chat_id) != ADMIN_ID:
                send_telegram_message(chat_id, "⛔ Commande réservée à l'admin.")
                return jsonify(success=True)

            message = (
                "🎉 <b>Paiement confirmé !</b>\n\n"
                "Ton accès est activé.\n\n"
                "Clique sur le bouton pour ouvrir la plateforme :"
            )
            send_telegram_message(chat_id, message, reply_markup=miniapp_keyboard())
            return jsonify(success=True)

        elif text == "/admin1" and str(chat_id) == str(ADMIN_ID):
            keyboard = {
                "inline_keyboard": [
                    [{"text": "▶️ Démarrer", "callback_data": "admin_start"}],
                    [{"text": "⏹️ Arrêter", "callback_data": "admin_stop"}]
                ]
            }
            send_telegram_message(chat_id, "🔐 Mode Administrateur", reply_markup=keyboard)

        elif text.lower().startswith("/promo "):
            parts = text[7:].strip().split()
            if len(parts) >= 3:
                try:
                    site = parts[0]
                    percent = int(parts[1])
                    code = parts[2].upper()
                    expire = parts[3] if len(parts) >= 4 else None
                    description = f"-{percent}%"
                    if expire:
                        description += f" (expire {expire})"
                    save_code("promo", site, code, description, None, display_name)
                    channel_message = (
                        f"🏷️ <b>CODE PROMO</b>\n\nDe : {display_name}\nSite : {site}\n"
                        f"Réduction : <b>-{percent}%</b>\nCode : <code>{code}</code>"
                    )
                    send_telegram_message(CHANNEL_ID, channel_message)
                    send_telegram_message(chat_id, "✅ Code promo publié et enregistré !")
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
                    expire = parts[3] if len(parts) >= 4 else None
                    description = f"+{montant}€"
                    if expire:
                        description += f" (expire {expire})"
                    save_code("parrainage", site, code, description, None, display_name)
                    channel_message = (
                        f"🔗 <b>CODE PARRAINAGE</b>\n\nDe : {display_name}\nSite : {site}\n"
                        f"Bonus : <b>+{montant}€</b>\nCode : <code>{code}</code>"
                    )
                    send_telegram_message(CHANNEL_ID, channel_message)
                    send_telegram_message(chat_id, "✅ Code parrainage publié et enregistré !")
                except:
                    send_telegram_message(chat_id, "Format : /parrainage Site 20 CODE")
            else:
                send_telegram_message(chat_id, "Format : /parrainage Site 20 CODE")

        elif text.startswith("/acces"):
            send_telegram_message(chat_id, "Voici ton accès :", reply_markup=miniapp_keyboard())

        elif text and not text.startswith("/"):
            send_telegram_message(chat_id, "🔍 Recherche en cours...")
            reply = ask_grok(text)
            send_telegram_message(chat_id, reply)

    return jsonify(success=True)

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
