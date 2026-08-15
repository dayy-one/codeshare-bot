from flask import Flask, request, jsonify
import stripe
import requests
import os
from datetime import datetime

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_LINK = os.getenv("CHANNEL_LINK")
ADMIN_ID = os.getenv("ADMIN_ID")
CHANNEL_ID = "-1004414166682"
SERVER_URL = "https://codeshare-bot-production.up.railway.app"

admin_mode = False

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

@app.route("/")
def home():
    return "CodeShare Server + Bot 24/7 is running ✅"

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
                        "name": "Accès CodeShare - À vie",
                        "description": "350 codes promo + 150 codes de parrainage"
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
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception as e:
        return str(e), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        telegram_id = getattr(session, "client_reference_id", None)
        if not telegram_id and hasattr(session, "metadata") and session.metadata:
            telegram_id = session.metadata.get("telegram_id")
        if telegram_id:
            message = (
                "🎉 <b>Paiement confirmé !</b>\n\n"
                "Ton accès a été activé automatiquement.\n\n"
                f"Voici le lien du canal privé :\n{CHANNEL_LINK}\n\n"
                "Bienvenue dans <b>CodeShare</b> !\n\n"
                "Tu peux maintenant partager tes codes :\n"
                "• <code>/promo NomDuSite 30 CODE</code>\n"
                "• <code>/parrainage NomDuSite 20 CODE</code>"
            )
            send_telegram_message(telegram_id, message)
            if ADMIN_ID:
                send_telegram_message(ADMIN_ID, f"✅ Nouveau paiement\nID : <code>{telegram_id}</code>")
    return jsonify(success=True)

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    global admin_mode
    data = request.get_json()

    # Boutons admin
    if "callback_query" in data:
        query = data["callback_query"]
        chat_id = query["from"]["id"]
        data_btn = query.get("data", "")

        if str(chat_id) != str(ADMIN_ID):
            return jsonify(success=True)

        if data_btn == "admin_start":
            admin_mode = True
            send_telegram_message(chat_id, "✅ <b>Mode Admin activé</b>\n\nTout ce que tu écris sera publié anonymement dans le canal.")
        elif data_btn == "admin_stop":
            admin_mode = False
            send_telegram_message(chat_id, "🛑 <b>Mode Admin désactivé</b>")

        return jsonify(success=True)

    if "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()
        user = message.get("from", {})
        first_name = user.get("first_name", "Utilisateur")
        username = user.get("username")
        display_name = f"@{username}" if username else first_name

        # Mode admin anonyme
        if str(chat_id) == str(ADMIN_ID) and admin_mode and not text.startswith("/"):
            send_telegram_message(CHANNEL_ID, text)
            send_telegram_message(chat_id, "✅ Publié anonymement dans le canal")
            return jsonify(success=True)

        # /start
        if text.startswith("/start"):
            try:
                response = requests.post(f"{SERVER_URL}/create-checkout", json={"telegram_id": chat_id}, timeout=10)
                result = response.json()
                if "url" in result:
                    keyboard = {"inline_keyboard": [[{"text": "💳 Payer 10 € – Accès à vie", "url": result["url"]}]]}
                    send_telegram_message(
                        chat_id,
                        f"👋 Salut <b>{first_name}</b> !\n\n"
                        f"Bienvenue sur <b>CodeShare</b>.\n\n"
                        f"Tu auras accès à :\n"
                        f"• 350 codes promo actifs\n"
                        f"• 150 codes de parrainage actifs\n"
                        f"• Possibilité de partager ton propre code\n\n"
                        f"<b>Prix : 10 €</b> (accès à vie)\n\n"
                        f"Clique sur le bouton pour payer.\n"
                        f"Dès le paiement terminé, tu recevras automatiquement le lien du canal.",
                        reply_markup=keyboard
                    )
                else:
                    send_telegram_message(chat_id, "Erreur lors de la création du paiement.")
            except Exception as e:
                send_telegram_message(chat_id, "Erreur de connexion.")
                print(e)

        # /admin1
        elif text == "/admin1" and str(chat_id) == str(ADMIN_ID):
            keyboard = {
                "inline_keyboard": [
                    [{"text": "▶️ Démarrer", "callback_data": "admin_start"}],
                    [{"text": "⏹️ Arrêter", "callback_data": "admin_stop"}]
                ]
            }
            send_telegram_message(chat_id, "🔐 <b>Mode Administrateur</b>\n\nClique sur Démarrer pour publier anonymement.", reply_markup=keyboard)

        # /promo Site Pourcentage Code [Date]
        elif text.lower().startswith("/promo "):
            parts = text[7:].strip().split()
            if len(parts) >= 3:
                site = parts[0]
                try:
                    percent = int(parts[1])
                    code = parts[2].upper()
                    expire = parts[3] if len(parts) >= 4 else None

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
                    send_telegram_message(chat_id, f"✅ Code promo publié !\n{site} | -{percent}% | {code}")
                except:
                    send_telegram_message(chat_id, "Format incorrect.\nUtilise : /promo Site 30 CODE\nou /promo Site 30 CODE 31/12/2026")
            else:
                send_telegram_message(chat_id, "Utilisation : /promo Site 30 CODE\nExemple : /promo Zara 30 SOLDES20")

        # /parrainage Site Montant Code [Date]
        elif text.lower().startswith("/parrainage "):
            parts = text[12:].strip().split()
            if len(parts) >= 3:
                site = parts[0]
                try:
                    montant = int(parts[1])
                    code = parts[2].upper()
                    expire = parts[3] if len(parts) >= 4 else None

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
                    send_telegram_message(chat_id, f"✅ Code de parrainage publié !\n{site} | +{montant}€ | {code}")
                except:
                    send_telegram_message(chat_id, "Format incorrect.\nUtilise : /parrainage Site 20 CODE\nou /parrainage Site 20 CODE 15/09/2026")
            else:
                send_telegram_message(chat_id, "Utilisation : /parrainage Site 20 CODE\nExemple : /parrainage Boursorama 20 REF123")

        elif text.startswith("/acces"):
            send_telegram_message(chat_id, f"Voici le lien du canal :\n{CHANNEL_LINK}")

    return jsonify(success=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
