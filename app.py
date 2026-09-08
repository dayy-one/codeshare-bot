import os
import re
import sqlite3
import secrets
import smtplib
from email.message import EmailMessage
from functools import wraps
from datetime import datetime

import stripe
from flask import Flask, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DB = os.path.join(DATA, "codia.db")
os.makedirs(DATA, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("CODIA_SECRET", secrets.token_hex(32))

PAYOUT_TO = "contact@cod-ia.fr"
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PK = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
APP_URL = os.environ.get("APP_URL", "http://127.0.0.1:3000").rstrip("/")

LEVEL_ORDER = ["START", "PRO", "ELITE"]

LEVELS = {
    "START": {
        "price": 0,
        "rewards": [
            {"points": 10, "reward": 20},
            {"points": 25, "reward": 50},
            {"points": 50, "reward": 120},
        ],
    },
    "PRO": {
        "price": 0,
        "rewards": [
            {"points": 500, "reward": 1000},
            {"points": 1100, "reward": 2200},
            {"points": 1200, "reward": 2400},
            {"points": 1400, "reward": 2800},
        ],
    },
    "ELITE": {
        "price": 0,
        "rewards": [
            {"points": 10, "reward": 80},
            {"points": 25, "reward": 220},
            {"points": 50, "reward": 500},
        ],
    },
}

TIER_LABELS = ["premier", "deuxième", "troisième", "quatrième", "cinquième"]


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db():
    con = db()
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            code TEXT NOT NULL UNIQUE,
            referred_by INTEGER,
            level TEXT NOT NULL DEFAULT 'START',
            points INTEGER NOT NULL DEFAULT 0,
            claimed INTEGER NOT NULL DEFAULT 0,
            paid INTEGER NOT NULL DEFAULT 0,
            ref_locked INTEGER NOT NULL DEFAULT 0,
            first_name TEXT DEFAULT '',
            last_name TEXT DEFAULT '',
            iban TEXT DEFAULT '',
            photo TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER NOT NULL,
            referred_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            code TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        """
    )
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
    if "tier_index" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN tier_index INTEGER NOT NULL DEFAULT 0")
    if "points_locked" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN points_locked INTEGER NOT NULL DEFAULT 0")
    con.commit()
    con.close()


init_db()


def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def initials(name):
    parts = [p for p in re.split(r"\s+", name or "") if p]
    return ("".join(p[0] for p in parts)[:2].upper() or "C")


def make_code():
    a = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "COD-" + "".join(secrets.choice(a) for _ in range(6))


def is_iban(value):
    s = re.sub(r"\s+", "", str(value or "")).upper()
    return bool(re.match(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}$", s))


def user_level(user):
    return user["level"] if user["level"] in LEVELS else "START"


def tier_index_of(user):
    rewards = LEVELS[user_level(user)]["rewards"]
    try:
        idx = int(user["tier_index"] or 0)
    except (KeyError, TypeError, ValueError):
        idx = 0
    return max(0, min(idx, len(rewards) - 1))


def current_tier(user):
    rewards = LEVELS[user_level(user)]["rewards"]
    return rewards[tier_index_of(user)]


def is_locked(user):
    try:
        locked = int(user["points_locked"] or 0)
    except (KeyError, TypeError, ValueError):
        locked = 0
    tier = current_tier(user)
    return bool(locked or user["points"] >= tier["points"])


def reward_state(user):
    level = user_level(user)
    idx = tier_index_of(user)
    rewards = LEVELS[level]["rewards"]
    tier = rewards[idx]
    locked = is_locked(user)
    nxt = None if locked else tier
    return {
        "current": tier["reward"] if locked else 0,
        "current_points": tier["points"],
        "next": nxt,
        "locked": locked,
        "mustWithdraw": locked,
        "tierIndex": idx,
        "tierNumber": idx + 1,
        "tierLabel": TIER_LABELS[idx] if idx < len(TIER_LABELS) else str(idx + 1),
        "level": level,
    }


def progress_of(user):
    tier = current_tier(user)
    if is_locked(user):
        return 100
    return min(100, round((user["points"] / tier["points"]) * 100)) if tier["points"] else 0


def user_by_id(con, user_id):
    return con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def add_activity(con, user_id, kind, title, description=""):
    con.execute(
        "INSERT INTO activities (user_id, kind, title, description, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, kind, title, description, now()),
    )


def ensure_admin():
    con = db()
    email = "contact@cod-ia.fr"
    existing = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        con.execute("UPDATE users SET paid=1, ref_locked=1 WHERE email=?", (email,))
        con.commit()
        con.close()
        return
    con.execute(
        """INSERT INTO users
           (name,username,email,password_hash,code,referred_by,level,points,claimed,paid,ref_locked,created_at)
           VALUES (?,?,?,?,?,NULL,'ELITE',0,0,1,1,?)""",
        (
            "Admin COD-IA",
            "admin",
            email,
            generate_password_hash("CodiaAdmin2026!"),
            "COD-ADMIN1",
            now(),
        ),
    )
    uid = con.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
    add_activity(con, uid, "info", "Compte admin créé", "Accès direct")
    con.commit()
    con.close()


ensure_admin()


def public_user(con, user):
    refs = con.execute(
        "SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='validated'",
        (user["id"],),
    ).fetchone()["c"]
    return {
        "id": user["id"], "name": user["name"], "username": user["username"],
        "initials": initials(user["name"]), "level": user_level(user), "points": user["points"],
        "photo": user["photo"] or "", "refs": refs,
    }


def dashboard(con, user):
    validated = con.execute(
        "SELECT COUNT(*) c FROM referrals WHERE referrer_id=? AND status='validated'",
        (user["id"],),
    ).fetchone()["c"]
    higher = con.execute("SELECT COUNT(*) c FROM users WHERE paid=1 AND points > ?", (user["points"],)).fetchone()["c"]
    reward = reward_state(user)
    return {
        "user": {
            "id": user["id"], "name": user["name"], "username": user["username"],
            "initials": initials(user["name"]), "level": user_level(user), "points": user["points"],
            "code": user["code"], "photo": user["photo"] or "",
            "firstName": user["first_name"] or "", "lastName": user["last_name"] or "",
            "iban": user["iban"] or "", "claimed": user["claimed"] or 0,
            "paid": bool(user["paid"]), "refLocked": bool(user["ref_locked"]),
            "pointsLocked": reward["locked"], "tierIndex": reward["tierIndex"],
        },
        "referrals": {"validated": validated},
        "ranking": {"position": higher + 1 if user["paid"] else None},
        "reward": reward,
        "wallet": {"available": reward["current"]},
        "progress": progress_of(user),
    }


def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    con = db()
    user = user_by_id(con, uid)
    con.close()
    return user


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"message": "Non connecté"}), 401
        return fn(user, *args, **kwargs)
    return wrapper


def require_paid(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = current_user()
        if not user:
            return jsonify({"message": "Non connecté"}), 401
        if not user["paid"]:
            return jsonify({"message": "Paiement requis", "needPay": True}), 402
        return fn(user, *args, **kwargs)
    return wrapper


def grant_point(con, referrer_id, referred_name=""):
    referrer = user_by_id(con, referrer_id)
    if not referrer:
        return False
    if is_locked(referrer):
        add_activity(
            con,
            referrer_id,
            "info",
            "Parrainage non comptabilisé",
            "Palier atteint : retire ta récompense pour que les points comptent à nouveau.",
        )
        return False
    tier = current_tier(referrer)
    new_points = referrer["points"] + 1
    locked = 1 if new_points >= tier["points"] else 0
    con.execute(
        "UPDATE users SET points=?, points_locked=? WHERE id=?",
        (new_points, locked, referrer_id),
    )
    add_activity(con, referrer_id, "point", "Parrainage validé", referred_name)
    if locked:
        label = TIER_LABELS[tier_index_of(referrer)] if tier_index_of(referrer) < len(TIER_LABELS) else str(tier_index_of(referrer) + 1)
        add_activity(
            con,
            referrer_id,
            "palier",
            f"Vous avez franchi un {label} palier",
            "Retrait obligatoire. Les points sont bloqués tant que la récompense n’est pas retirée.",
        )
    return True


def validate_referral(con, user):
    if not user["paid"] or not user["referred_by"]:
        return False
    ref = con.execute("SELECT * FROM referrals WHERE referred_id=?", (user["id"],)).fetchone()
    if not ref or ref["status"] == "validated":
        return False
    con.execute("UPDATE referrals SET status='validated' WHERE id=?", (ref["id"],))
    grant_point(con, ref["referrer_id"], user["name"])
    return True


def mark_paid_user(con, user):
    if not user or user["paid"]:
        return user
    con.execute("UPDATE users SET paid=1 WHERE id=?", (user["id"],))
    add_activity(con, user["id"], "info", "Entrée payée", "Stripe")
    con.commit()
    user = user_by_id(con, user["id"])
    validate_referral(con, user)
    con.commit()
    return user_by_id(con, user["id"])


@app.get("/")
def landing():
    path = os.path.join(BASE, "landing.html")
    if os.path.exists(path):
        return send_from_directory(BASE, "landing.html")
    return send_from_directory(BASE, "index.html")


@app.get("/app")
def spa():
    return send_from_directory(BASE, "index.html")


@app.get("/pay/success")
def pay_success_page():
    return send_from_directory(BASE, "index.html")


@app.get("/api/config")
def config():
    return jsonify({"levels": LEVELS, "brand": "COD-IA", "payoutHours": 48, "accessPrice": 9.99})


@app.post("/api/auth/register")
def register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "Membre").strip()
    username = (data.get("username") or "").strip().lower()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if len(username) < 3 or "@" not in email or len(password) < 8:
        return jsonify({"message": "Champs invalides"}), 400
    con = db()
    try:
        code = make_code()
        while con.execute("SELECT id FROM users WHERE code=?", (code,)).fetchone():
            code = make_code()
        cur = con.execute(
            """INSERT INTO users (name,username,email,password_hash,code,referred_by,level,points,claimed,paid,ref_locked,created_at)
               VALUES (?,?,?,?,?,NULL,'START',0,0,0,0,?)""",
            (name, username, email, generate_password_hash(password), code, now()),
        )
        uid = cur.lastrowid
        add_activity(con, uid, "info", "Compte créé", "En attente de paiement")
        add_activity(con, uid, "rules", "Règles du Parrainage", "Appuie pour lire les règles")
        con.commit()
        user = user_by_id(con, uid)
        session["uid"] = uid
        return jsonify({"dashboard": dashboard(con, user), "needPay": True})
    except sqlite3.IntegrityError:
        return jsonify({"message": "Email ou nom d’utilisateur déjà utilisé"}), 400
    finally:
        con.close()


@app.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip().lower()
    password = data.get("password") or ""
    con = db()
    user = con.execute("SELECT * FROM users WHERE email=? OR username=?", (identifier, identifier)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        con.close()
        return jsonify({"message": "Identifiants incorrects"}), 400
    session["uid"] = user["id"]
    payload = dashboard(con, user)
    con.close()
    return jsonify({"dashboard": payload, "needPay": not bool(user["paid"])})


@app.post("/api/auth/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/auth/me")
def me():
    user = current_user()
    if not user:
        return jsonify({"authenticated": False, "paid": False})
    con = db()
    payload = dashboard(con, user)
    con.close()
    return jsonify({"authenticated": True, "paid": bool(user["paid"]), "dashboard": payload, "needPay": not bool(user["paid"])})


@app.get("/api/pay/config")
def pay_config():
    return jsonify({"pk": STRIPE_PK})


@app.post("/api/pay/session")
@require_auth
def pay_session(user):
    if user["paid"]:
        return jsonify({"paid": True})
    if not stripe.api_key or not STRIPE_PK:
        return jsonify({"message": "Stripe non configuré"}), 500
    session_obj = stripe.checkout.Session.create(
        ui_mode="embedded_page",
        mode="payment",
        customer_email=user["email"],
        client_reference_id=str(user["id"]),
        line_items=[{
            "price_data": {
                "currency": "eur",
                "unit_amount": 999,
                "product_data": {"name": "Accès COD-IA"},
            },
            "quantity": 1,
        }],
        return_url=APP_URL + "/pay/success?session_id={CHECKOUT_SESSION_ID}",
    )
    return jsonify({"clientSecret": session_obj.client_secret, "paid": False})


@app.get("/api/pay/confirm")
@require_auth
def pay_confirm(user):
    session_id = request.args.get("session_id") or ""
    con = db()
    user = user_by_id(con, user["id"])
    if session_id and stripe.api_key:
        try:
            s = stripe.checkout.Session.retrieve(session_id)
            email = ((s.get("customer_details") or {}).get("email") or s.get("customer_email") or "").lower()
            same_user = str(s.get("client_reference_id") or "") == str(user["id"])
            same_email = email == (user["email"] or "").lower()
            if s.get("payment_status") == "paid" and (same_user or same_email):
                user = mark_paid_user(con, user)
        except Exception:
            pass
    payload = dashboard(con, user)
    paid = bool(user["paid"])
    con.close()
    return jsonify({"ok": True, "paid": paid, "dashboard": payload})


@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return jsonify({"message": "Webhook invalide"}), 400
    if event["type"] == "checkout.session.completed":
        s = event["data"]["object"]
        con = db()
        user = None
        uid = s.get("client_reference_id")
        if uid:
            try:
                user = user_by_id(con, int(uid))
            except Exception:
                user = None
        if not user:
            email = ((s.get("customer_details") or {}).get("email") or s.get("customer_email") or "").lower()
            if email:
                user = con.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user:
            mark_paid_user(con, user)
        con.close()
    return jsonify({"ok": True})


@app.post("/api/referral/attach")
@require_auth
def attach_referral(user):
    if not user["paid"]:
        return jsonify({"message": "Paiement requis"}), 402
    if user["ref_locked"]:
        return jsonify({"message": "Code déjà enregistré", "counted": False})
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    if not code:
        con = db()
        con.execute("UPDATE users SET ref_locked=1 WHERE id=?", (user["id"],))
        con.commit()
        payload = dashboard(con, user_by_id(con, user["id"]))
        con.close()
        return jsonify({"ok": True, "counted": False, "dashboard": payload})
    con = db()
    sponsor = con.execute("SELECT * FROM users WHERE code=?", (code,)).fetchone()
    if not sponsor or sponsor["id"] == user["id"]:
        con.close()
        return jsonify({"message": "Code parrain invalide"}), 400
    existing = con.execute("SELECT id FROM referrals WHERE referred_id=?", (user["id"],)).fetchone()
    if not existing:
        con.execute(
            "INSERT INTO referrals (referrer_id, referred_id, status, created_at) VALUES (?, ?, 'pending', ?)",
            (sponsor["id"], user["id"], now()),
        )
        con.execute("UPDATE users SET referred_by=? WHERE id=?", (sponsor["id"], user["id"]))
    con.execute("UPDATE users SET ref_locked=1 WHERE id=?", (user["id"],))
    user = user_by_id(con, user["id"])
    counted = validate_referral(con, user)
    con.commit()
    payload = dashboard(con, user_by_id(con, user["id"]))
    con.close()
    return jsonify({"ok": True, "counted": counted, "dashboard": payload})


@app.get("/api/activity")
@require_paid
def activity(user):
    con = db()
    has_rules = con.execute(
        "SELECT id FROM activities WHERE user_id=? AND kind='rules'",
        (user["id"],),
    ).fetchone()
    if not has_rules:
        add_activity(con, user["id"], "rules", "Règles du Parrainage", "Appuie pour lire les règles")
        con.commit()
    rows = con.execute(
        "SELECT kind, title, description FROM activities WHERE user_id=? ORDER BY id DESC LIMIT 20",
        (user["id"],),
    ).fetchall()
    con.close()
    items = [dict(r) for r in rows]
    items.sort(key=lambda a: 0 if a.get("kind") == "rules" else 1)
    return jsonify({"activities": items})


@app.get("/api/members")
@require_paid
def members(user):
    q = f"%{(request.args.get('q') or '').strip()}%"
    con = db()
    rows = con.execute(
        "SELECT * FROM users WHERE paid=1 AND (name LIKE ? OR username LIKE ?) ORDER BY points DESC, id ASC LIMIT 50",
        (q, q),
    ).fetchall()
    out = [public_user(con, r) for r in rows]
    con.close()
    return jsonify({"members": out})


@app.get("/api/ranking")
@require_paid
def ranking(user):
    con = db()
    rows = con.execute("SELECT * FROM users WHERE paid=1 ORDER BY points DESC, id ASC LIMIT 50").fetchall()
    ranking_list = []
    for i, r in enumerate(rows, start=1):
        item = public_user(con, r)
        item["position"] = i
        ranking_list.append(item)
    higher = con.execute("SELECT COUNT(*) c FROM users WHERE paid=1 AND points > ?", (user["points"],)).fetchone()["c"]
    con.close()
    return jsonify({"me": {"position": higher + 1, "points": user["points"]}, "ranking": ranking_list})


@app.get("/api/posts")
@require_paid
def list_posts(user):
    con = db()
    rows = con.execute(
        """SELECT p.id, p.user_id, p.type, p.code, p.description, p.created_at, u.name
           FROM posts p JOIN users u ON u.id=p.user_id ORDER BY p.id DESC LIMIT 100"""
    ).fetchall()
    con.close()
    return jsonify({"posts": [{"id": r["id"], "userId": r["user_id"], "type": r["type"], "code": r["code"], "desc": r["description"], "by": r["name"], "at": r["created_at"]} for r in rows]})


@app.post("/api/posts")
@require_paid
def create_post(user):
    data = request.get_json(silent=True) or {}
    ptype, code, desc = data.get("type"), (data.get("code") or "").strip().upper(), (data.get("desc") or "").strip()
    if ptype not in ("promo", "parrainage") or len(code) < 4 or len(desc) < 8:
        return jsonify({"message": "Publication invalide"}), 400
    con = db()
    con.execute("INSERT INTO posts (user_id, type, code, description, created_at) VALUES (?, ?, ?, ?, ?)", (user["id"], ptype, code, desc, now()))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.delete("/api/posts/<int:post_id>")
@require_paid
def delete_post(user, post_id):
    con = db()
    row = con.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
    if not row or row["user_id"] != user["id"]:
        con.close()
        return jsonify({"message": "Impossible de supprimer"}), 403
    con.execute("DELETE FROM posts WHERE id=?", (post_id,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.post("/api/profile")
@require_paid
def update_profile(user):
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or user["username"]).strip().lower()
    photo = data.get("photo") if data.get("photo") is not None else user["photo"]
    if len(username) < 3:
        return jsonify({"message": "Username trop court"}), 400
    con = db()
    try:
        con.execute("UPDATE users SET username=?, photo=? WHERE id=?", (username, photo or "", user["id"]))
        con.commit()
        return jsonify({"dashboard": dashboard(con, user_by_id(con, user["id"]))})
    except sqlite3.IntegrityError:
        return jsonify({"message": "Username déjà utilisé"}), 400
    finally:
        con.close()


@app.post("/api/profile/bank")
@require_paid
def update_bank(user):
    data = request.get_json(silent=True) or {}
    first, last, iban = (data.get("firstName") or "").strip(), (data.get("lastName") or "").strip(), re.sub(r"\s+", "", data.get("iban") or "").upper()
    if len(first) < 2 or len(last) < 2 or not is_iban(iban):
        return jsonify({"message": "Coordonnées invalides"}), 400
    con = db()
    con.execute("UPDATE users SET first_name=?, last_name=?, iban=?, name=? WHERE id=?", (first, last, iban, f"{first} {last}", user["id"]))
    con.commit()
    payload = dashboard(con, user_by_id(con, user["id"]))
    con.close()
    return jsonify({"dashboard": payload})


@app.post("/api/payout")
@require_paid
def payout(user):
    state = reward_state(user)
    amount = state["current"]
    first = (user["first_name"] or "").strip()
    last = (user["last_name"] or "").strip()
    iban = re.sub(r"\s+", "", user["iban"] or "").upper()
    if amount <= 0 or not state["locked"]:
        return jsonify({"message": "Aucune récompense à retirer. Un palier doit d’abord être atteint."}), 400
    if len(first) < 2 or len(last) < 2 or not is_iban(iban):
        return jsonify({"message": "Coordonnées bancaires manquantes"}), 400
    if not SMTP_USER or not SMTP_PASS:
        return jsonify({"message": "SMTP non configuré"}), 500
    msg = EmailMessage()
    msg["Subject"] = f"Demande de retrait COD-IA — {first} {last}"
    msg["From"] = SMTP_USER
    msg["To"] = PAYOUT_TO
    msg.set_content(
        f"Prénom : {first}\nNom : {last}\nIBAN : {iban}\nRécompense : {amount} €\n"
        f"Points du palier : {user['points']}\nNiveau : {user_level(user)}\n"
        f"Palier : {state['tierNumber']}\nCode : {user['code']}\nDélai : 48h\n"
    )
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    except Exception:
        return jsonify({"message": "Envoi mail impossible"}), 500

    level = user_level(user)
    idx = tier_index_of(user) + 1
    rewards = LEVELS[level]["rewards"]
    unlocked = None
    if idx >= len(rewards):
        li = LEVEL_ORDER.index(level)
        if li < len(LEVEL_ORDER) - 1:
            level = LEVEL_ORDER[li + 1]
            idx = 0
            unlocked = level
        else:
            idx = len(rewards) - 1

    con = db()
    con.execute(
        "UPDATE users SET points=0, points_locked=0, claimed=?, tier_index=?, level=? WHERE id=?",
        ((user["claimed"] or 0) + amount, idx, level, user["id"]),
    )
    con.execute(
        "INSERT INTO payouts (user_id, amount, status, created_at) VALUES (?, ?, 'pending', ?)",
        (user["id"], amount, now()),
    )
    add_activity(con, user["id"], "money", "Retrait demandé", f"{amount} € · points remis à 0 · 48h")
    if unlocked:
        add_activity(con, user["id"], "info", "Niveau débloqué", f"Tu passes au niveau {unlocked}")
    con.commit()
    payload = dashboard(con, user_by_id(con, user["id"]))
    con.close()
    return jsonify({"ok": True, "dashboard": payload})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 3000)), debug=True)
