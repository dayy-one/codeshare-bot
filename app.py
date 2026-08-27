import os
import logging
import secrets
from datetime import timedelta

import psycopg2
import psycopg2.extras
import stripe

from flask import (
    Flask,
    jsonify,
    request,
    session,
    send_from_directory,
)

from werkzeug.security import (
    generate_password_hash,
    check_password_hash,
)


logging.basicConfig(level=logging.INFO)

app = Flask(__name__, static_folder=".", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
app.permanent_session_lifetime = timedelta(days=90)

DATABASE_URL = os.environ.get("DATABASE_URL")
SERVER_URL = os.environ.get(
    "SERVER_URL",
    "https://cod-ia.fr"
).rstrip("/")

PRICE_CENTS = int(
    os.environ.get("PRICE_CENTS", "999")
)

ADMIN_EMAILS = [
    e.strip().lower()
    for e in os.environ.get(
        "ADMIN_EMAILS",
        "contact@cod-ia.fr"
    ).split(",")
    if e.strip()
]

ADMIN_IDS = [
    x.strip()
    for x in os.environ.get(
        "ADMIN_IDS",
        "8091031583,6886937051"
    ).split(",")
    if x.strip()
]

stripe.api_key = os.environ.get(
    "STRIPE_SECRET_KEY"
)

STRIPE_WEBHOOK_SECRET = os.environ.get(
    "STRIPE_WEBHOOK_SECRET"
)

CHALLENGE_DAYS = 21

BRONZE_TARGET = 500
BRONZE_REWARD = 500

SILVER_TARGET = 1000
SILVER_REWARD = 1000

GOLD_TARGET = 1500
GOLD_REWARD = 1500


# ============================================================
# DATABASE
# ============================================================

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL manquant"
        )

    return psycopg2.connect(
        DATABASE_URL
    )


def session_user_id():
    return session.get("user_id")


def get_user_by_id(uid):
    if not uid:
        return None

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        "SELECT * FROM users WHERE id=%s",
        (uid,)
    )

    user = cur.fetchone()

    cur.close()
    conn.close()

    return user


def is_admin_email(email):
    return bool(email) and (
        str(email).lower()
        in ADMIN_EMAILS
    )


def is_admin_id(uid):
    return (
        uid is not None
        and str(uid) in ADMIN_IDS
    )


def is_admin_user(user):
    if not user:
        return False

    return (
        is_admin_email(user.get("email"))
        or is_admin_id(user.get("id"))
        or user.get("role") == "admin"
    )


def is_admin_current():
    uid = session_user_id()

    if not uid:
        return False

    return is_admin_user(
        get_user_by_id(uid)
    )


def is_paid(uid):
    if not uid:
        return False

    user = get_user_by_id(uid)

    if is_admin_user(user):
        return True

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        SELECT 1
        FROM paid_users
        WHERE user_id=%s
        LIMIT 1
        """,
        (uid,)
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    return bool(row)


def user_public(
    user,
    paid=None,
    admin=None
):
    if not user:
        return None

    uid = user["id"]

    if admin is None:
        admin = is_admin_user(user)

    if paid is None:
        paid = True if admin else is_paid(uid)

    return {
        "id": uid,
        "username": user.get("username"),
        "email": user.get("email"),
        "points": user.get("points") or 0,
        "role":
            "admin"
            if admin
            else user.get("role") or "user",
        "paid": paid,
        "is_admin": admin,
    }


# ============================================================
# DATABASE INIT
# ============================================================

def init_db():

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT,
            email TEXT UNIQUE,
            password TEXT,
            telegram_id TEXT,
            points INTEGER DEFAULT 0,
            role TEXT DEFAULT 'user',
            bio TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS paid_users (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            telegram_id TEXT,
            paid_at TIMESTAMP DEFAULT NOW()
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS codes (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            type TEXT,
            site TEXT,
            title TEXT,
            code TEXT,
            description TEXT,
            url TEXT,
            expires_at DATE,
            added_by TEXT,
            likes INTEGER DEFAULT 0,
            copies INTEGER DEFAULT 0,
            clicks INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            deleted BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS code_saves (
            user_id INTEGER,
            code_id INTEGER,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, code_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS code_reacts (
            user_id INTEGER,
            code_id INTEGER,
            reaction TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, code_id, reaction)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id SERIAL PRIMARY KEY,
            user_id INTEGER UNIQUE,
            code TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referral_uses (
            id SERIAL PRIMARY KEY,
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            created_at TIMESTAMP DEFAULT NOW(),
            status TEXT DEFAULT 'pending',
            validated_at TIMESTAMP,
            rejected_at TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS challenges (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            start_date TIMESTAMP NOT NULL,
            end_date TIMESTAMP NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT NOW(),
            finished_at TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS challenge_rewards (
            id SERIAL PRIMARY KEY,
            challenge_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            rank INTEGER,
            referrals INTEGER DEFAULT 0,
            tier TEXT,
            reward INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(challenge_id, user_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS support_messages (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            user_name TEXT,
            message TEXT,
            admin_reply TEXT,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )

    alter_statements = [

        "ALTER TABLE users ADD COLUMN IF NOT EXISTS points INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user'",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT",

        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'approved'",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS clicks INTEGER DEFAULT 0",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS title TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS copies INTEGER DEFAULT 0",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS likes INTEGER DEFAULT 0",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS deleted BOOLEAN DEFAULT FALSE",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS url TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS expires_at DATE",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS added_by TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS user_id INTEGER",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS type TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS site TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS code TEXT",
        "ALTER TABLE codes ADD COLUMN IF NOT EXISTS description TEXT",

        "ALTER TABLE referral_uses ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending'",
        "ALTER TABLE referral_uses ADD COLUMN IF NOT EXISTS validated_at TIMESTAMP",
        "ALTER TABLE referral_uses ADD COLUMN IF NOT EXISTS rejected_at TIMESTAMP",
    ]

    for stmt in alter_statements:
        try:
            cur.execute(stmt)
        except Exception as e:
            logging.warning(
                "ALTER ignoré: %s",
                e
            )
            conn.rollback()

    cur.execute(
        """
        UPDATE users
        SET role='admin'
        WHERE lower(email)=ANY(%s)
        """,
        (ADMIN_EMAILS,)
    )

    conn.commit()

    cur.close()
    conn.close()


# ============================================================
# CHALLENGE
# ============================================================

def get_or_create_challenge():

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM challenges
        ORDER BY id DESC
        LIMIT 1
        """
    )

    challenge = cur.fetchone()

    if challenge:

        cur.close()
        conn.close()

        return challenge

    cur.execute(
        """
        INSERT INTO challenges
        (
            name,
            start_date,
            end_date,
            status
        )
        VALUES
        (
            %s,
            NOW(),
            NOW() + INTERVAL '21 days',
            'active'
        )
        RETURNING *
        """,
        ("Challenge COD.IA — 21 jours",)
    )

    challenge = cur.fetchone()

    conn.commit()

    cur.close()
    conn.close()

    return challenge


