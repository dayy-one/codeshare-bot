from flask import Flask, request, jsonify
import stripe
import requests
import os
import sqlite3
from openai import OpenAI
from datetime import datetime

app = Flask(__name__)

# ================== CONFIGURATION ==================
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

# ========== BASE DE DONNÉES ==========
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
        print("Erreur ajout paid_user:", e)

def save_code(code_type, site, code, description, link, added_by):
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute('''
            INSERT INTO codes (type, site, code, description, link, added_by, working, dead)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0)
        ''', (code_type, site, code, description, link, added_by))
        conn.commit()
        conn.close()
        print(f"Code sauvegardé : {site} - {code}")
    except Exception as e:
        print("Erreur sauvegarde code:", e)

def get_best_codes():
    try:
        conn = sqlite3.connect("codes.db")
        c = conn.cursor()
        c.execute('''
            SELECT type, site, code, description, working, dead 
            FROM codes 
            ORDER BY (working - dead) DESC, created_at DESC 
            LIMIT 30
        ''')
        rows = c.fetchall()
        conn.close()

        if not rows:
            return "Aucun code validé pour le moment."

        text = "Meilleurs codes de la communauté (triés par fiabilité) :\n\n"
        for row in rows:
            score = row[4] - row[5]
            text += f"- {row[0]} | {row[1]} : {row[2]} | {row[3]} | Score: {score}\n"
        return text
    except:
        return "Aucun code validé pour le moment."

