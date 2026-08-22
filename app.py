import os
import json
import logging
import random
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
ADMIN_ID = int(os.getenv("ADMIN_ID", "8091031583"))
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/+ODE8T52A5yEzMTZk")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004414166682")
DATABASE_URL = os.getenv("DATABASE_URL")
XAI_API_KEY = os.getenv("XAI_API_KEY")
SERVER_URL = os.getenv("SERVER_URL", "https://codeshare-bot-production.up.railway.app")
MINIAPP_URL = os.getenv("MINIAPP_URL", f"{SERVER_URL}/miniapp?v=11")
PRICE_CENTS = int(os.getenv("PRICE_CENTS", "1000"))

BASE_MEMBERS = 2345

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
 telegram_id BIGINT PRIMARY KEY,
 paid_at TIMESTAMP DEFAULT NOW()
 );
 """)
 cur.execute("""
 CREATE TABLE IF NOT EXISTS codes (
 id SERIAL PRIMARY KEY,
 type VARCHAR(20) DEFAULT 'promo',
 site VARCHAR(120),
 code VARCHAR(120),
 description TEXT,
 url TEXT,
 expires_at TIMESTAMP,
 added_by VARCHAR(120),
 user_id BIGINT,
 photo_url TEXT,
 likes INT DEFAULT 0,
 dislikes INT DEFAULT 0,
 copies INT DEFAULT 0,
 deleted BOOLEAN DEFAULT FALSE,
 created_at TIMESTAMP DEFAULT NOW()
 );
 """)
 try:
 cur.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE;")
 except Exception:
 pass
 try:
 cur.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS url TEXT;")
 except Exception:
 pass
 try:
 cur.execute("ALTER TABLE codes ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP;")
 except Exception:
 pass

 cur.execute("""
 CREATE TABLE IF NOT EXISTS follows (
 id SERIAL PRIMARY KEY,
 follower_id BIGINT NOT NULL,
 followed_id BIGINT NOT NULL,
 created_at TIMESTAMP DEFAULT NOW(),
 UNIQUE(follower_id, followed_id)
 );
 """)
 cur.execute("""
 CREATE TABLE IF NOT EXISTS code_copies (
 code_id INT NOT NULL,
 user_id BIGINT NOT NULL,
 created_at TIMESTAMP DEFAULT NOW(),
 PRIMARY KEY (code_id, user_id)
 );
 """)
 cur.execute("""
 CREATE TABLE IF NOT EXISTS notifications (
 id SERIAL PRIMARY KEY,
 user_id BIGINT NOT NULL,
 type VARCHAR(20),
 actor_id BIGINT,
 actor_name VARCHAR(120),
 code_id INT,
 message TEXT,
 is_read BOOLEAN DEFAULT FALSE,
 created_at TIMESTAMP DEFAULT NOW()
 );
 """)
 cur.execute("""
 CREATE TABLE IF NOT EXISTS search_logs (
 id SERIAL PRIMARY KEY,
 query VARCHAR(200) NOT NULL,
 created_at TIMESTAMP DEFAULT NOW()
 );
 """)
 cur.execute("""
 CREATE TABLE IF NOT EXISTS saved_codes (
 user_id BIGINT NOT NULL,
 code_id INT NOT NULL,
 created_at TIMESTAMP DEFAULT NOW(),
 PRIMARY KEY (user_id, code_id)
 );
 """)
 cur.execute("""
 CREATE TABLE IF NOT EXISTS user_settings (
 telegram_id BIGINT PRIMARY KEY,
 push_enabled BOOLEAN DEFAULT TRUE,
 updated_at TIMESTAMP DEFAULT NOW()
 );
 """)

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
 "INSERT INTO paid_users (telegram_id) VALUES (%s) ON CONFLICT (telegram_id) DO NOTHING",
 (int(telegram_id),),
 )
 conn.commit()
 cur.close()
 conn.close()


def send_telegram_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
 if not TELEGRAM_TOKEN:
 return
 url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
 payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
 if reply_markup:
 payload["reply_markup"] = reply_markup
 try:
 requests.post(url, json=payload, timeout=12)
 except Exception as e:
 logging.error(f"send_telegram_message: {e}")


def is_push_enabled(telegram_id: int) -> bool:
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("SELECT push_enabled FROM user_settings WHERE telegram_id = %s", (int(telegram_id),))
 row = cur.fetchone()
 cur.close()
 conn.close()
 return True if row is None else bool(row["push_enabled"])
 except Exception:
 return True


def set_push_enabled(telegram_id: int, enabled: bool):
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("""
 INSERT INTO user_settings (telegram_id, push_enabled)
 VALUES (%s, %s)
 ON CONFLICT (telegram_id) DO UPDATE SET push_enabled = EXCLUDED.push_enabled, updated_at = NOW()
 """, (int(telegram_id), enabled))
 conn.commit()
 cur.close()
 conn.close()
 except Exception as e:
 logging.error(f"set_push_enabled: {e}")


def send_push(chat_id, text):
 if not is_push_enabled(chat_id):
 return
 reply_markup = {
 "inline_keyboard": [[{
 "text": "Ouvrir COD.IA",
 "web_app": {"url": MINIAPP_URL}
 }]]
 }
 send_telegram_message(chat_id, text, reply_markup=reply_markup)


def create_notification(user_id, notif_type, actor_id, actor_name, code_id, message):
 if not user_id or int(user_id) == int(actor_id or 0):
 return
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute(
 """
 INSERT INTO notifications (user_id, type, actor_id, actor_name, code_id, message)
 VALUES (%s, %s, %s, %s, %s, %s)
 """,
 (int(user_id), notif_type, actor_id, actor_name, code_id, message),
 )
 conn.commit()
 cur.close()
 conn.close()
 send_push(int(user_id), message)
 except Exception as e:
 logging.error(f"create_notification: {e}")


def discover_keyboard(paid: bool):
 btn_text = "Ouvrir COD.IA" if paid else "Découvrir"
 return {"inline_keyboard": [[{"text": btn_text, "web_app": {"url": MINIAPP_URL}}]]}


def grant_access_message(chat_id):
 send_telegram_message(
 chat_id,
 "✅ <b>Accès COD.IA activé</b>\n\nTu peux ouvrir l’app maintenant.\n"
 f"Canal : {CHANNEL_LINK}",
 reply_markup=discover_keyboard(True),
 )


# Filtre pour cacher les codes expirés depuis plus de 4 jours
ACTIVE_CODES_FILTER = """
 deleted = FALSE
 AND (expires_at IS NULL OR expires_at > NOW() - INTERVAL '4 days')
