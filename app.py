from flask import Flask, request, jsonify
import stripe
import requests
import os
import json
from openai import OpenAI
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8091031583"))
CHANNEL_ID = "-1004414166682"
SERVER_URL = os.getenv("SERVER_URL", "https://codeshare-bot-production.up.railway.app")
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://codeshare-bot-production.up.railway.app/miniapp")
DATABASE_URL = os.getenv("DATABASE_URL")

client = OpenAI(api_key=os.getenv("XAI_API_KEY"), base_url="https://api.x.ai/v1")
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
            copies INTEGER DEFAULT 0,
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

def set_menu_button(chat_id, text="Découvrir"):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setChatMenuButton",
            json={
                "chat_id": int(chat_id),
                "menu_button": {
                    "type": "web_app",
                    "text": text,
                    "web_app": {"url": MINIAPP_URL}
                }
            },
            timeout=10
        )
    except Exception as e:
        print("set_menu_button error:", e)

def save_code(code_type, site, code, description, link, added_by, user_id=None, photo_url=None):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''
            INSERT INTO codes (type, site, code, description, link, added_by, user_id, photo_url, likes, dislikes, copies)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,0)
        ''', (code_type, site, code, description, link, added_by, user_id, photo_url))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Erreur save_code:", e)

def row_to_code(r):
    return {
        "id": r["id"], "type": r["type"], "site": r["site"], "code": r["code"],
        "description": r["description"], "added_by": r["added_by"] or "Membre Codia",
        "user_id": r["user_id"], "photo_url": r["photo_url"],
        "likes": r["likes"] or 0, "dislikes": r["dislikes"] or 0, "copies": r["copies"] or 0,
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None
    }

def get_best_codes_text():
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''
            SELECT type, site, code, description, likes, dislikes FROM codes
            WHERE COALESCE(dislikes,0) < 8 OR COALESCE(likes,0) >= COALESCE(dislikes,0)
            ORDER BY (COALESCE(likes,0)-COALESCE(dislikes,0)) DESC, created_at DESC LIMIT 30
        ''')
        rows = c.fetchall()
        conn.close()
        if not rows:
            return "Aucun code validé pour le moment."
        text = "Meilleurs codes:\n\n"
        for r in rows:
            text += f"- {r[0]} | {r[1]} : {r[2]} | {r[3]}\n"
        return text
    except:
        return "Aucun code validé pour le moment."

def ask_grok(user_message: str, city=None):
    codes_context = get_best_codes_text()
    system_prompt = f"""
Tu es l'assistant Codia.
{('Localisation: '+city) if city else ''}
Codes communauté:
{codes_context}
Toujours répondre utilement, en français, sans réponse vide/négative.
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
        return "Voici une bonne piste : Booking, Uber Eats ou Getaround."

def send_telegram_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print("Erreur send:", e)

def send_daily_codes():
    prompt = """