def reward_for_referrals(count):

    count = int(count or 0)

    if count >= GOLD_TARGET:
        return "OR", GOLD_REWARD

    if count >= SILVER_TARGET:
        return "ARGENT", SILVER_REWARD

    if count >= BRONZE_TARGET:
        return "BRONZE", BRONZE_REWARD

    return None, 0


def finish_challenge_if_needed():

    challenge = get_or_create_challenge()

    if challenge["status"] == "finished":
        return challenge

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT NOW() >= %s AS finished
        """,
        (challenge["end_date"],)
    )

    finished = cur.fetchone()["finished"]

    if not finished:

        cur.close()
        conn.close()

        return challenge

    cur.execute(
        """
        SELECT
            u.id AS user_id,
            COUNT(r.id) AS referrals
        FROM users u
        LEFT JOIN referral_uses r
            ON r.referrer_id=u.id
            AND r.status='validated'
        GROUP BY u.id
        ORDER BY COUNT(r.id) DESC, u.id ASC
        """
    )

    rows = cur.fetchall()

    rank = 0

    for row in rows:

        rank += 1

        tier, reward = reward_for_referrals(
            row["referrals"]
        )

        cur.execute(
            """
            INSERT INTO challenge_rewards
            (
                challenge_id,
                user_id,
                rank,
                referrals,
                tier,
                reward
            )
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT
            (
                challenge_id,
                user_id
            )
            DO UPDATE SET
                rank=EXCLUDED.rank,
                referrals=EXCLUDED.referrals,
                tier=EXCLUDED.tier,
                reward=EXCLUDED.reward
            """,
            (
                challenge["id"],
                row["user_id"],
                rank,
                row["referrals"],
                tier,
                reward,
            )
        )

    cur.execute(
        """
        UPDATE challenges
        SET
            status='finished',
            finished_at=NOW()
        WHERE id=%s
        """,
        (challenge["id"],)
    )

    conn.commit()

    cur.execute(
        """
        SELECT *
        FROM challenges
        WHERE id=%s
        """,
        (challenge["id"],)
    )

    challenge = cur.fetchone()

    cur.close()
    conn.close()

    return challenge


def challenge_leaderboard(challenge_id):

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT
            u.id,
            COALESCE(u.username,'Membre') AS name,
            COUNT(r.id) AS referrals
        FROM users u
        LEFT JOIN referral_uses r
            ON r.referrer_id=u.id
            AND r.status='validated'
        GROUP BY u.id, u.username
        HAVING COUNT(r.id) > 0
        ORDER BY COUNT(r.id) DESC, u.id ASC
        LIMIT 100
        """
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    output = []

    for index, row in enumerate(rows, start=1):

        tier, reward = reward_for_referrals(
            row["referrals"]
        )

        output.append({
            "rank": index,
            "id": row["id"],
            "name": row["name"],
            "referrals": int(
                row["referrals"] or 0
            ),
            "tier": tier,
            "reward": reward,
        })

    return output


