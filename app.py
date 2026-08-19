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
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://codeshare-bot-production.up.railway.app/miniapp")
DATABASE_URL = os.getenv("DATABASE_URL")

client = OpenAI(api_key=os.getenv("XAI_API_KEY"), base_url="https://api.x.ai/v1")

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_conn()
    c = conn.cursor()

    # Table codes
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
            deleted BOOLEAN DEFAULT FALSE,
            expiry DATE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Ajout colonnes si elles n'existent pas
    try:
        c.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE")
    except: pass
    try:
        c.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS expiry DATE")
    except: pass

    # Table paid_users
    c.execute('''
        CREATE TABLE IF NOT EXISTS paid_users (
            telegram_id BIGINT PRIMARY KEY,
            paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Table copies uniques
    c.execute('''
        CREATE TABLE IF NOT EXISTS code_copies (
            code_id INTEGER,
            user_id BIGINT,
            PRIMARY KEY (code_id, user_id)
        )
    ''')

    # Table abonnements
    c.execute('''
        CREATE TABLE IF NOT EXISTS follows (
            follower_id BIGINT,
            following_id BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (follower_id, following_id)
        )
    ''')

    conn.commit()
    conn.close()
    print("✅ Base de données initialisée")

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
    except:
        return False

def add_paid_user(telegram_id):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO paid_users (telegram_id) VALUES (%s) ON CONFLICT DO NOTHING",
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

def save_code(code_type, site, code, description, link, added_by, user_id=None, photo_url=None, expiry=None):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''
            INSERT INTO codes (type, site, code, description, link, added_by, user_id, photo_url, likes, dislikes, copies, deleted, expiry)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,0,FALSE,%s)
        ''', (code_type, site, code, description, link, added_by, user_id, photo_url, expiry))
        conn.commit()
        conn.close()
    except Exception as e:
        print("Erreur save_code:", e)

def row_to_code(r):
    return {
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
        "copies": r["copies"] or 0,
        "expiry": str(r["expiry"]) if r.get("expiry") else None,
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None
    }

# ====================== ROUTES PRINCIPALES ======================

@app.route("/")
def home():
    return "Codia Server is running ✅"

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
    return jsonify({"paid": bool(is_paid_user(int(user_id)))})

@app.route("/codes", methods=["GET"])
def get_codes():
    try:
        conn = get_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute('''
            SELECT * FROM codes
            WHERE (deleted IS NULL OR deleted = FALSE)
            ORDER BY created_at DESC LIMIT 50
        ''')
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

    description = (data.get("description") or "").strip() or "Promo"
    expiry = data.get("expiry") or None

    save_code(
        data.get("type", "promo"),
        site, code, description, None,
        data.get("added_by") or "Membre Codia",
        data.get("user_id"),
        data.get("photo_url"),
        expiry
    )
    return jsonify({"success": True})

@app.route("/codes/delete", methods=["POST"])
def delete_code():
    data = request.json or {}
    code_id = data.get("id")
    user_id = data.get("user_id")

    if not code_id:
        return jsonify({"success": False, "error": "ID manquant"}), 400

    try:
        conn = get_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT user_id FROM codes WHERE id = %s", (code_id,))
        code = c.fetchone()

        if not code:
            conn.close()
            return jsonify({"success": False, "error": "Code introuvable"}), 404

        is_owner = str(code["user_id"]) == str(user_id)
        is_admin = int(user_id) == ADMIN_ID

        if not (is_admin or is_owner):
            conn.close()
            return jsonify({"success": False, "error": "Pas autorisé"}), 403

        c.execute("UPDATE codes SET deleted = TRUE WHERE id = %s", (code_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/codes/deleted")
def get_deleted_codes():
    try:
        conn = get_conn()
        c = conn.cursor(cursor_factory=RealDictCursor)
        c.execute("SELECT * FROM codes WHERE deleted = TRUE ORDER BY created_at DESC")
        rows = c.fetchall()
        conn.close()
        return jsonify({"codes": [row_to_code(r) for r in rows]})
    except Exception as e:
        return jsonify({"codes": [], "error": str(e)})

@app.route("/codes/restore", methods=["POST"])
def restore_code():
    data = request.json or {}
    code_id = data.get("id")
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("UPDATE codes SET deleted = FALSE WHERE id = %s", (code_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/code/copy", methods=["POST"])
def code_copy():
    data = request.json or {}
    code_id = data.get("id")
    user_id = data.get("user_id")

    if not code_id:
        return jsonify({"error": "id manquant"}), 400

    try:
        conn = get_conn()
        c = conn.cursor()

        if user_id:
            c.execute("SELECT 1 FROM code_copies WHERE code_id = %s AND user_id = %s", (code_id, user_id))
            if c.fetchone():
                c.execute("SELECT COALESCE(copies,0) FROM codes WHERE id = %s", (code_id,))
                value = c.fetchone()[0]
                conn.close()
                return jsonify({"success": True, "copies": value, "already": True})

            c.execute("INSERT INTO code_copies (code_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (code_id, user_id))

        c.execute("UPDATE codes SET copies = COALESCE(copies,0) + 1 WHERE id = %s", (code_id,))
        conn.commit()
        c.execute("SELECT COALESCE(copies,0) FROM codes WHERE id = %s", (code_id,))
        value = c.fetchone()[0]
        conn.close()
        return jsonify({"success": True, "copies": value})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Tu peux ajouter les autres routes (follow, react, stripe, telegram...) si besoin

try:
    init_db()
except Exception as e:
    print("Init DB error:", e)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
