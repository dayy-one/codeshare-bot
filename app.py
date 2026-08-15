from flask import Flask, request, jsonify
import stripe
import requests
import os

app = Flask(__name__)

# Variables d'environnement (on les mettra dans Railway)
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_LINK = os.getenv("CHANNEL_LINK")
ADMIN_ID = os.getenv("ADMIN_ID")

def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    requests.post(url, json=payload)

@app.route("/")
def home():
    return "CodeShare Server is running ✅"

@app.route("/create-checkout", methods=["POST"])
def create_checkout():
    data = request.json
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
                    "unit_amount": 1000,  # 10,00 €
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url="https://t.me/ton_bot_username",  # on changera plus tard
            cancel_url="https://t.me/ton_bot_username",
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
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except Exception as e:
        return str(e), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        telegram_id = session.get("client_reference_id") or session.get("metadata", {}).get("telegram_id")

        if telegram_id:
            message = (
                "🎉 *Paiement confirmé !*\n\n"
                "Ton accès a été activé automatiquement.\n\n"
                f"Voici le lien du canal privé :\n{CHANNEL_LINK}\n\n"
                "Bienvenue dans CodeShare !"
            )
            send_telegram_message(telegram_id, message)

            # Notification admin
            if ADMIN_ID:
                send_telegram_message(
                    ADMIN_ID,
                    f"✅ Nouveau paiement automatique\nID Telegram : `{telegram_id}`"
                )

    return jsonify(success=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