def challenge_current_user(uid):

    if not uid:
        return None

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT
            COUNT(*) AS referrals
        FROM referral_uses
        WHERE referrer_id=%s
        AND status='validated'
        """,
        (uid,)
    )

    row = cur.fetchone()

    referrals = int(
        row["referrals"] or 0
    )

    cur.execute(
        """
        SELECT COUNT(*) + 1 AS rank
        FROM
        (
            SELECT
                referrer_id
            FROM referral_uses
            WHERE status='validated'
            GROUP BY referrer_id
            HAVING COUNT(*) > %s
        ) x
        """,
        (referrals,)
    )

    rank = cur.fetchone()["rank"]

    cur.close()
    conn.close()

    tier, reward = reward_for_referrals(
        referrals
    )

    return {
        "referrals": referrals,
        "rank": rank,
        "tier": tier,
        "reward": reward,
    }


# ============================================================
# ROUTES FRONT
# ============================================================

@app.route("/")
def home():
    return send_from_directory(
        ".",
        "landing.html"
    )


@app.route("/app")
@app.route("/miniapp")
def miniapp():
    return send_from_directory(
        ".",
        "miniapp.html"
    )


@app.route("/config")
def config():
    return jsonify(
        stripe_pk=os.environ.get(
            "STRIPE_PUBLISHABLE_KEY",
            ""
        )
    )


@app.route("/me")
def me():

    uid = session_user_id()

    user = get_user_by_id(uid)

    if not user:
        return jsonify(
            user=None,
            paid=False,
            is_admin=False
        )

    admin = is_admin_user(user)

    paid = (
        True
        if admin
        else is_paid(uid)
    )

    return jsonify(
        user=user_public(
            user,
            paid=paid,
            admin=admin
        ),
        paid=paid,
        is_admin=admin
    )


@app.route("/access")
def access():

    uid = session_user_id()

    return jsonify(
        ok=is_paid(uid)
    )


# ============================================================
# AUTH
# ============================================================

@app.route(
    "/auth/register",
    methods=["POST"]
)
def auth_register():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    email = (
        data.get("email")
        or ""
    ).strip().lower()

    username = (
        data.get("username")
        or ""
    ).strip()

    password = (
        data.get("password")
        or ""
    )

    referral_code = (
        data.get("referral")
        or ""
    ).strip().upper()

    if not email or not username or not password:

        return jsonify(
            success=False,
            error=
            "Tous les champs sont obligatoires."
        ), 400

    if len(password) < 8:

        return jsonify(
            success=False,
            error=
            "Mot de passe : 8 caractères minimum."
        ), 400

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT id
        FROM users
        WHERE lower(email)=%s
        """,
        (email,)
    )

    if cur.fetchone():

        cur.close()
        conn.close()

        return jsonify(
            success=False,
            error=
            "Cet email est déjà utilisé."
        ), 409

    cur.execute(
        """
        SELECT id
        FROM users
        WHERE lower(username)=%s
        """,
        (username.lower(),)
    )

    if cur.fetchone():

        cur.close()
        conn.close()

        return jsonify(
            success=False,
            error=
            "Ce username est déjà utilisé."
        ), 409

    role = (
        "admin"
        if email in ADMIN_EMAILS
        else "user"
    )

    cur.execute(
        """
        INSERT INTO users
        (
            username,
            email,
            password,
            points,
            role
        )
        VALUES (%s,%s,%s,%s,%s)
        RETURNING *
        """,
        (
            username,
            email,
            generate_password_hash(password),
            50,
            role,
        )
    )

    user = cur.fetchone()

    # Parrainage transmis pendant l'inscription.
    if referral_code:

        cur.execute(
            """
            SELECT user_id
            FROM referrals
            WHERE upper(code)=%s
            """,
            (referral_code,)
        )

        referral = cur.fetchone()

        if (
            referral
            and str(referral["user_id"])
            != str(user["id"])
        ):

            cur.execute(
                """
                INSERT INTO referral_uses
                (
                    referrer_id,
                    referred_id,
                    status
                )
                VALUES (%s,%s,'pending')
                ON CONFLICT (referred_id)
                DO NOTHING
                """,
                (
                    referral["user_id"],
                    user["id"],
                )
            )

    conn.commit()

    cur.close()
    conn.close()

    session.permanent = True
    session["user_id"] = user["id"]

    return jsonify(
        success=True,
        user=user_public(user)
    )


@app.route(
    "/auth/login",
    methods=["POST"]
)
def auth_login():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    login = (
        data.get("login")
        or data.get("email")
        or ""
    ).strip()

    password = (
        data.get("password")
        or ""
    )

    if not login or not password:

        return jsonify(
            success=False,
            error=
            "Identifiant et mot de passe requis."
        ), 400

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM users
        WHERE lower(email)=%s
        OR lower(username)=%s
        """,
        (
            login.lower(),
            login.lower()
        )
    )

    user = cur.fetchone()

    cur.close()
    conn.close()

    if (
        not user
        or not user.get("password")
        or not check_password_hash(
            user["password"],
            password
        )
    ):

        return jsonify(
            success=False,
            error=
            "Email ou mot de passe incorrect."
        ), 401

    if is_admin_email(
        user.get("email")
    ):

        conn = get_conn()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE users
            SET role='admin'
            WHERE id=%s
            """,
            (user["id"],)
        )

        conn.commit()

        cur.close()
        conn.close()

        user["role"] = "admin"

    session.permanent = True
    session["user_id"] = user["id"]

    return jsonify(
        success=True,
        user=user_public(user)
    )