def ask_grok(user_message: str):
    codes_context = get_best_codes()

    system_prompt = f"""
Tu es l'assistant officiel de CODE IA.

Voici les meilleurs codes de la communauté (triés par fiabilité) :
{codes_context}

RÈGLES ABSOLUES :
1. Tu dois TOUJOURS proposer quelque chose d'utile.
2. Interdiction totale de dire : "aucun code", "je n'ai rien trouvé", "pas disponible", "je ne sais pas".
3. Interdiction de répondre de façon vide ou négative.
4. Priorise les codes de la liste ci-dessus s'ils correspondent.
5. Si aucun code exact n'existe, propose la meilleure alternative possible (autre site, autre offre, autre remise).
6. Réponds toujours de façon claire, positive et en français.
7. Donne au minimum une proposition concrète (code, remise, ou alternative).
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
        return "Voici une bonne alternative pour toi : regarde les offres actuelles sur Booking, Uber Eats ou Getaround selon ton besoin."

def send_telegram_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        response = requests.post(url, json=payload, timeout=10)
        print(f"Envoi à {chat_id} → {response.status_code}")
    except Exception as e:
        print("Erreur:", e)

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
        return f"Erreur chargement Mini App : {e}", 500

@app.route("/ask", methods=["POST"])
def ask():
    data = request.json or {}
    question = data.get("question", "").strip()

    if not question:
        return jsonify({"answer": "Dis-moi précisément ce que tu recherches (ex: code Uber Eats, hôtel Paris, location voiture...)."}), 200

    answer = ask_grok(question)
    return jsonify({"answer": answer})

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
                        "description": "Codes promo + IA intelligente + Communauté"
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
        print("Webhook error:", e)
        return jsonify({"error": "Invalid"}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        
        telegram_id = session.get("client_reference_id")
        if not telegram_id and session.get("metadata"):
            telegram_id = session["metadata"].get("telegram_id")

        if telegram_id:
            add_paid_user(telegram_id)

            keyboard = {
                "inline_keyboard": [[
                    {
                        "text": "🚀 Ouvrir CODE IA",
                        "web_app": {
                            "url": MINIAPP_URL
                        }
                    }
                ]]
            }

            message = (
                "🎉 <b>Paiement confirmé !</b>\n\n"
                "Ton accès est activé.\n\n"
                "Clique sur le bouton ci-dessous pour ouvrir la plateforme :"
            )
            send_telegram_message(telegram_id, message, reply_markup=keyboard)
            
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

        # ========== BLOCAGE GLOBAL ==========
        if not is_paid_user(chat_id) and not text.startswith("/start"):
            send_telegram_message(
                chat_id,
                "🔒 <b>Accès refusé</b>\n\n"
                "Tu dois payer pour utiliser CODE IA.\n\n"
                "Fais /start pour obtenir l'accès."
            )
            return jsonify(success=True)

        # Mode admin anonyme
        if str(chat_id) == str(ADMIN_ID) and admin_mode and not text.startswith("/"):
            send_telegram_message(CHANNEL_ID, text)
            send_telegram_message(chat_id, "✅ Publié anonymement")
            return jsonify(success=True)

        # /start
        if text.startswith("/start"):
            if int(chat_id) == ADMIN_ID:
                keyboard = {
                    "inline_keyboard": [[
                        {
                            "text": "🚀 Ouvrir CODE IA",
                            "web_app": {"url": MINIAPP_URL}
                        }
                    ]]
                }
                send_telegram_message(chat_id, f"👋 Salut Admin <b>{first_name}</b> !\n\nAccès complet activé.", reply_markup=keyboard)
                return jsonify(success=True)

            if is_paid_user(chat_id):
                keyboard = {
                    "inline_keyboard": [[
                        {
                            "text": "🚀 Ouvrir CODE IA",
                            "web_app": {"url": MINIAPP_URL}
                        }
                    ]]
                }
                send_telegram_message(chat_id, f"👋 Rebonjour <b>{first_name}</b> !\n\nTu as déjà accès à CODE IA.", reply_markup=keyboard)
                return jsonify(success=True)

            try:
                response = requests.post(f"{SERVER_URL}/create-checkout", json={"telegram_id": chat_id}, timeout=10)
                result = response.json()
                if "url" in result:
                    keyboard = {"inline_keyboard": [[{"text": "💳 Payer 10 € – Accès à vie", "url": result["url"]}]]}
                    send_telegram_message(
                        chat_id,
                        f"👋 Salut <b>{first_name}</b> !\n\n"
                        f"Bienvenue sur <b>CODE IA</b>.\n\n"
                        f"Tu auras accès à :\n"
                        f"• Codes promo & parrainage\n"
                        f"• Assistant IA intelligent\n"
                        f"• Espace communauté\n\n"
                        f"<b>Prix : 10 €</b> (accès à vie)",
                        reply_markup=keyboard
                    )
                else:
                    send_telegram_message(chat_id, "Erreur lors de la création du paiement.")
            except Exception as e:
                send_telegram_message(chat_id, "Erreur de connexion.")
                print(e)

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
                site = parts[0]
                try:
                    percent = int(parts[1])
                    code = parts[2].upper()
                    expire = parts[3] if len(parts) >= 4 else None

                    description = f"-{percent}%"
                    if expire:
                        description += f" (expire le {expire})"

                    save_code("promo", site, code, description, None, display_name)

                    channel_message = (
                        f"🏷️ <b>CODE PROMO</b>\n\n"
                        f"De : {display_name}\n"
                        f"Site : {site}\n"
                        f"Réduction : <b>-{percent}%</b>\n"
                        f"Code : <code>{code}</code>\n"
                        f"Statut : ✅ Actif"
                    )
                    if expire:
                        channel_message += f"\nExpire le : {expire}"

                    send_telegram_message(CHANNEL_ID, channel_message)
                    send_telegram_message(chat_id, f"✅ Code promo publié et enregistré pour l'IA !")
                except:
                    send_telegram_message(chat_id, "Format incorrect.\nUtilise : /promo Site 30 CODE")
            else:
                send_telegram_message(chat_id, "Utilisation : /promo Site 30 CODE")

        elif text.lower().startswith("/parrainage "):
            parts = text[12:].strip().split()
            if len(parts) >= 3:
                site = parts[0]
                try:
                    montant = int(parts[1])
                    code = parts[2].upper()
                    expire = parts[3] if len(parts) >= 4 else None

                    description = f"+{montant}€"
                    if expire:
                        description += f" (expire le {expire})"

                    save_code("parrainage", site, code, description, None, display_name)

                    channel_message = (
                        f"🔗 <b>CODE DE PARRAINAGE</b>\n\n"
                        f"De : {display_name}\n"
                        f"Site : {site}\n"
                        f"Bonus : <b>+{montant}€</b>\n"
                        f"Code : <code>{code}</code>\n"
                        f"Statut : ✅ Actif"
                    )
                    if expire:
                        channel_message += f"\nExpire le : {expire}"

                    send_telegram_message(CHANNEL_ID, channel_message)
                    send_telegram_message(chat_id, f"✅ Code de parrainage publié et enregistré pour l'IA !")
                except:
                    send_telegram_message(chat_id, "Format incorrect.")
            else:
                send_telegram_message(chat_id, "Utilisation : /parrainage Site 20 CODE")

        elif text.startswith("/acces"):
            keyboard = {
                "inline_keyboard": [[
                    {
                        "text": "🚀 Ouvrir CODE IA",
                        "web_app": {"url": MINIAPP_URL}
                    }
                ]]
            }
            send_telegram_message(chat_id, "Voici ton accès à la plateforme :", reply_markup=keyboard)

        # ===== IA =====
        elif text and not text.startswith("/"):
            send_telegram_message(chat_id, "🔍 Recherche en cours avec CODE IA...")
            reply = ask_grok(text)
            send_telegram_message(chat_id, reply)

    return jsonify(success=True)

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