Trouve des REMISES / CODES PROMO utiles en France.
Priorité: voyages/hôtels (Booking, Airbnb), vêtements (Zara, Nike, H&M...).
Règles: n'invente pas. Si rien de crédible, renvoie [].
JSON uniquement:
[{"site":"Booking","code":"XXX","description":"-15%","type":"promo","confidence":0.8}]
Garde confidence >= 0.7 sinon [].
"""
    try:
        response = client.chat.completions.create(
            model="grok-3",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1
        )
        text = response.choices[0].message.content.strip()
        start, end = text.find("["), text.rfind("]") + 1
        if start == -1 or end <= 0:
            return 0
        items = json.loads(text[start:end])
        selected = []
        used = set()
        for item in items if isinstance(items, list) else []:
            site = str(item.get("site", "")).strip()
            code = str(item.get("code", "")).strip().upper()
            conf = float(item.get("confidence", 0) or 0)
            if not site or not code or conf < 0.7 or code in ("XXXX", "CODE", "N/A"):
                continue
            if site.lower() in used:
                continue
            used.add(site.lower())
            selected.append({
                "site": site, "code": code,
                "description": str(item.get("description", "Remise")).strip(),
                "type": item.get("type", "promo")
            })
            if len(selected) >= 5:
                break
        if not selected:
            return 0

        for s in selected:
            save_code(s["type"], s["site"], s["code"], s["description"], None, "Codia IA")

        conn = get_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT telegram_id FROM paid_users")
        users = [u["telegram_id"] for u in c.fetchall()]
        conn.close()
        if ADMIN_ID not in users:
            users.append(ADMIN_ID)

        msg = "🆕 <b>Nouvelles remises — Codia</b>\n\nFocus voyages / hôtels / vêtements\n\n"
        for s in selected:
            msg += f"• <b>{s['site']}</b> — <code>{s['code']}</code> {s['description']}\n"
        msg += "\nVérifie avant paiement."
        for uid in users:
            send_telegram_message(uid, msg, reply_markup={
                "inline_keyboard": [[{"text": "Ouvrir Codia", "web_app": {"url": MINIAPP_URL}}]]
            })
        return len(users)
    except Exception as e:
        print("Daily error:", e)
        return 0

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

@app.route("/config")
def config():
    return jsonify({"stripe_pk": STRIPE_PUBLISHABLE_KEY})

@app.route("/access")
def access():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"paid": False})
    try:
        return jsonify({"paid": bool(is_paid_user(int(user_id)))})
    except Exception:
        return jsonify({"paid": False})

@app.route("/create-embedded-checkout", methods=["POST"])
def create_embedded_checkout():
    data = request.json or {}
    telegram_id = data.get("telegram_id")
    if not telegram_id:
        return jsonify({"error": "telegram_id manquant"}), 400
    try:
        session = stripe.checkout.Session.create(
            ui_mode="embedded",
            mode="payment",
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "eur",
                    "product_data": {
                        "name": "Accès Codia - À vie",
                        "description": "Feed, Discover IA, Profil, alertes remises"
                    },
                    "unit_amount": 1000,
                },
                "quantity": 1,
            }],
            return_url=MINIAPP_URL + "?paid=1&session_id={CHECKOUT_SESSION_ID}",
            client_reference_id=str(telegram_id),
            metadata={"telegram_id": str(telegram_id)},
        )
        return jsonify({"clientSecret": session.client_secret})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    try:
        conn = get_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''
            SELECT * FROM codes
            WHERE COALESCE(dislikes,0) < 8 OR COALESCE(likes,0) >= COALESCE(dislikes,0)
            ORDER BY (COALESCE(likes,0)-COALESCE(dislikes,0)) DESC, created_at DESC LIMIT 50
        ''')
        rows = c.fetchall()
        conn.close()
        return jsonify({"codes": [row_to_code(r) for r in rows]})
    except Exception as e:
        return jsonify({"codes": [], "error": str(e)})

@app.route("/codes/search")
def search_codes():
    q = (request.args.get("q") or "").strip().lower()
    try:
        conn = get_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        if q:
            c.execute('''
                SELECT * FROM codes
                WHERE (COALESCE(dislikes,0) < 8 OR COALESCE(likes,0) >= COALESCE(dislikes,0))
                  AND (LOWER(site) LIKE %s OR LOWER(code) LIKE %s OR LOWER(COALESCE(description,'')) LIKE %s)
                ORDER BY (COALESCE(likes,0)-COALESCE(dislikes,0)) DESC LIMIT 30
            ''', (f'%{q}%', f'%{q}%', f'%{q}%'))
        else:
            c.execute('''
                SELECT * FROM codes
                WHERE COALESCE(dislikes,0) < 8 OR COALESCE(likes,0) >= COALESCE(dislikes,0)
                ORDER BY (COALESCE(likes,0)-COALESCE(dislikes,0)) DESC LIMIT 20
            ''')
        rows = c.fetchall()
        conn.close()
        return jsonify({"codes": [row_to_code(r) for r in rows]})
    except Exception as e:
        return jsonify({"codes": [], "error": str(e)})

@app.route("/codes/mine")
def my_codes():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"codes": []})
    try:
        conn = get_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM codes WHERE user_id = %s ORDER BY created_at DESC LIMIT 50", (int(user_id),))
        rows = c.fetchall()
        conn.close()
        return jsonify({"codes": [row_to_code(r) for r in rows]})
    except Exception as e:
        return jsonify({"codes": [], "error": str(e)})

@app.route("/codes/add", methods=["POST"])
def add_code_from_app():
    data = request.json or {}
    site = (data.get("site") or "").strip()
    code = (data.get("code") or "").strip().upper()
    if not site or not code:
        return jsonify({"error": "site et code requis"}), 400
    description = (data.get("description") or "").strip() or ("Promo" if data.get("type") == "promo" else "Parrainage")
    save_code(data.get("type", "promo"), site, code, description, None,
              data.get("added_by") or "Membre Codia", data.get("user_id"), data.get("photo_url"))
    return jsonify({"success": True})

@app.route("/code/react", methods=["POST"])
def code_react():
    data = request.json or {}
    code_id, reaction, action = data.get("id"), data.get("reaction"), data.get("action")
    if not code_id or reaction not in ("like", "dislike") or action not in ("add", "remove"):
        return jsonify({"error": "paramètres invalides"}), 400
    column = "likes" if reaction == "like" else "dislikes"
    delta = 1 if action == "add" else -1
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute(f"UPDATE codes SET {column} = GREATEST(COALESCE({column},0)+%s,0) WHERE id=%s", (delta, code_id))
        conn.commit()
        c.execute(f"SELECT COALESCE({column},0) FROM codes WHERE id=%s", (code_id,))
        value = c.fetchone()[0]
        conn.close()
        return jsonify({"success": True, "value": value})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/code/copy", methods=["POST"])
def code_copy():
    code_id = (request.json or {}).get("id")
    if not code_id:
        return jsonify({"error": "id manquant"}), 400
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("UPDATE codes SET copies = COALESCE(copies,0)+1 WHERE id=%s", (code_id,))
        conn.commit()
        c.execute("SELECT COALESCE(copies,0) FROM codes WHERE id=%s", (code_id,))
        value = c.fetchone()[0]
        conn.close()
        return jsonify({"success": True, "copies": value})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/notify/daily", methods=["POST"])
def notify_daily():
    data = request.json or {}
    if str(data.get("admin_id")) != str(ADMIN_ID):
        return jsonify({"error": "unauthorized"}), 403
    try:
        return jsonify({"ok": True, "sent": send_daily_codes()})
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
            set_menu_button(telegram_id, "Ouvrir")
            send_telegram_message(
                telegram_id,
                "✅ <b>Accès activé</b>\n\nBienvenue sur Codia.\nAppuie sur <b>Ouvrir</b> en bas.",
                reply_markup={"inline_keyboard": [[{"text": "Ouvrir Codia", "web_app": {"url": MINIAPP_URL}}]]}
            )
            if ADMIN_ID:
                send_telegram_message(ADMIN_ID, f"✅ Paiement\nID: <code>{telegram_id}</code>")
    return jsonify(success=True), 200

@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    global admin_mode
    data = request.get_json()

    if "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()
        user = message.get("from", {})
        first_name = user.get("first_name", "toi")

        if text.startswith("/start"):
            paid = is_paid_user(chat_id)
            if paid:
                set_menu_button(chat_id, "Ouvrir")
                send_telegram_message(
                    chat_id,
                    f"👋 Rebonjour <b>{first_name}</b>\n\nTon accès Codia est actif.\nAppuie sur <b>Ouvrir</b> en bas.",
                    reply_markup={"inline_keyboard": [[{"text": "Ouvrir Codia", "web_app": {"url": MINIAPP_URL}}]]}
                )
            else:
                set_menu_button(chat_id, "Découvrir")
                send_telegram_message(
                    chat_id,
                    f"👋 Bienvenue sur <b>Codia</b>\n\n"
                    f"Codes promo & parrainage, recherche IA, communauté.\n\n"
                    f"Appuie sur <b>Découvrir</b> en bas pour voir l’app et débloquer l’accès.",
                    reply_markup={"inline_keyboard": [[{"text": "Découvrir", "web_app": {"url": MINIAPP_URL}}]]}
                )
            return jsonify(success=True)

        if not is_paid_user(chat_id):
            set_menu_button(chat_id, "Découvrir")
            send_telegram_message(
                chat_id,
                "🔒 Accès réservé aux membres.\nAppuie sur <b>Découvrir</b> pour voir Codia et débloquer l’accès.",
                reply_markup={"inline_keyboard": [[{"text": "Découvrir", "web_app": {"url": MINIAPP_URL}}]]}
            )
            return jsonify(success=True)

        set_menu_button(chat_id, "Ouvrir")

        if text.lower() == "/payadmin" and int(chat_id) == ADMIN_ID:
            send_telegram_message(chat_id, "✅ Accès admin", reply_markup={
                "inline_keyboard": [[{"text": "Ouvrir Codia", "web_app": {"url": MINIAPP_URL}}]]
            })
        elif text.lower() == "/daily" and int(chat_id) == ADMIN_ID:
            sent = send_daily_codes()
            send_telegram_message(chat_id, "ℹ️ Aucune remise fiable." if sent == 0 else f"✅ Envoyé à {sent}")
        elif text.lower().startswith("/promo "):
            parts = text[7:].strip().split()
            if len(parts) >= 3:
                try:
                    site, percent, code = parts[0], int(parts[1]), parts[2].upper()
                    save_code("promo", site, code, f"-{percent}%", None, f"@{user.get('username')}" if user.get("username") else first_name, chat_id)
                    send_telegram_message(chat_id, "✅ Code promo publié")
                except:
                    send_telegram_message(chat_id, "Format: /promo Site 30 CODE")
            else:
                send_telegram_message(chat_id, "Format: /promo Site 30 CODE")
        elif text.lower().startswith("/parrainage "):
            parts = text[12:].strip().split()
            if len(parts) >= 3:
                try:
                    site, montant, code = parts[0], int(parts[1]), parts[2].upper()
                    save_code("parrainage", site, code, f"+{montant}€", None, f"@{user.get('username')}" if user.get("username") else first_name, chat_id)
                    send_telegram_message(chat_id, "✅ Code parrainage publié")
                except:
                    send_telegram_message(chat_id, "Format: /parrainage Site 20 CODE")
            else:
                send_telegram_message(chat_id, "Format: /parrainage Site 20 CODE")
        elif text and not text.startswith("/"):
            send_telegram_message(chat_id, "🔍 Recherche...")
            send_telegram_message(chat_id, ask_grok(text))

    return jsonify(success=True)

try:
    init_db()
except Exception as e:
    print("Init DB error:", e)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