@app.route(
    "/auth/logout",
    methods=["POST"]
)
def auth_logout():

    session.clear()

    return jsonify(
        success=True
    )


# ============================================================
# STRIPE
# ============================================================

@app.route(
    "/create-embedded-checkout",
    methods=["POST"]
)
def create_embedded_checkout():

    uid = session_user_id()

    if not uid:

        return jsonify(
            error="Connecte-toi d’abord"
        ), 401

    if not stripe.api_key:

        return jsonify(
            error="Stripe non configuré"
        ), 500

    checkout = stripe.checkout.Session.create(

        mode="payment",

        ui_mode="embedded",

        return_url=
            SERVER_URL +
            "/app?paid=1",

        line_items=[
            {
                "price_data": {
                    "currency": "eur",
                    "product_data": {
                        "name":
                            "Accès COD.IA"
                    },
                    "unit_amount":
                        PRICE_CENTS,
                },
                "quantity": 1,
            }
        ],

        metadata={
            "user_id": str(uid)
        }
    )

    return jsonify(
        clientSecret=
            checkout.client_secret
    )


@app.route(
    "/stripe/webhook",
    methods=["POST"]
)
def stripe_webhook():

    payload = request.get_data()

    sig = request.headers.get(
        "Stripe-Signature"
    )

    try:

        if STRIPE_WEBHOOK_SECRET:

            event = stripe.Webhook.construct_event(
                payload,
                sig,
                STRIPE_WEBHOOK_SECRET
            )

        else:

            event = request.get_json()

    except Exception as e:

        logging.error(
            "Stripe webhook: %s",
            e
        )

        return jsonify(
            error="invalid"
        ), 400

    event_type = (
        event.get("type")
        if isinstance(event, dict)
        else getattr(
            event,
            "type",
            ""
        )
    )

    data = (
        event.get("data", {})
        .get("object", {})
        if isinstance(event, dict)
        else {}
    )

    if event_type in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded"
    ):

        metadata = (
            data.get("metadata")
            or {}
        )

        uid = metadata.get(
            "user_id"
        )

        if uid:

            conn = get_conn()
            cur = conn.cursor()

            cur.execute(
                """
                SELECT 1
                FROM paid_users
                WHERE user_id=%s
                LIMIT 1
                """,
                (uid,)
            )

            if not cur.fetchone():

                cur.execute(
                    """
                    INSERT INTO paid_users
                    (user_id)
                    VALUES (%s)
                    """,
                    (uid,)
                )

            conn.commit()

            cur.close()
            conn.close()

    return jsonify(
        ok=True
    )


# ============================================================
# CODES
# ============================================================

def approved_filter():

    return """
    AND COALESCE(deleted,FALSE)=FALSE
    AND (
        status='approved'
        OR status IS NULL
    )
    """


@app.route("/codes")
def codes_list():

    typ = request.args.get(
        "type"
    )

    expiring = request.args.get(
        "expiring"
    )

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    query = (
        "SELECT * FROM codes "
        "WHERE 1=1 "
        + approved_filter()
    )

    params = []

    if typ in (
        "promo",
        "parrainage"
    ):

        query += """
        AND type=%s
        """

        params.append(typ)

    if expiring:

        query += """
        AND expires_at IS NOT NULL
        AND expires_at <=
        (
            CURRENT_DATE + INTERVAL '7 days'
        )
        """

    query += """
    ORDER BY id DESC
    LIMIT 200
    """

    cur.execute(
        query,
        params
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        codes=rows
    )


@app.route("/codes/top")
def codes_top():

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM codes
        WHERE 1=1
        """
        + approved_filter()
        + """
        ORDER BY
            COALESCE(likes,0) DESC,
            COALESCE(copies,0) DESC
        LIMIT 8
        """
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        codes=rows
    )


@app.route("/codes/search")
def codes_search():

    qtxt = (
        request.args.get("q")
        or ""
    ).strip()

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM codes
        WHERE 1=1
        """
        + approved_filter()
        + """
        AND
        (
            site ILIKE %s
            OR code ILIKE %s
            OR description ILIKE %s
            OR COALESCE(title,'') ILIKE %s
        )
        ORDER BY id DESC
        LIMIT 100
        """,
        (
            f"%{qtxt}%",
            f"%{qtxt}%",
            f"%{qtxt}%",
            f"%{qtxt}%"
        )
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        codes=rows
    )


