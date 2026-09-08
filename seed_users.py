import os
import secrets
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash
import psycopg2

DATABASE_URL = os.environ["DATABASE_URL"]
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

PEOPLE = [
    ("Lina Moreau", "lina", 8, "START"),
    ("Noah Bernard", "noah", 14, "START"),
    ("Emma Laurent", "emma", 22, "START"),
    ("Lucas Petit", "lucas", 6, "START"),
    ("Chloé Robert", "chloe", 31, "PRO"),
    ("Hugo Richard", "hugo", 11, "START"),
    ("Léa Durand", "lea", 19, "START"),
    ("Raphaël Dubois", "raphael", 4, "START"),
    ("Manon Morel", "manon", 27, "PRO"),
    ("Louis Simon", "louis", 9, "START"),
    ("Inès Michel", "ines", 16, "START"),
    ("Adam Lefevre", "adam", 3, "START"),
    ("Camille Roux", "camille", 41, "PRO"),
    ("Nathan Garcia", "nathan", 12, "START"),
    ("Jade David", "jade", 7, "START"),
    ("Théo Bertrand", "theo", 18, "START"),
    ("Sarah Roux", "sarah", 24, "START"),
    ("Tom Vincent", "tom", 5, "START"),
    ("Louise Fournier", "louise", 13, "START"),
    ("Maxime Moreau", "maxime", 36, "PRO"),
    ("Alice Girard", "alice", 10, "START"),
    ("Evan Lambert", "evan", 2, "START"),
    ("Nina Bonnet", "nina", 21, "START"),
    ("Arthur Francois", "arthur", 15, "START"),
    ("Eva Martinez", "eva", 28, "PRO"),
    ("Jules Lefebvre", "jules", 1, "START"),
    ("Maya Rousseau", "maya", 17, "START"),
    ("Ethan Nicolas", "ethan", 9, "START"),
    ("Louna Henry", "louna", 23, "START"),
    ("Sacha Perrin", "sacha", 55, "ELITE"),
]

ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PASSWORD = "CodiaTest2026!"


def make_code():
    return "COD-" + "".join(secrets.choice(ALPHA) for _ in range(6))


def now_minus(days, hours=0):
    return (datetime.utcnow() - timedelta(days=days, hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

con = psycopg2.connect(DATABASE_URL)
cur = con.cursor()
pw = generate_password_hash(PASSWORD)
ids = []

for i, (name, username, points, level) in enumerate(PEOPLE, start=1):
    email = f"{username}@cod-ia.test"
    cur.execute("SELECT id FROM users WHERE email=%s OR username=%s", (email, username))
    row = cur.fetchone()
    if row:
        ids.append(row[0])
        cur.execute(
            "UPDATE users SET name=%s, points=%s, level=%s, paid=1, ref_locked=1 WHERE id=%s",
            (name, points, level, row[0]),
        )
        continue
    cur.execute(
        """INSERT INTO users
           (name, username, email, password_hash, code, referred_by, level,
            points, claimed, paid, ref_locked, created_at, tier_index, points_locked)
           VALUES (%s,%s,%s,%s,%s,NULL,%s,%s,0,1,1,%s,0,0)
           RETURNING id""",
        (name, username, email, pw, make_code(), level, points, now_minus(40 - i)),
    )
    ids.append(cur.fetchone()[0])

# Faux parrainages validés (statistiques classement / accueil)
for i, uid in enumerate(ids):
    filleuls = min(5, (i % 6))
    for j in range(filleuls):
        other = ids[(i + j + 1) % len(ids)]
        if other == uid:
            continue
        cur.execute(
            "SELECT id FROM referrals WHERE referrer_id=%s AND referred_id=%s",
            (uid, other),
        )
        if cur.fetchone():
            continue
        try:
            cur.execute(
                """INSERT INTO referrals (referrer_id, referred_id, status, created_at)
                   VALUES (%s,%s,'validated',%s)""",
                (uid, other, now_minus(j + 1, i)),
            )
        except Exception:
            con.rollback()

# Activité
for i, uid in enumerate(ids):
    cur.execute(
        """INSERT INTO activities (user_id, kind, title, description, created_at)
           VALUES (%s,'point','Parrainage validé','Nouveau filleul',%s)""",
        (uid, now_minus(i % 10)),
    )

# Quelques posts Espace
samples = [
    ("promo", "ZARA20", "Réduction Zara ce week-end"),
    ("promo", "SHEIN15", "Code Shein -15%"),
    ("parrainage", "COD-HELLO1", "Code parrainage communauté"),
    ("promo", "NIKE10", "Nike running -10%"),
]
for i, (ptype, code, desc) in enumerate(samples):
    cur.execute(
        """INSERT INTO posts (user_id, type, code, description, created_at)
           VALUES (%s,%s,%s,%s,%s)""",
        (ids[i], ptype, code, desc, now_minus(i)),
    )

con.commit()
cur.close()
con.close()
print("30 faux comptes prêts")
print("Mot de passe commun :", PASSWORD)
print("Exemples : lina / noah / emma ... sacha")