"""


@app.route("/")
def home():
 return "COD.IA Server is running ✅"


@app.route("/miniapp")
def miniapp():
 resp = send_from_directory(".", "miniapp.html")
 resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
 resp.headers["Pragma"] = "no-cache"
 resp.headers["Expires"] = "0"
 return resp


@app.route("/config")
def config():
 return jsonify({"stripe_pk": STRIPE_PUBLISHABLE_KEY})


@app.route("/access")
def access():
 try:
 uid = int(request.args.get("user_id"))
 except Exception:
 return jsonify({"paid": False})
 return jsonify({"paid": is_paid(uid), "is_admin": uid == ADMIN_ID})


@app.route("/stats")
def stats():
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("SELECT COUNT(*) AS c FROM paid_users")
 real = cur.fetchone()["c"] or 0
 cur.close()
 conn.close()
 total = BASE_MEMBERS + int(real)
 display = f"{total / 1000:.3f}k"
 return jsonify({"members": total, "members_display": display})
 except Exception as e:
 logging.error(e)
 return jsonify({"members": BASE_MEMBERS, "members_display": "2.345k"})


@app.route("/create-checkout", methods=["POST"])
def create_checkout():
 data = request.json or {}
 telegram_id = data.get("telegram_id")
 if not telegram_id:
 return jsonify({"error": "telegram_id manquant"}), 400
 success = f"{MINIAPP_URL}&paid=1" if "?" in MINIAPP_URL else f"{MINIAPP_URL}?paid=1"
 try:
 session = stripe.checkout.Session.create(
 mode="payment",
 payment_method_types=["card"],
 line_items=[{
 "price_data": {
 "currency": "eur",
 "product_data": {"name": "COD.IA — accès à vie"},
 "unit_amount": PRICE_CENTS,
 },
 "quantity": 1,
 }],
 success_url=success,
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
 return_url = f"{MINIAPP_URL}&paid=1" if "?" in MINIAPP_URL else f"{MINIAPP_URL}?paid=1"
 try:
 session = stripe.checkout.Session.create(
 ui_mode="embedded_page",
 mode="payment",
 line_items=[{
 "price_data": {
 "currency": "eur",
 "product_data": {"name": "COD.IA — accès à vie"},
 "unit_amount": PRICE_CENTS,
 },
 "quantity": 1,
 }],
 return_url=return_url,
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


@app.route("/codes")
def list_codes():
 type_filter = request.args.get("type") # promo / parrainage / None
 try:
 conn = get_conn()
 cur = conn.cursor()
 if type_filter in ("promo", "parrainage"):
 cur.execute(f"""
 SELECT * FROM codes
 WHERE {ACTIVE_CODES_FILTER} AND type = %s
 ORDER BY created_at DESC LIMIT 100
 """, (type_filter,))
 else:
 cur.execute(f"""
 SELECT * FROM codes
 WHERE {ACTIVE_CODES_FILTER}
 ORDER BY created_at DESC LIMIT 100
 """)
 rows = cur.fetchall()
 cur.close()
 conn.close()
 return jsonify({"codes": rows})
 except Exception as e:
 logging.error(e)
 return jsonify({"codes": []})


@app.route("/codes/top")
def top_codes():
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute(f"""
 SELECT * FROM codes
 WHERE {ACTIVE_CODES_FILTER}
 ORDER BY (likes + copies) DESC, created_at DESC
 LIMIT 5
 """)
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
 cur.execute(f"""
 SELECT * FROM codes
 WHERE {ACTIVE_CODES_FILTER}
 AND (site ILIKE %s OR code ILIKE %s OR description ILIKE %s)
 ORDER BY created_at DESC LIMIT 50
 """, (f"%{q}%", f"%{q}%", f"%{q}%"))
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
 cur.execute("SELECT * FROM codes WHERE user_id = %s ORDER BY created_at DESC LIMIT 50", (int(user_id),))
 rows = cur.fetchall()
 cur.close()
 conn.close()
 return jsonify({"codes": rows})
 except Exception as e:
 logging.error(e)
 return jsonify({"codes": []})


@app.route("/codes/user")
def user_codes():
 user_id = request.args.get("user_id")
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute(f"""
 SELECT * FROM codes
 WHERE user_id = %s AND {ACTIVE_CODES_FILTER}
 ORDER BY created_at DESC LIMIT 50
 """, (int(user_id),))
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

 expires_at = data.get("expires_at") or None
 if expires_at:
 try:
 # accepte "2026-09-15" ou datetime
 expires_at = datetime.fromisoformat(expires_at.replace("Z", ""))
 except Exception:
 expires_at = None

 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("""
 INSERT INTO codes (type, site, code, description, url, expires_at, added_by, user_id, photo_url)
 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
 """, (
 data.get("type") or "promo",
 data.get("site"),
 data.get("code"),
 data.get("description"),
 data.get("url") or None,
 expires_at,
 data.get("added_by"),
 data.get("user_id"),
 data.get("photo_url"),
 ))
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
 user_id = data.get("user_id")
 actor_name = data.get("actor_name") or "Quelqu’un"
 if not code_id or not user_id:
 return jsonify({"copies": 0, "already": True})
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("SELECT 1 FROM code_copies WHERE code_id = %s AND user_id = %s", (code_id, int(user_id)))
 if cur.fetchone():
 cur.execute("SELECT copies FROM codes WHERE id = %s", (code_id,))
 row = cur.fetchone()
 cur.close()
 conn.close()
 return jsonify({"copies": row["copies"] if row else 0, "already": True})

 cur.execute("INSERT INTO code_copies (code_id, user_id) VALUES (%s, %s)", (code_id, int(user_id)))
 cur.execute("UPDATE codes SET copies = copies + 1 WHERE id = %s RETURNING copies, user_id, site, code", (code_id,))
 row = cur.fetchone()
 conn.commit()
 cur.close()
 conn.close()

 if row and row["user_id"] and int(row["user_id"]) != int(user_id):
 msg = f"📋 <b>{actor_name}</b> a copié ton code <code>{row['code']}</code> sur <b>{row['site']}</b>"
 create_notification(row["user_id"], "copy", user_id, actor_name, code_id, msg)

 return jsonify({"copies": row["copies"] if row else 0, "already": False})
 except Exception as e:
 logging.error(e)
 return jsonify({"copies": 0, "already": False})


@app.route("/code/react", methods=["POST"])
def code_react():
 data = request.json or {}
 code_id = data.get("id")
 reaction = data.get("reaction")
 action = data.get("action")
 user_id = data.get("user_id")
 actor_name = data.get("actor_name") or "Quelqu’un"
 col = "likes" if reaction == "like" else "dislikes"
 delta = 1 if action == "add" else -1
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute(
 f"UPDATE codes SET {col} = GREATEST({col} + %s, 0) WHERE id = %s RETURNING {col} AS value, user_id, site, code",
 (delta, code_id),
 )
 row = cur.fetchone()
 conn.commit()
 cur.close()
 conn.close()

 if action == "add" and row and row["user_id"] and user_id and int(row["user_id"]) != int(user_id):
 if reaction == "like":
 msg = f"❤️ <b>{actor_name}</b> a aimé ton code <code>{row['code']}</code> sur <b>{row['site']}</b>"
 create_notification(row["user_id"], "like", user_id, actor_name, code_id, msg)
 else:
 msg = f"👎 <b>{actor_name}</b> n’a pas aimé ton code <code>{row['code']}</code> sur <b>{row['site']}</b>"
 create_notification(row["user_id"], "dislike", user_id, actor_name, code_id, msg)

 return jsonify({"value": row["value"] if row else 0})
 except Exception as e:
 logging.error(e)
 return jsonify({"value": 0})


@app.route("/code/delete", methods=["POST"])
def code_delete():
 data = request.json or {}
 code_id = data.get("id")
 user_id = data.get("user_id")
 if not code_id or not user_id:
 return jsonify({"success": False}), 400
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("SELECT user_id FROM codes WHERE id = %s", (code_id,))
 row = cur.fetchone()
 if not row:
 cur.close()
 conn.close()
 return jsonify({"success": False}), 404
 owner_id = row["user_id"]
 if not (int(user_id) == ADMIN_ID or (owner_id and int(owner_id) == int(user_id))):
 cur.close()
 conn.close()
 return jsonify({"success": False}), 403
 cur.execute("UPDATE codes SET deleted = TRUE WHERE id = %s", (code_id,))
 conn.commit()
 cur.close()
 conn.close()
 return jsonify({"success": True})
 except Exception as e:
 logging.error(e)
 return jsonify({"success": False}), 500


@app.route("/code/restore", methods=["POST"])
def code_restore():
 data = request.json or {}
 code_id = data.get("id")
 user_id = data.get("user_id")
 if not code_id or not user_id:
 return jsonify({"success": False}), 400
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("SELECT user_id FROM codes WHERE id = %s", (code_id,))
 row = cur.fetchone()
 if not row:
 cur.close()
 conn.close()
 return jsonify({"success": False}), 404
 owner_id = row["user_id"]
 if not (int(user_id) == ADMIN_ID or (owner_id and int(owner_id) == int(user_id))):
 cur.close()
 conn.close()
 return jsonify({"success": False}), 403
 cur.execute("UPDATE codes SET deleted = FALSE WHERE id = %s", (code_id,))
 conn.commit()
 cur.close()
 conn.close()
 return jsonify({"success": True})
 except Exception as e:
 logging.error(e)
 return jsonify({"success": False}), 500


@app.route("/code/save", methods=["POST"])
def save_code():
 data = request.json or {}
 user_id = data.get("user_id")
 code_id = data.get("id")
 actor_name = data.get("actor_name") or "Quelqu’un"
 if not user_id or not code_id:
 return jsonify({"success": False}), 400
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("""
 INSERT INTO saved_codes (user_id, code_id)
 VALUES (%s, %s)
 ON CONFLICT (user_id, code_id) DO NOTHING
 RETURNING code_id
 """, (int(user_id), int(code_id)))
 inserted = cur.fetchone()
 if inserted:
 cur.execute("SELECT user_id, site, code FROM codes WHERE id = %s", (code_id,))
 owner = cur.fetchone()
 if owner and owner["user_id"] and int(owner["user_id"]) != int(user_id):
 msg = f"🔖 <b>{actor_name}</b> a ajouté ton code <code>{owner['code']}</code> ({owner['site']}) à ses favoris"
 create_notification(owner["user_id"], "save", user_id, actor_name, code_id, msg)
 conn.commit()
 cur.close()
 conn.close()
 return jsonify({"success": True, "saved": True})
 except Exception as e:
 logging.error(e)
 return jsonify({"success": False}), 500


@app.route("/code/unsave", methods=["POST"])
def unsave_code():
 data = request.json or {}
 user_id = data.get("user_id")
 code_id = data.get("id")
 if not user_id or not code_id:
 return jsonify({"success": False}), 400
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("DELETE FROM saved_codes WHERE user_id = %s AND code_id = %s", (int(user_id), int(code_id)))
 conn.commit()
 cur.close()
 conn.close()
 return jsonify({"success": True, "saved": False})
 except Exception as e:
 logging.error(e)
 return jsonify({"success": False}), 500


@app.route("/codes/saved")
def saved_codes():
 user_id = request.args.get("user_id")
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute(f"""
 SELECT c.* FROM codes c
 INNER JOIN saved_codes s ON s.code_id = c.id
 WHERE s.user_id = %s AND {ACTIVE_CODES_FILTER}
 ORDER BY s.created_at DESC
 LIMIT 50
 """, (int(user_id),))
 rows = cur.fetchall()
 cur.close()
 conn.close()
 return jsonify({"codes": rows})
 except Exception as e:
 logging.error(e)
 return jsonify({"codes": []})


@app.route("/follow", methods=["POST"])
def follow():
 data = request.json or {}
 follower_id = data.get("follower_id")
 followed_id = data.get("followed_id")
 actor_name = data.get("actor_name") or "Quelqu’un"
 if not follower_id or not followed_id or int(follower_id) == int(followed_id):
 return jsonify({"success": False}), 400
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("""
 INSERT INTO follows (follower_id, followed_id)
 VALUES (%s, %s)
 ON CONFLICT (follower_id, followed_id) DO NOTHING
 RETURNING id
 """, (int(follower_id), int(followed_id)))
 row = cur.fetchone()
 conn.commit()
 cur.close()
 conn.close()
 if row:
 msg = f"👤 <b>{actor_name}</b> s’est abonné à ton profil COD.IA"
 create_notification(followed_id, "follow", follower_id, actor_name, None, msg)
 return jsonify({"success": True, "following": True})
 except Exception as e:
 logging.error(e)
 return jsonify({"success": False}), 500


@app.route("/unfollow", methods=["POST"])
def unfollow():
 data = request.json or {}
 follower_id = data.get("follower_id")
 followed_id = data.get("followed_id")
 if not follower_id or not followed_id:
 return jsonify({"success": False}), 400
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("DELETE FROM follows WHERE follower_id = %s AND followed_id = %s", (int(follower_id), int(followed_id)))
 conn.commit()
 cur.close()
 conn.close()
 return jsonify({"success": True, "following": False})
 except Exception as e:
 logging.error(e)
 return jsonify({"success": False}), 500


@app.route("/is_following")
def is_following():
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("SELECT 1 FROM follows WHERE follower_id = %s AND followed_id = %s",
 (int(request.args.get("follower")), int(request.args.get("followed"))))
 row = cur.fetchone()
 cur.close()
 conn.close()
 return jsonify({"following": bool(row)})
 except Exception:
 return jsonify({"following": False})


@app.route("/followers")
def followers():
 user_id = request.args.get("user_id")
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("""
 SELECT f.follower_id AS user_id,
 COALESCE(
 (SELECT added_by FROM codes WHERE user_id = f.follower_id ORDER BY created_at DESC LIMIT 1),
 'Membre COD.IA'
 ) AS name,
 (SELECT photo_url FROM codes WHERE user_id = f.follower_id AND photo_url IS NOT NULL ORDER BY created_at DESC LIMIT 1) AS photo_url
 FROM follows f
 WHERE f.followed_id = %s
 ORDER BY f.created_at DESC LIMIT 100
 """, (int(user_id),))
 rows = cur.fetchall()
 cur.close()
 conn.close()
 return jsonify({"users": rows})
 except Exception as e:
 logging.error(e)
 return jsonify({"users": []})


@app.route("/following")
def following():
 user_id = request.args.get("user_id")
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("""
 SELECT f.followed_id AS user_id,
 COALESCE(
 (SELECT added_by FROM codes WHERE user_id = f.followed_id ORDER BY created_at DESC LIMIT 1),
 'Membre COD.IA'
 ) AS name,
 (SELECT photo_url FROM codes WHERE user_id = f.followed_id AND photo_url IS NOT NULL ORDER BY created_at DESC LIMIT 1) AS photo_url
 FROM follows f
 WHERE f.follower_id = %s
 ORDER BY f.created_at DESC LIMIT 100
 """, (int(user_id),))
 rows = cur.fetchall()
 cur.close()
 conn.close()
 return jsonify({"users": rows})
 except Exception as e:
 logging.error(e)
 return jsonify({"users": []})


@app.route("/profile_stats")
def profile_stats():
 user_id = request.args.get("user_id")
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("SELECT COUNT(*) AS c FROM follows WHERE followed_id = %s", (int(user_id),))
 followers_count = cur.fetchone()["c"]
 cur.execute("SELECT COUNT(*) AS c FROM follows WHERE follower_id = %s", (int(user_id),))
 following_count = cur.fetchone()["c"]
 cur.close()
 conn.close()
 return jsonify({"followers": followers_count, "following": following_count})
 except Exception:
 return jsonify({"followers": 0, "following": 0})


@app.route("/notifications")
def get_notifications():
 user_id = request.args.get("user_id")
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("SELECT * FROM notifications WHERE user_id = %s ORDER BY created_at DESC LIMIT 50", (int(user_id),))
 rows = cur.fetchall()
 cur.execute("SELECT COUNT(*) AS c FROM notifications WHERE user_id = %s AND is_read = FALSE", (int(user_id),))
 unread = cur.fetchone()["c"]
 cur.close()
 conn.close()
 return jsonify({"notifications": rows, "unread": unread})
 except Exception as e:
 logging.error(e)
 return jsonify({"notifications": [], "unread": 0})


@app.route("/notifications/read", methods=["POST"])
def mark_notifications_read():
 data = request.json or {}
 user_id = data.get("user_id")
 if not user_id:
 return jsonify({"success": False}), 400
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("UPDATE notifications SET is_read = TRUE WHERE user_id = %s AND is_read = FALSE", (int(user_id),))
 conn.commit()
 cur.close()
 conn.close()
 return jsonify({"success": True})
 except Exception as e:
 logging.error(e)
 return jsonify({"success": False}), 500


@app.route("/settings/push", methods=["GET", "POST"])
def settings_push():
 if request.method == "GET":
 user_id = request.args.get("user_id")
 if not user_id:
 return jsonify({"enabled": True})
 return jsonify({"enabled": is_push_enabled(int(user_id))})

 data = request.json or {}
 user_id = data.get("user_id")
 enabled = data.get("enabled", True)
 if not user_id:
 return jsonify({"success": False}), 400
 set_push_enabled(int(user_id), bool(enabled))
 return jsonify({"success": True, "enabled": bool(enabled)})


@app.route("/search/log", methods=["POST"])
def log_search():
 data = request.json or {}
 q = (data.get("q") or "").strip()
 if len(q) < 2:
 return jsonify({"ok": True})
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("INSERT INTO search_logs (query) VALUES (%s)", (q[:200],))
 conn.commit()
 cur.close()
 conn.close()
 except Exception as e:
 logging.error(e)
 return jsonify({"ok": True})


@app.route("/search/recent")
def recent_searches():
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute("""
 SELECT query FROM search_logs
 WHERE created_at > NOW() - INTERVAL '30 days'
 GROUP BY query
 ORDER BY MAX(created_at) DESC
 LIMIT 40
 """)
 rows = cur.fetchall()
 cur.close()
 conn.close()
 queries = [r["query"] for r in rows]
 random.shuffle(queries)
 return jsonify({"queries": queries[:12]})
 except Exception as e:
 logging.error(e)
 return jsonify({"queries": []})


@app.route("/ask", methods=["POST"])
def ask():
 data = request.json or {}
 question = (data.get("question") or "").strip()
 if not question:
 return jsonify({"answer": "Dis-moi ce que tu cherches."})
 if not client:
 return jsonify({"answer": "IA non configurée (XAI_API_KEY manquante)."})

 # Récupère les codes actifs pour enrichir le prompt
 active_codes_text = ""
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute(f"""
 SELECT site, code, description, type
 FROM codes
 WHERE {ACTIVE_CODES_FILTER}
 ORDER BY created_at DESC
 LIMIT 40
 """)
 rows = cur.fetchall()
 cur.close()
 conn.close()
 if rows:
 lines = []
 for r in rows:
 lines.append(f"- {r['site']} | {r['code']} | {r['description'] or ''} ({r['type']})")
 active_codes_text = "\n".join(lines)
 except Exception as e:
 logging.error(e)

 system = (
 "Tu es l'assistant COD.IA. Tu aides les utilisateurs à trouver des codes promo et parrainages en France. "
 "Réponds en français, de façon courte, claire et utile.\n\n"
 "Voici les codes actuellement disponibles dans l'application :\n"
 f"{active_codes_text or 'Aucun code pour le moment.'}\n\n"
 "Si l'utilisateur cherche un code, recommande en priorité ceux de la liste ci-dessus. "
 "Si aucun ne correspond, donne des conseils généraux."
 )

 try:
 completion = client.chat.completions.create(
 model="grok-2-latest",
 messages=[
 {"role": "system", "content": system},
 {"role": "user", "content": question},
 ],
 temperature=0.5,
 )
 return jsonify({"answer": completion.choices[0].message.content})
 except Exception as e:
 logging.error(e)
 return jsonify({"answer": "Essaie Booking, Uber Eats, Zara ou les banques en ligne."})


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

 if text.startswith("/start"):
 paid = is_paid(user_id)
 if paid:
 send_telegram_message(
 chat_id,
 f"👋 Salut <b>{user.get('first_name') or ''}</b>\n\nTon accès COD.IA est actif.\nClique sur <b>Ouvrir</b>.",
 reply_markup=discover_keyboard(True),
 )
 else:
 send_telegram_message(
 chat_id,
 "👋 Bienvenue sur <b>COD.IA</b>\n\nCodes promo, parrainage, IA et communauté.\nClique sur <b>Découvrir</b>.",
 reply_markup=discover_keyboard(False),
 )
 return jsonify(success=True)

 if text.strip().lower() == "/payadmin":
 if int(user_id) != ADMIN_ID:
 send_telegram_message(chat_id, "Commande réservée à l’admin.")
 return jsonify(success=True)
 mark_paid(user_id)
 grant_access_message(chat_id)
 return jsonify(success=True)

 if not is_paid(user_id):
 send_telegram_message(
 chat_id,
 "🔒 Accès réservé. Clique sur Découvrir.",
 reply_markup=discover_keyboard(False),
 )
 return jsonify(success=True)

 if text.startswith("/acces"):
 send_telegram_message(chat_id, f"Lien canal :\n{CHANNEL_LINK}")
 return jsonify(success=True)

 if text.startswith("/promo"):
 parts = text.split()
 if len(parts) >= 4:
 site, montant, code = parts[1], parts[2], parts[3]
 display = f"@{user.get('username')}" if user.get("username") else user.get("first_name", "Membre")
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute(
 "INSERT INTO codes (type, site, code, description, added_by, user_id) VALUES ('promo', %s, %s, %s, %s, %s)",
 (site, code, f"-{montant}%", display, user_id),
 )
 conn.commit()
 cur.close()
 conn.close()
 except Exception as e:
 logging.error(e)
 send_telegram_message(
 CHANNEL_ID,
 f"🏷 <b>CODE PROMO</b>\n\nDe : {display}\nSite : {site}\nRemise : <b>{montant}%</b>\nCode : <code>{code}</code>",
 )
 send_telegram_message(chat_id, f"✅ Promo publiée : {site} | {code}")
 else:
 send_telegram_message(chat_id, "Format : /promo Site 30 CODE")
 return jsonify(success=True)

 if text.startswith("/parrainage"):
 parts = text.split()
 if len(parts) >= 4:
 site, montant, code = parts[1], parts[2], parts[3]
 display = f"@{user.get('username')}" if user.get("username") else user.get("first_name", "Membre")
 try:
 conn = get_conn()
 cur = conn.cursor()
 cur.execute(
 "INSERT INTO codes (type, site, code, description, added_by, user_id) VALUES ('parrainage', %s, %s, %s, %s, %s)",
 (site, code, f"+{montant}€", display, user_id),
 )
 conn.commit()
 cur.close()
 conn.close()
 except Exception as e:
 logging .error(e)
 send_telegram_message(
 CHANNEL_ID,
 f"🔗 <b>CODE DE PARRAINAGE</b>\n\nDe : {display}\nSite : {site}\nBonus : <b>+{montant}€</b>\nCode : <code>{code}</code>",
 )
 send_telegram_message(chat_id, f"✅ Parrainage publié : {site} | {code}")
 else:
 send_telegram_message(chat_id, "Format : /parrainage Site 20 CODE")
 return jsonify(success=True)

 return jsonify(success=True)


@app.route("/notify/daily", methods=["POST"])
def notify_daily():
 data = request.json or {}
 if int(data.get("admin_id") or 0) != ADMIN_ID:
 return jsonify({"error": "unauthorized"}), 403
 send_telegram_message(ADMIN_ID, "Cron daily reçu ✅")
 return jsonify({"ok": True})


if __name__ == "__main__":
 port = int(os.environ.get("PORT", 5000))
 app.run(host="0.0.0.0", port=port)