@app.route(
    "/codes/add",
    methods=["POST"]
)
def codes_add():

    uid = session_user_id()

    if not uid:

        return jsonify(
            success=False,
            error="Connecte-toi"
        ), 401

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    site = (
        data.get("site")
        or data.get("store")
        or ""
    ).strip()

    code = (
        data.get("code")
        or ""
    ).strip()

    typ = (
        data.get("type")
        or "promo"
    ).strip()

    title = (
        data.get("title")
        or ""
    ).strip()

    desc = (
        data.get("description")
        or title
    ).strip()

    url = (
        data.get("url")
        or data.get("link")
        or ""
    ).strip() or None

    expires = (
        data.get("expires_at")
        or None
    )

    user = get_user_by_id(uid)

    added_by = (
        data.get("added_by")
        or user.get("username")
        if user
        else None
    )

    if not site or not code:

        return jsonify(
            success=False,
            error=
            "Site et code obligatoires."
        ), 400

    if typ not in (
        "promo",
        "parrainage"
    ):

        return jsonify(
            success=False,
            error="Type invalide."
        ), 400

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO codes
        (
            user_id,
            type,
            site,
            title,
            code,
            description,
            url,
            expires_at,
            added_by,
            status
        )
        VALUES
        (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,
            'pending'
        )
        RETURNING id
        """,
        (
            uid,
            typ,
            site,
            title,
            code,
            desc,
            url,
            expires,
            added_by
        )
    )

    new_id = cur.fetchone()[0]

    cur.execute(
        """
        UPDATE users
        SET points=
            COALESCE(points,0)+10
        WHERE id=%s
        """,
        (uid,)
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True,
        id=new_id
    )


@app.route("/codes/mine")
def codes_mine():

    uid = (
        request.args.get(
            "user_id"
        )
        or session_user_id()
    )

    if not uid:
        return jsonify(
            codes=[]
        )

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM codes
        WHERE user_id=%s
        AND COALESCE(
            deleted,FALSE
        )=FALSE
        ORDER BY id DESC
        """,
        (uid,)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        codes=rows
    )


@app.route("/codes/user")
def codes_user():

    uid = request.args.get(
        "user_id"
    )

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM codes
        WHERE user_id=%s
        """
        + approved_filter()
        + """
        ORDER BY id DESC
        """,
        (uid,)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        codes=rows
    )


@app.route("/codes/saved")
def codes_saved():

    uid = (
        request.args.get(
            "user_id"
        )
        or session_user_id()
    )

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT c.*
        FROM codes c
        JOIN code_saves s
            ON s.code_id=c.id
        WHERE s.user_id=%s
        AND COALESCE(
            c.deleted,FALSE
        )=FALSE
        AND (
            c.status='approved'
            OR c.status IS NULL
        )
        ORDER BY s.created_at DESC
        """,
        (uid,)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        codes=rows
    )


# ============================================================
# CODE ACTIONS
# ============================================================

@app.route(
    "/code/copy",
    methods=["POST"]
)
def code_copy():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    cid = data.get("id")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE codes
        SET copies=
            COALESCE(copies,0)+1
        WHERE id=%s
        RETURNING copies
        """,
        (cid,)
    )

    row = cur.fetchone()

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True,
        copies=
            row[0]
            if row
            else 0
    )


@app.route(
    "/code/click",
    methods=["POST"]
)
def code_click():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    cid = data.get("id")

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        UPDATE codes
        SET clicks=
            COALESCE(clicks,0)+1
        WHERE id=%s
        RETURNING url, clicks
        """,
        (cid,)
    )

    row = cur.fetchone()

    conn.commit()

    cur.close()
    conn.close()

    if not row:

        return jsonify(
            error="introuvable"
        ), 404

    return jsonify(
        ok=True,
        url=row.get("url"),
        clicks=row.get("clicks") or 0
    )


@app.route(
    "/code/react",
    methods=["POST"]
)
def code_react():

    uid = session_user_id()

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    cid = data.get("id")

    if not uid:

        return jsonify(
            error="Connexion requise"
        ), 401

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO code_reacts
        (
            user_id,
            code_id,
            reaction
        )
        VALUES (%s,%s,%s)
        ON CONFLICT DO NOTHING
        """,
        (
            uid,
            cid,
            "like"
        )
    )

    if cur.rowcount:

        cur.execute(
            """
            UPDATE codes
            SET likes=
                COALESCE(likes,0)+1
            WHERE id=%s
            """,
            (cid,)
        )

    cur.execute(
        """
        SELECT likes
        FROM codes
        WHERE id=%s
        """,
        (cid,)
    )

    row = cur.fetchone()

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True,
        value=
            row[0]
            if row
            else 0
    )


@app.route(
    "/code/save",
    methods=["POST"]
)
def code_save():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    uid = (
        session_user_id()
        or data.get("user_id")
    )

    cid = data.get("id")

    if not uid:
        return jsonify(
            error="Connexion requise"
        ), 401

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO code_saves
        (
            user_id,
            code_id
        )
        VALUES (%s,%s)
        ON CONFLICT DO NOTHING
        """,
        (
            uid,
            cid
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


@app.route(
    "/code/unsave",
    methods=["POST"]
)
def code_unsave():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    uid = (
        session_user_id()
        or data.get("user_id")
    )

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM code_saves
        WHERE user_id=%s
        AND code_id=%s
        """,
        (
            uid,
            data.get("id")
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


@app.route(
    "/code/delete",
    methods=["POST"]
)
def code_delete():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    uid = session_user_id()

    user = get_user_by_id(uid)

    if not user:

        return jsonify(
            error="unauthorized"
        ), 403

    conn = get_conn()
    cur = conn.cursor()

    if is_admin_user(user):

        cur.execute(
            """
            UPDATE codes
            SET deleted=TRUE
            WHERE id=%s
            """,
            (data.get("id"),)
        )

    else:

        cur.execute(
            """
            UPDATE codes
            SET deleted=TRUE
            WHERE id=%s
            AND user_id=%s
            """,
            (
                data.get("id"),
                uid
            )
        )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


@app.route(
    "/code/restore",
    methods=["POST"]
)
def code_restore():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE codes
        SET deleted=FALSE
        WHERE id=%s
        """,
        (data.get("id"),)
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


# ============================================================
# PROFILE
# ============================================================

@app.route(
    "/profile/full_stats"
)
def profile_full_stats():

    uid = (
        request.args.get(
            "user_id"
        )
        or session_user_id()
    )

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT
            username,
            bio,
            points
        FROM users
        WHERE id=%s
        """,
        (uid,)
    )

    user = cur.fetchone() or {}

    cur.execute(
        """
        SELECT
            COUNT(*) AS total_codes,
            COALESCE(
                SUM(likes),0
            ) AS total_likes,
            COALESCE(
                SUM(copies),0
            ) AS total_copies,
            COALESCE(
                SUM(clicks),0
            ) AS total_clicks
        FROM codes
        WHERE user_id=%s
        AND COALESCE(
            deleted,FALSE
        )=FALSE
        """,
        (uid,)
    )

    stats = cur.fetchone() or {}

    cur.close()
    conn.close()

    return jsonify(
        username=user.get(
            "username"
        ),
        bio=user.get("bio") or "",
        points=user.get(
            "points"
        ) or 0,
        total_codes=stats.get(
            "total_codes"
        ) or 0,
        total_likes=stats.get(
            "total_likes"
        ) or 0,
        total_copies=stats.get(
            "total_copies"
        ) or 0,
        total_clicks=stats.get(
            "total_clicks"
        ) or 0,
    )


@app.route(
    "/profile/bio",
    methods=["POST"]
)
def profile_bio():

    uid = session_user_id()

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE users
        SET bio=%s
        WHERE id=%s
        """,
        (
            (
                data.get("bio")
                or ""
            )[:160],
            uid
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


# ============================================================
# REFERRAL
# ============================================================

@app.route(
    "/referral/status"
)
def referral_status():

    uid = (
        request.args.get(
            "user_id"
        )
        or session_user_id()
    )

    if not uid:

        return jsonify(
            my_code=None,
            referrals_count=0,
            has_used=False
        )

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT code
        FROM referrals
        WHERE user_id=%s
        """,
        (uid,)
    )

    row = cur.fetchone()

    cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM referral_uses
        WHERE referrer_id=%s
        AND status='validated'
        """,
        (uid,)
    )

    count = cur.fetchone()["c"]

    cur.execute(
        """
        SELECT 1
        FROM referral_uses
        WHERE referred_id=%s
        LIMIT 1
        """,
        (uid,)
    )

    used = bool(
        cur.fetchone()
    )

    cur.close()
    conn.close()

    return jsonify(
        my_code=
            row["code"]
            if row
            else None,
        referrals_count=count,
        has_used=used
    )


@app.route(
    "/referral/generate",
    methods=["POST"]
)
def referral_generate():

    uid = session_user_id()

    if not uid:

        return jsonify(
            error="login"
        ), 401

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT code
        FROM referrals
        WHERE user_id=%s
        """,
        (uid,)
    )

    existing = cur.fetchone()

    if existing:

        cur.close()
        conn.close()

        return jsonify(
            success=True,
            code=existing["code"]
        )

    code = (
        "CODIA"
        + secrets.token_hex(4).upper()
    )

    cur.execute(
        """
        INSERT INTO referrals
        (
            user_id,
            code
        )
        VALUES (%s,%s)
        RETURNING code
        """,
        (
            uid,
            code
        )
    )

    result = cur.fetchone()

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True,
        code=result["code"]
    )


@app.route(
    "/referral/integrate",
    methods=["POST"]
)
def referral_integrate():

    uid = session_user_id()

    if not uid:

        return jsonify(
            success=False,
            error="login"
        ), 401

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    code = (
        data.get("code")
        or ""
    ).strip().upper()

    if not code:

        return jsonify(
            success=False,
            error="Code inconnu"
        ), 400

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT user_id
        FROM referrals
        WHERE upper(code)=%s
        """,
        (code,)
    )

    ref = cur.fetchone()

    if not ref:

        cur.close()
        conn.close()

        return jsonify(
            success=False,
            error="Code inconnu"
        ), 404

    if str(
        ref["user_id"]
    ) == str(uid):

        cur.close()
        conn.close()

        return jsonify(
            success=False,
            error=
            "Tu ne peux pas utiliser ton propre code"
        ), 400

    cur.execute(
        """
        INSERT INTO referral_uses
        (
            referrer_id,
            referred_id,
            status
        )
        VALUES
        (
            %s,%s,'pending'
        )
        ON CONFLICT
        (referred_id)
        DO NOTHING
        """,
        (
            ref["user_id"],
            uid
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


# ============================================================
# CHALLENGE PUBLIC
# ============================================================

@app.route("/challenge")
def challenge():

    challenge = (
        finish_challenge_if_needed()
    )

    leaderboard = (
        challenge_leaderboard(
            challenge["id"]
        )
    )

    uid = session_user_id()

    me = (
        challenge_current_user(uid)
        if uid
        else None
    )

    return jsonify(

        challenge={
            "id": challenge["id"],
            "name": challenge["name"],
            "start": challenge["start_date"],
            "end": challenge["end_date"],
            "status": challenge["status"],
        },

        rules={
            "bronze": {
                "target": BRONZE_TARGET,
                "reward": BRONZE_REWARD,
            },
            "silver": {
                "target": SILVER_TARGET,
                "reward": SILVER_REWARD,
            },
            "gold": {
                "target": GOLD_TARGET,
                "reward": GOLD_REWARD,
            },
        },

        leaderboard=leaderboard,

        me=me
    )


# ============================================================
# ADMIN
# ============================================================

@app.route("/admin/stats")
def admin_stats():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        "SELECT COUNT(*) AS c FROM users"
    )

    members = cur.fetchone()["c"]

    cur.execute(
        "SELECT COUNT(*) AS c FROM paid_users"
    )

    paid = cur.fetchone()["c"]

    cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM codes
        WHERE COALESCE(
            deleted,FALSE
        )=FALSE
        """
    )

    codes = cur.fetchone()["c"]

    cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM referral_uses
        WHERE status='validated'
        """
    )

    refs = cur.fetchone()["c"]

    cur.execute(
        """
        SELECT COUNT(*) AS c
        FROM codes
        WHERE status='pending'
        AND COALESCE(
            deleted,FALSE
        )=FALSE
        """
    )

    pending = cur.fetchone()["c"]

    cur.execute(
        """
        SELECT
            COALESCE(
                SUM(clicks),0
            ) AS c
        FROM codes
        """
    )

    clicks = cur.fetchone()["c"]

    cur.close()
    conn.close()

    return jsonify(
        total_members=members,
        paid_members=paid,
        total_codes=codes,
        total_referrals=refs,
        pending=pending,
        clicks=clicks
    )


@app.route("/admin/moderation")
def admin_moderation():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT
            c.*,
            COALESCE(
                u.username,
                'Membre'
            ) AS username,
            u.email
        FROM codes c
        LEFT JOIN users u
            ON u.id=c.user_id
        WHERE COALESCE(
            c.deleted,FALSE
        )=FALSE
        ORDER BY c.id DESC
        LIMIT 200
        """
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        codes=rows
    )


@app.route(
    "/admin/code/approve",
    methods=["POST"]
)
def admin_approve():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        UPDATE codes
        SET status='approved'
        WHERE id=%s
        RETURNING user_id
        """,
        (data.get("id"),)
    )

    row = cur.fetchone()

    if row and row[0]:

        cur.execute(
            """
            UPDATE users
            SET points=
                COALESCE(points,0)+25
            WHERE id=%s
            """,
            (row[0],)
        )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


@app.route(
    "/admin/code/reject",
    methods=["POST"]
)
def admin_reject():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        UPDATE codes
        SET status='rejected'
        WHERE id=%s
        """,
        (data.get("id"),)
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


# ============================================================
# ADMIN PARRAINAGES
# ============================================================

@app.route(
    "/admin/referrals"
)
def admin_referrals():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT
            r.id,
            r.status,
            r.created_at,
            r.validated_at,

            COALESCE(
                u1.username,
                'Membre'
            ) AS referrer_name,

            COALESCE(
                u2.username,
                'Membre'
            ) AS referred_name,

            u1.email AS referrer_email,
            u2.email AS referred_email

        FROM referral_uses r

        LEFT JOIN users u1
            ON u1.id=r.referrer_id

        LEFT JOIN users u2
            ON u2.id=r.referred_id

        ORDER BY r.id DESC

        LIMIT 500
        """
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        referrals=rows
    )


@app.route(
    "/admin/referral/approve",
    methods=["POST"]
)
def admin_referral_approve():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    referral_id = data.get(
        "id"
    )

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM referral_uses
        WHERE id=%s
        FOR UPDATE
        """,
        (referral_id,)
    )

    referral = cur.fetchone()

    if not referral:

        conn.rollback()
        cur.close()
        conn.close()

        return jsonify(
            error=
            "Parrainage introuvable"
        ), 404

    if referral["status"] == "validated":

        cur.close()
        conn.close()

        return jsonify(
            success=True,
            message=
            "Déjà validé"
        )

    if referral["status"] == "rejected":

        cur.close()
        conn.close()

        return jsonify(
            error=
            "Parrainage déjà refusé"
        ), 400

    cur.execute(
        """
        UPDATE referral_uses
        SET
            status='validated',
            validated_at=NOW()
        WHERE id=%s
        """,
        (referral_id,)
    )

    # Bonus COD.IA de 100 points
    # à chaque parrainage validé.
    cur.execute(
        """
        UPDATE users
        SET points=
            COALESCE(points,0)+100
        WHERE id=%s
        """,
        (referral["referrer_id"],)
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


@app.route(
    "/admin/referral/reject",
    methods=["POST"]
)
def admin_referral_reject():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        UPDATE referral_uses
        SET
            status='rejected',
            rejected_at=NOW()
        WHERE id=%s
        AND status='pending'
        """,
        (data.get("id"),)
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


# ============================================================
# RÉCOMPENSES DU CHALLENGE
# ============================================================

@app.route(
    "/admin/challenge/rewards"
)
def admin_challenge_rewards():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    challenge = (
        finish_challenge_if_needed()
    )

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT
            cr.*,
            COALESCE(
                u.username,
                'Membre'
            ) AS username,
            u.email
        FROM challenge_rewards cr
        JOIN users u
            ON u.id=cr.user_id
        WHERE cr.challenge_id=%s
        ORDER BY cr.rank ASC
        """,
        (challenge["id"],)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        challenge=challenge,
        rewards=rows
    )


@app.route(
    "/admin/challenge/reward-status",
    methods=["POST"]
)
def admin_reward_status():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    reward_id = data.get(
        "id"
    )

    status = (
        data.get("status")
        or "pending"
    )

    allowed = {
        "pending",
        "approved",
        "paid",
        "cancelled"
    }

    if status not in allowed:

        return jsonify(
            error="Statut invalide"
        ), 400

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        UPDATE challenge_rewards
        SET status=%s
        WHERE id=%s
        """,
        (
            status,
            reward_id
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


# ============================================================
# ANCIEN LEADERBOARD — CONSERVÉ
# ============================================================

@app.route("/leaderboard")
def leaderboard():

    data = challenge()

    return jsonify(
        leaderboard=[
            {
                "name":
                    x["name"],
                "codes_count":
                    x["referrals"]
            }
            for x in data["leaderboard"]
        ]
    )


# ============================================================
# SUPPORT
# ============================================================

@app.route(
    "/support/send",
    methods=["POST"]
)
def support_send():

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    uid = (
        session_user_id()
        or data.get("user_id")
    )

    msg = (
        data.get("message")
        or ""
    ).strip()

    if not msg:

        return jsonify(
            success=False,
            error="Message vide"
        ), 400

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO support_messages
        (
            user_id,
            user_name,
            message
        )
        VALUES (%s,%s,%s)
        """,
        (
            uid,
            data.get(
                "user_name",
                "Membre"
            ),
            msg[:1000]
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


@app.route(
    "/support/list"
)
def support_list():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    status = (
        request.args.get(
            "status"
        )
        or "open"
    )

    conn = get_conn()

    cur = conn.cursor(
        cursor_factory=
        psycopg2.extras.RealDictCursor
    )

    cur.execute(
        """
        SELECT *
        FROM support_messages
        WHERE status=%s
        ORDER BY id DESC
        LIMIT 100
        """,
        (status,)
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    return jsonify(
        messages=rows
    )


@app.route(
    "/support/reply",
    methods=["POST"]
)
def support_reply():

    if not is_admin_current():

        return jsonify(
            error="unauthorized"
        ), 403

    data = (
        request.get_json(
            silent=True
        )
        or {}
    )

    conn = get_conn()

    cur = conn.cursor()

    cur.execute(
        """
        UPDATE support_messages
        SET
            admin_reply=%s,
            status='replied'
        WHERE id=%s
        """,
        (
            data.get("reply"),
            data.get("id")
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    return jsonify(
        success=True
    )


# ============================================================
# COACH / STATS / TELEGRAM — CONSERVÉS
# ============================================================

@app.route("/coach/tips")
def coach_tips():

    return jsonify(
        tips=[
            {
                "text":
                "Publie un code clair : site + code + lien."
            },
            {
                "text":
                "Partage ton lien COD.IA personnel."
            },
            {
                "text":
                "Les parrainages doivent être réels et vérifiables."
            }
        ]
    )


@app.route("/coach/daily")
def coach_daily():

    return jsonify(
        challenge={
            "label":
                "Partage 1 code utile aujourd’hui",
            "progress": 0,
            "target": 1
        }
    )


@app.route("/stats")
def stats():

    return jsonify(
        ok=True
    )


@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram():

    return jsonify(
        success=True
    )


# ============================================================
# START
# ============================================================

try:
    init_db()
    get_or_create_challenge()
except Exception as e:
    logging.error(
        "Init DB error: %s",
        e
    )


if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            8080
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
