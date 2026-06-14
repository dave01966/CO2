#!/usr/bin/env python3
# CO₂ kibocsátás mérő – Flask webszerver verzió (online + telefon támogatással)

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import json, os, hashlib, random, math, csv, sqlite3, threading
from datetime import date, timedelta
from collections import defaultdict
import calendar as cal_module
import io
from contextlib import contextmanager

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "co2_meroy_alap_kulcs_valtoztasd_meg_2024"

# SESSION COOKIE beállítások
app.config["SESSION_COOKIE_SECURE"] = False   # SSL indításkor True-ra állítódik
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30  # 30 nap

DATA_DIR = "data"
DB_PATH = os.path.join(DATA_DIR, "co2.db")
_local = threading.local()

@app.teardown_appcontext
def close_db(exception=None):
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None

def get_db():
    """Thread-local SQLite kapcsolat WAL módban (párhuzamos olvasás/írás)."""
    if not hasattr(_local, "conn") or _local.conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # több olvasó + 1 író egyszerre
        conn.execute("PRAGMA synchronous=NORMAL") # gyors és biztonságos
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")  # 5mp várakozás lock esetén
        _local.conn = conn
    return _local.conn

def init_db():
    """Táblák létrehozása ha még nem léteznek."""
    db = get_db()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            nev      TEXT PRIMARY KEY,
            hash     TEXT NOT NULL,
            letrehozva TEXT DEFAULT (date('now'))
        );
        CREATE TABLE IF NOT EXISTS user_data (
            nev         TEXT PRIMARY KEY REFERENCES users(nev),
            xp          INTEGER DEFAULT 0,
            coins       INTEGER DEFAULT 0,
            napi_cel    REAL DEFAULT 8.0,
            streak      INTEGER DEFAULT 0,
            legjobb_streak INTEGER DEFAULT 0,
            aktiv_tema  TEXT DEFAULT NULL,
            megvett_temak TEXT DEFAULT '[]',
            bejegyzesek TEXT DEFAULT '{}',
            kuldetesek  TEXT DEFAULT '{}',
            szokasok    TEXT DEFAULT '{}',
            xp_log      TEXT DEFAULT '[]',
            coins_log   TEXT DEFAULT '[]',
            naplo       TEXT DEFAULT '{}',
            kihivasok   TEXT DEFAULT '{}',
            cel_napok   TEXT DEFAULT '{}'
        );
    """)
    db.commit()
    # Régi adatbázisok bővítése új oszlopokkal (ha hiányoznak)
    cols = {row["name"] for row in db.execute("PRAGMA table_info(user_data)").fetchall()}
    if "kihivasok" not in cols:
        db.execute("ALTER TABLE user_data ADD COLUMN kihivasok TEXT DEFAULT '{}'")
    if "cel_napok" not in cols:
        db.execute("ALTER TABLE user_data ADD COLUMN cel_napok TEXT DEFAULT '{}'")
    db.commit()
    _migrate_json_to_db()

def _migrate_json_to_db():
    """Egyszeri migráció: régi JSON fájlok -> SQLite."""
    db = get_db()
    users_json = os.path.join(DATA_DIR, "co2_users.json")
    if not os.path.exists(users_json):
        return
    try:
        with open(users_json, "r", encoding="utf-8") as f:
            users = json.load(f)
    except Exception:
        return
    migrated = 0
    for nev, info in users.items():
        existing = db.execute("SELECT nev FROM users WHERE nev=?", (nev,)).fetchone()
        if existing:
            continue
        db.execute("INSERT OR IGNORE INTO users (nev, hash) VALUES (?,?)",
                   (nev, info.get("hash", "")))
        # Felhasználó adatfájl
        data_path = os.path.join(DATA_DIR, f"co2_{nev}.json")
        d = {}
        if os.path.exists(data_path):
            try:
                with open(data_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                pass
        db.execute("""INSERT OR IGNORE INTO user_data
            (nev, xp, coins, napi_cel, streak, legjobb_streak,
             aktiv_tema, megvett_temak, bejegyzesek, kuldetesek,
             szokasok, xp_log, coins_log, naplo)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            nev,
            d.get("xp", 0), d.get("coins", 0),
            d.get("napi_cel", 8.0), d.get("streak", 0),
            d.get("legjobb_streak", 0), d.get("aktiv_tema"),
            json.dumps(d.get("megvett_temak", []), ensure_ascii=False),
            json.dumps(d.get("bejegyzesek", {}), ensure_ascii=False),
            json.dumps(d.get("kuldetesek", {}), ensure_ascii=False),
            json.dumps(d.get("szokasok", {}), ensure_ascii=False),
            json.dumps(d.get("xp_log", []), ensure_ascii=False),
            json.dumps(d.get("coins_log", []), ensure_ascii=False),
            json.dumps(d.get("naplo", {}), ensure_ascii=False),
        ))
        migrated += 1
    db.commit()
    if migrated:
        print(f"   ✅ {migrated} felhasználó migrálva JSON -> SQLite")
        # Régi fájlok átnevezése .bak-ra
        os.rename(users_json, users_json + ".bak")
os.makedirs(DATA_DIR, exist_ok=True)

TODAY = str(date.today())

# ── Kibocsátási tényezők ──────────────────────────────────────────────────────
KATEGORIAK = {
    "🚗 Közlekedés": {
        "szin": "#EF5350", "ikon": "🚗",
        "elemek": {
            "Autóval megtett út (km)":        ("auto_b",   0.190),
            "Motorral/robogóval (km)":        ("motor",    0.100),
            "Repülőút (km)":                  ("rep_r",    0.255),
            "Vonattal (km)":                  ("vonat",    0.041),
            "Busszal (km)":                   ("busz",     0.089),
            "Metróval/villamossal (km)":      ("metro",    0.029),
            "Taxival/Uberrel (km)":           ("taxi",     0.245),
        }
    },
    "🏠 Otthon & Energia": {
        "szin": "#42A5F5", "ikon": "🏠",
        "elemek": {
            "Villany (óra otthon töltve, átlag háztartás)": ("villany",  0.500),
            "Gázfűtés (nap, 1 = egész nap fűtöttél)":       ("gaz",      4.500),
            "Elektromos fűtés (nap)":                        ("efutes",   3.200),
            "Fatüzelés (nap)":                               ("tuzifa",   1.500),
            "Légkondicionáló (óra)":                         ("klima",    0.350),
            "Zuhanyozás (perc)":                             ("melegviz", 0.070),
            "Fürdőkád (alkalom)":                            ("furdo",    0.520),
        }
    },
    "🍽️ Élelmiszer": {
        "szin": "#66BB6A", "ikon": "🍽️",
        "elemek": {
            "Marha/bárány étel (adag)":       ("marha",    6.700),
            "Sertés/csirke étel (adag)":      ("csirke",   1.900),
            "Halétel (adag)":                 ("hal_t",    1.500),
            "Tejtermék (adag)":               ("tejt",     1.350),
            "Vegetáriánus étel (adag)":       ("huvely",   0.500),
            "Vegán étel (adag)":              ("vegan",    0.300),
            "Kávé (csésze)":                  ("kave",     0.210),
            "Étteremben ettél (alkalom)":     ("etterem",  4.500),
            "Gyorsétterem (alkalom)":         ("gyorset",  5.200),
            "Eldobott étel (adag)":           ("pazarlas", 2.500),
        }
    },
    "🛍️ Vásárlás": {
        "szin": "#AB47BC", "ikon": "🛍️",
        "elemek": {
            "Ruha vásárlás (db)":             ("ruhazat",  12.000),
            "Cipő vásárlás (pár)":            ("cipo",     11.000),
            "Kis elektronika (fülhallgató, stb.)": ("elek_k",  15.000),
            "Nagy elektronika (telefon, laptop)":  ("elek_n",  80.000),
            "Háztartási gép (mosógép, hűtő)": ("hgep_n",  400.000),
            "Online rendelés (csomag)":       ("csomag",    0.500),
            "Kozmetikum/tisztítószer (db)":   ("kozmet",    3.500),
        }
    },
    "♻️ Hulladék": {
        "szin": "#FFA726", "ikon": "♻️",
        "elemek": {
            "Szelektív gyűjtés? (igen = 1)":  ("recikl_p", -1.200),
            "Komposztálás? (igen = 1)":        ("komposz",  -0.500),
            "Sok szemét ma? (igen = 1)":        ("veg_h",     2.000),
        }
    },
    "🌳 Kompenzáció": {
        "szin": "#26A69A", "ikon": "🌳",
        "elemek": {
            "Fa ültetés (db)":                ("fa_ultet", -21.770),
            "Napelem saját (kWh termelt)":    ("napelem2",  -0.233),
        }
    },
}

FAKTOROK = {}
for _kn, _ki in KATEGORIAK.items():
    for _en, (_k, _f) in _ki["elemek"].items():
        FAKTOROK[_k] = (_en, _f, _kn)

KULDETESEK = [
    {"s":"Menj gyalog v. biciklivel ≥3 km-t!",               "xp":60,  "sv":0.63,  "i":"🚲", "kat":"mozgas"},
    {"s":"Egyél húsmentes ételt egész nap!",                  "xp":80,  "sv":5.50,  "i":"🥗", "kat":"etel"},
    {"s":"Kapcsold ki az összes készenléti eszközt!",         "xp":30,  "sv":0.10,  "i":"🔌", "kat":"energia"},
    {"s":"Hozz újrahasználható bevásárlótáskát!",             "xp":25,  "sv":0.05,  "i":"👜", "kat":"vasarlas"},
    {"s":"Zuhanyzás ≤5 perc!",                                "xp":35,  "sv":0.15,  "i":"🚿", "kat":"energia"},
    {"s":"Ne vásárolj semmit szükségtelent!",                 "xp":50,  "sv":4.50,  "i":"💰", "kat":"vasarlas"},
    {"s":"Tömegközlekedést használj autó helyett!",           "xp":65,  "sv":1.80,  "i":"🚌", "kat":"kozlekedes"},
    {"s":"Reciklálj ≥1 kg hulladékot!",                       "xp":40,  "sv":0.50,  "i":"♻️", "kat":"hulladek"},
    {"s":"Ültess egy növényt v. öntözd a kerted!",            "xp":55,  "sv":0.02,  "i":"🌱", "kat":"termeszet"},
    {"s":"Olvass online, papír újság helyett!",               "xp":20,  "sv":0.01,  "i":"📱", "kat":"vasarlas"},
    {"s":"Csökkentsd a fűtést 1 fokkal!",                     "xp":50,  "sv":0.30,  "i":"🌡️", "kat":"energia"},
    {"s":"Egyél helyi terméket importált helyett!",           "xp":40,  "sv":0.80,  "i":"🛒", "kat":"etel"},
    {"s":"Húzd ki a töltőt feltöltés után!",                  "xp":20,  "sv":0.05,  "i":"🔋", "kat":"energia"},
    {"s":"Menj el egy helyi takarítási akcióra!",             "xp":90,  "sv":1.00,  "i":"🧹", "kat":"termeszet"},
    {"s":"Főzz otthon étterem helyett!",                      "xp":45,  "sv":2.10,  "i":"🍳", "kat":"etel"},
    {"s":"LED izzót cserélj be!",                             "xp":40,  "sv":0.20,  "i":"💡", "kat":"energia"},
    {"s":"Kerékpárral menj bevásárolni!",                     "xp":55,  "sv":0.84,  "i":"🛒", "kat":"kozlekedes"},
    {"s":"Komposztálj konyhahulladékot!",                     "xp":50,  "sv":0.30,  "i":"🌿", "kat":"hulladek"},
    {"s":"Ellenőrizd az autógumik nyomását!",                 "xp":25,  "sv":0.12,  "i":"🚗", "kat":"kozlekedes"},
    {"s":"Igyál csapvizet palackos helyett!",                 "xp":30,  "sv":0.08,  "i":"💧", "kat":"energia"},
    {"s":"Adj el/adj ajándékba egy felesleges tárgyat!",      "xp":45,  "sv":15.00, "i":"🎁", "kat":"vasarlas"},
    {"s":"Egyél fennmaradó ételből, ne dobj semmit!",         "xp":35,  "sv":3.80,  "i":"🍱", "kat":"etel"},
    {"s":"Vegyél második kézből egy terméket!",               "xp":60,  "sv":20.00, "i":"🏪", "kat":"vasarlas"},
    {"s":"Kapcsold ki a monitort amikor nem használod!",      "xp":15,  "sv":0.03,  "i":"🖥️", "kat":"energia"},
    {"s":"Sétálj legalább 30 percet a szabadban!",            "xp":35,  "sv":0.00,  "i":"🚶", "kat":"mozgas"},
]

SZOKASOK = [
    {"nev": "Napi kerékpározás",    "ikon": "🚲", "xp_nap": 20, "cel_nap": 21},
    {"nev": "Húsmentes hétköznap",  "ikon": "🥗", "xp_nap": 25, "cel_nap": 21},
    {"nev": "5 perces zuhanyzás",   "ikon": "🚿", "xp_nap": 15, "cel_nap": 21},
    {"nev": "Zero waste nap",       "ikon": "♻️", "xp_nap": 30, "cel_nap": 14},
    {"nev": "Otthoni főzés",        "ikon": "🍳", "xp_nap": 20, "cel_nap": 14},
    {"nev": "Tömegközlekedés",      "ikon": "🚌", "xp_nap": 25, "cel_nap": 21},
    {"nev": "Napi 8000+ lépés",     "ikon": "👟", "xp_nap": 15, "cel_nap": 30},
    {"nev": "Energiatakarékos nap", "ikon": "💡", "xp_nap": 20, "cel_nap": 14},
]

RANGOK = [
    (0,      "🌱 Kezdő",             "#78909C"),
    (150,    "🌿 Zöld Újonc",        "#66BB6A"),
    (400,    "🍃 Ökotudatos",         "#26A69A"),
    (800,    "♻️ Újrahasznosító",     "#42A5F5"),
    (1400,   "🌊 Természetvédő",      "#1E88E5"),
    (2200,   "🌳 Erdővédő",           "#7E57C2"),
    (3200,   "⚡ Energiatakarékos",   "#AB47BC"),
    (4500,   "☀️ Napenergia Bajnok",  "#FFA726"),
    (6000,   "🌍 Bolygóvédő",         "#EF5350"),
    (8000,   "🦋 Ökoszisztéma Őr",    "#EC407A"),
    (10500,  "🏆 Klímahős",           "#FFD54F"),
    (13500,  "⭐ Zöld Legenda",        "#00E5C3"),
    (17000,  "🌌 Klíma Mester",        "#CE93D8"),
]

TEMAK = [
    {"id": None, "nev": "🌑 Alapértelmezett", "ar": 0},
    {"id": "deep_ocean", "nev": "🌊 Mélytenger", "ar": 100},
    {"id": "forest",     "nev": "🌲 Erdő",       "ar": 150},
    {"id": "sunset",     "nev": "🌅 Napszálta",   "ar": 200},
    {"id": "arctic",     "nev": "❄️ Sarki jég",   "ar": 250},
    {"id": "galaxy",     "nev": "🌌 Galaxis",      "ar": 300},
    {"id": "ember",      "nev": "🔥 Parázs",       "ar": 350},
]

EU_NAP   = 21.9
GLOB_NAP = 12.3
PAR_NAP  = 4.1
FENN_NAP = 2.1
CEL_DEF  = 10.0

TIPPEK = [
    "💡 Ha rövidebb utakra kerékpárt használnál, akár 2 kg CO₂-t is spórolhatsz naponta.",
    "💡 Egyetlen húsmentes nap hetente ~330 kg CO₂-t spórol évente.",
    "💡 1 fok hőmérséklet-csökkentés ~5-10% energiamegtakarítást jelent.",
    "🌟 Fantasztikus nap volt! Próbáld meg holnap is így tartani a streak-edet!",
    "💡 LED izzókra váltva 75%-kal csökkented a világítási fogyasztást.",
    "💡 Próbálj ki egy carpooling appot – a megosztott autózás felére csökkenti az emissziódat.",
    "💡 A helyi piacokon vásárolt zöldség importáltnál 4x kisebb lábnyomú.",
]

# ── Adatkezelés ───────────────────────────────────────────────────────────────

def jelszo_hash(jelszo):
    return hashlib.sha256(jelszo.encode("utf-8")).hexdigest()

def users_betolt():
    """Visszaadja {nev: {hash: ...}} dict-et – csak auth-hoz kell."""
    db = get_db()
    rows = db.execute("SELECT nev, hash FROM users").fetchall()
    return {r["nev"]: {"hash": r["hash"]} for r in rows}

def users_ment(users):
    """Új user hozzáadása vagy hash frissítése."""
    db = get_db()
    for nev, info in users.items():
        db.execute("INSERT OR REPLACE INTO users (nev, hash) VALUES (?,?)",
                   (nev, info["hash"]))
        db.execute("INSERT OR IGNORE INTO user_data (nev) VALUES (?)", (nev,))
    db.commit()

def _default_szokasok():
    return {s["nev"]: {"aktiv": False, "napok": []} for s in SZOKASOK}

def betolt(felhasznalo):
    """User adatok betöltése SQLite-ból."""
    db = get_db()
    row = db.execute("SELECT * FROM user_data WHERE nev=?", (felhasznalo,)).fetchone()
    if row is None:
        # Első belépés: sor létrehozása
        db.execute("INSERT OR IGNORE INTO user_data (nev) VALUES (?)", (felhasznalo,))
        db.commit()
        row = db.execute("SELECT * FROM user_data WHERE nev=?", (felhasznalo,)).fetchone()
    def jd(val, default):
        try: return json.loads(val) if val else default
        except: return default
    d = {
        "bejegyzesek":    jd(row["bejegyzesek"], {}),
        "kuldetesek":     jd(row["kuldetesek"], {}),
        "szokasok":       jd(row["szokasok"], _default_szokasok()),
        "xp":             row["xp"] or 0,
        "xp_log":         jd(row["xp_log"], []),
        "napi_cel":       row["napi_cel"] or CEL_DEF,
        "streak":         row["streak"] or 0,
        "legjobb_streak": row["legjobb_streak"] or 0,
        "coins":          row["coins"] or 0,
        "coins_log":      jd(row["coins_log"], []),
        "naplo":          jd(row["naplo"], {}),
        "kihivasok":      jd(row["kihivasok"], {}),
        "cel_napok":      jd(row["cel_napok"], {}),
        "aktiv_tema":     row["aktiv_tema"],
        "megvett_temak":  jd(row["megvett_temak"], []),
    }
    # Szokások alapértelmezés ha hiányos
    for s in SZOKASOK:
        d["szokasok"].setdefault(s["nev"], {"aktiv": False, "napok": []})
    return d

def ment(d, felhasznalo):
    """User adatok mentése SQLite-ba – atomikus UPDATE."""
    db = get_db()
    db.execute("""UPDATE user_data SET
        xp=?, coins=?, napi_cel=?, streak=?, legjobb_streak=?,
        aktiv_tema=?, megvett_temak=?,
        bejegyzesek=?, kuldetesek=?, szokasok=?,
        xp_log=?, coins_log=?, naplo=?, kihivasok=?, cel_napok=?
        WHERE nev=?""", (
        d.get("xp", 0), d.get("coins", 0),
        d.get("napi_cel", CEL_DEF), d.get("streak", 0),
        d.get("legjobb_streak", 0), d.get("aktiv_tema"),
        json.dumps(d.get("megvett_temak", []), ensure_ascii=False),
        json.dumps(d.get("bejegyzesek", {}), ensure_ascii=False),
        json.dumps(d.get("kuldetesek", {}), ensure_ascii=False),
        json.dumps(d.get("szokasok", {}), ensure_ascii=False),
        json.dumps(d.get("xp_log", []), ensure_ascii=False),
        json.dumps(d.get("coins_log", []), ensure_ascii=False),
        json.dumps(d.get("naplo", {}), ensure_ascii=False),
        json.dumps(d.get("kihivasok", {}), ensure_ascii=False),
        json.dumps(d.get("cel_napok", {}), ensure_ascii=False),
        felhasznalo,
    ))
    db.commit()
def nap_osszeg(bej):
    return round(sum(bej.get(k, 0) * f for k, (_, f, __) in FAKTOROK.items()), 3)

def kat_osszeg(bej):
    res = {k: 0.0 for k in KATEGORIAK}
    for kulcs, ertek in bej.items():
        if kulcs in FAKTOROK:
            _, f, kat = FAKTOROK[kulcs]
            res[kat] += ertek * f
    return res

def streak_szamol(d):
    s = 0
    nap = date.today()
    while str(nap) in d["bejegyzesek"]:
        s += 1
        nap -= timedelta(days=1)
    return s

def rang_info(xp):
    rang = RANGOK[0]
    for r in RANGOK:
        if xp >= r[0]:
            rang = r
    idx = RANGOK.index(rang)
    kov = RANGOK[idx + 1] if idx + 1 < len(RANGOK) else None
    prog = ((xp - rang[0]) / (kov[0] - rang[0])) if kov else 1.0
    return rang[1], rang[2], min(prog, 1.0), (kov[0] if kov else rang[0])

def napi_kuldetesek(d):
    nap = TODAY
    if nap not in d["kuldetesek"]:
        seed = int(nap.replace("-", "")) % len(KULDETESEK)
        bon = random.sample([i for i in range(len(KULDETESEK)) if i != seed], 3)
        d["kuldetesek"][nap] = {
            "fo": seed, "bonuszok": bon,
            "fo_kesz": False, "bonusz_kesz": [],
        }
    return d["kuldetesek"][nap]

def xp_hozzaad(d, mennyiseg, ok, felhasznalo):
    d["xp"] = d.get("xp", 0) + mennyiseg
    d.setdefault("xp_log", []).append({"datum": TODAY, "xp": mennyiseg, "ok": ok})
    d["coins"] = d.get("coins", 0) + mennyiseg
    d.setdefault("coins_log", []).append({"datum": TODAY, "coins": mennyiseg, "ok": ok})
    ment(d, felhasznalo)

def elojelzes(d, napok=30):
    adatok = sorted(d["bejegyzesek"].items())
    if len(adatok) < 3:
        return None
    utolso = adatok[-min(30, len(adatok)):]
    n = len(utolso)
    ertekek = [nap_osszeg(v) for _, v in utolso]
    x_atl = (n - 1) / 2
    y_atl = sum(ertekek) / n
    num = sum((i - x_atl) * (ertekek[i] - y_atl) for i in range(n))
    den = sum((i - x_atl) ** 2 for i in range(n))
    slope = num / den if den else 0
    pred = []
    for i in range(napok):
        val = max(0, ertekek[-1] + slope * (i + 1))
        d_jov = date.today() + timedelta(days=i + 1)
        pred.append((str(d_jov), val))
    return pred, slope

# ── Auth helpers ──────────────────────────────────────────────────────────────

def current_user():
    return session.get("felhasznalo")

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if current_user():
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    # Ha már be van jelentkezve és nem akar váltani, dobja vissza
    if current_user() and request.method == "GET" and request.args.get("valtas") != "1":
        return redirect(url_for("dashboard"))
    error = None
    mod = "belepes"
    if request.method == "POST":
        mod = request.form.get("mod", "belepes")
        nev = request.form.get("nev", "").strip()
        jelszo = request.form.get("jelszo", "")
        if not nev:
            error = "Add meg a felhasználónevet!"
        elif len(nev) < 2:
            error = "A felhasználónév legalább 2 karakter legyen!"
        else:
            users = users_betolt()
            h = jelszo_hash(jelszo)
            if mod == "belepes":
                if nev not in users:
                    error = "Nincs ilyen felhasználó! Regisztrálj először."
                elif users[nev]["hash"] != h:
                    error = "Hibás jelszó!"
                else:
                    session.permanent = True
                    session["felhasznalo"] = nev
                    return redirect(url_for("dashboard"))
            else:
                if nev in users:
                    error = "Ez a felhasználónév már foglalt!"
                elif len(jelszo) < 4:
                    error = "A jelszó legalább 4 karakter legyen!"
                else:
                    users[nev] = {"hash": h}
                    users_ment(users)
                    session.permanent = True
                    session["felhasznalo"] = nev
                    return redirect(url_for("dashboard"))
    return render_template("login.html", error=error, mod=mod)


@app.route("/ranglista")
@login_required
def ranglista():
    db = get_db()
    sql = ("SELECT u.nev, ud.xp, ud.streak, ud.bejegyzesek "
           "FROM users u LEFT JOIN user_data ud ON u.nev=ud.nev "
           "ORDER BY ud.xp DESC")
    rows = db.execute(sql).fetchall()
    lista = []
    for row in rows:
        try:
            xp = row["xp"] or 0
            rang_nev, rang_emoji, _, _ = rang_info(xp)
            bej = json.loads(row["bejegyzesek"] or "{}")
            utolso30 = sorted(bej.items())[-30:]
            atlag = round(sum(nap_osszeg(v) for _, v in utolso30) / len(utolso30), 2) if utolso30 else 0
            lista.append({
                "nev": row["nev"],
                "xp": xp,
                "rang": rang_nev,
                "rang_emoji": rang_emoji,
                "streak": row["streak"] or 0,
                "atlag_co2": atlag,
            })
        except Exception:
            pass
    en = current_user()
    return render_template("ranglista.html", lista=lista, en=en)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login") + "?valtas=1")

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("app.html", page="dashboard")

@app.route("/bevitel")
@login_required
def bevitel():
    return render_template("app.html", page="bevitel")

@app.route("/kuldetesek")
@login_required
def kuldetesek():
    return render_template("app.html", page="kuldetesek")

@app.route("/szokasok")
@login_required
def szokasok():
    return render_template("app.html", page="szokasok")

@app.route("/statisztika")
@login_required
def statisztika():
    return render_template("app.html", page="statisztika")

@app.route("/hoterkep")
@login_required
def hoterkep():
    return render_template("app.html", page="hoterkep")

@app.route("/elojelzes")
@login_required
def elojelzes_page():
    return render_template("app.html", page="elojelzes")

@app.route("/kalkulator")
@login_required
def kalkulator():
    return render_template("app.html", page="kalkulator")

@app.route("/naplo")
@login_required
def naplo():
    return render_template("app.html", page="naplo")

@app.route("/bolt")
@login_required
def bolt():
    return render_template("app.html", page="bolt")

@app.route("/beallitas")
@login_required
def beallitas():
    return render_template("app.html", page="beallitas")

@app.route("/heti")
@login_required
def heti():
    return render_template("app.html", page="heti")

@app.route("/tippek")
@login_required
def tippek():
    return render_template("app.html", page="tippek")

@app.route("/kihivas")
@login_required
def kihivas():
    return render_template("app.html", page="kihivas")

@app.route("/cimke")
@login_required
def cimke():
    return render_template("app.html", page="cimke")

# ── API endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/data")
@login_required
def api_data():
    u = current_user()
    d = betolt(u)
    streak = streak_szamol(d)
    d["streak"] = streak
    ment(d, u)
    today_bej = d["bejegyzesek"].get(TODAY, {})
    today_sum = nap_osszeg(today_bej)
    xp = d.get("xp", 0)
    rang_nev, rang_szin, rang_prog, rang_kov = rang_info(xp)

    utolso30 = []
    for i in range(29, -1, -1):
        nap = str(date.today() - timedelta(days=i))
        v = nap_osszeg(d["bejegyzesek"].get(nap, {}))
        utolso30.append({"datum": nap, "ertek": v})

    kat = kat_osszeg(today_bej)
    kuldetesek_ma = napi_kuldetesek(d)

    return jsonify({
        "felhasznalo": u,
        "xp": xp,
        "coins": d.get("coins", 0),
        "streak": streak,
        "napi_cel": d.get("napi_cel", CEL_DEF),
        "today_sum": today_sum,
        "today_bej": today_bej,
        "rang_nev": rang_nev,
        "rang_szin": rang_szin,
        "rang_prog": rang_prog,
        "rang_kov": rang_kov,
        "utolso30": utolso30,
        "kategoriak_ma": {k: round(v, 3) for k, v in kat.items()},
        "kuldetesek_ma": kuldetesek_ma,
        "kuldetesek_lista": KULDETESEK,
        "szokasok": d.get("szokasok", {}),
        "szokasok_lista": SZOKASOK,
        "naplo": d.get("naplo", {}),
        "kihivasok_szam": sum(1 for k in d.get("kihivasok", {}).values() if not k.get("elfogadva")),
        "aktiv_tema": d.get("aktiv_tema"),
        "megvett_temak": d.get("megvett_temak", []),
        "tipp": random.choice(TIPPEK),
        "bejegyzesek": d.get("bejegyzesek", {}),
        "EU_NAP": EU_NAP, "GLOB_NAP": GLOB_NAP,
        "PAR_NAP": PAR_NAP, "FENN_NAP": FENN_NAP,
    })

@app.route("/api/kategoriak")
@login_required
def api_kategoriak():
    return jsonify(KATEGORIAK)

@app.route("/api/bevitel", methods=["POST"])
@login_required
def api_bevitel():
    u = current_user()
    d = betolt(u)
    data = request.json or {}
    nap = data.get("datum", TODAY)
    bej = data.get("bejegyzesek", {})
    tiszta = {k: float(v) for k, v in bej.items() if k in FAKTOROK and v}
    d["bejegyzesek"][nap] = tiszta
    osszeg = nap_osszeg(tiszta)
    xp_earn = 0
    cel_napok = d.setdefault("cel_napok", {})
    # Csak egyszer adunk célteljesítés XP-t egy adott napra (szerkesztésnél nem duplázódik)
    if osszeg < d.get("napi_cel", CEL_DEF):
        if not cel_napok.get(nap):
            xp_earn = 50
            cel_napok[nap] = True
            xp_hozzaad(d, xp_earn, "Célteljesítés", u)
        else:
            ment(d, u)
    else:
        # Ha a nap utólag a cél fölé került, vegyük el a jelölést (újra elérhető legyen, ha javít)
        if cel_napok.get(nap):
            cel_napok[nap] = False
        ment(d, u)
    streak = streak_szamol(d)
    d["streak"] = streak
    ment(d, u)
    return jsonify({"ok": True, "osszeg": osszeg, "xp_earn": xp_earn, "streak": streak})

@app.route("/api/kuldetes_kesz", methods=["POST"])
@login_required
def api_kuldetes_kesz():
    u = current_user()
    d = betolt(u)
    data = request.json or {}
    tipusa = data.get("tipusa")
    bonusz_idx = data.get("bonusz_idx")
    k = napi_kuldetesek(d)
    xp_earn = 0
    if tipusa == "fo" and not k["fo_kesz"]:
        k["fo_kesz"] = True
        xp_earn = KULDETESEK[k["fo"]]["xp"]
        xp_hozzaad(d, xp_earn, "Főküldetés", u)
    elif tipusa == "bonusz" and bonusz_idx is not None:
        bi = k["bonuszok"][bonusz_idx]
        if bi not in k["bonusz_kesz"]:
            k["bonusz_kesz"].append(bi)
            xp_earn = KULDETESEK[bi]["xp"]
            xp_hozzaad(d, xp_earn, "Bónuszküldetés", u)
    d["kuldetesek"][TODAY] = k
    ment(d, u)
    return jsonify({"ok": True, "xp_earn": xp_earn, "xp": d["xp"], "coins": d["coins"]})

@app.route("/api/szokas_toggle", methods=["POST"])
@login_required
def api_szokas_toggle():
    u = current_user()
    d = betolt(u)
    data = request.json or {}
    nev = data.get("nev")
    akcio = data.get("akcio")
    sz = d["szokasok"].get(nev)
    if not sz:
        return jsonify({"ok": False})
    xp_earn = 0
    if akcio == "aktivalas":
        sz["aktiv"] = True
    elif akcio == "deaktivalas":
        sz["aktiv"] = False
    elif akcio == "teljesit":
        if TODAY not in sz["napok"]:
            sz["napok"].append(TODAY)
            s_def = next((s for s in SZOKASOK if s["nev"] == nev), None)
            if s_def:
                xp_earn = s_def["xp_nap"]
                xp_hozzaad(d, xp_earn, f"Szokás: {nev}", u)
    ment(d, u)
    return jsonify({"ok": True, "xp_earn": xp_earn, "xp": d["xp"], "coins": d["coins"]})

@app.route("/api/naplo_ment", methods=["POST"])
@login_required
def api_naplo_ment():
    u = current_user()
    d = betolt(u)
    data = request.json or {}
    nap = data.get("datum", TODAY)
    szoveg = data.get("szoveg", "")
    d["naplo"][nap] = szoveg
    ment(d, u)
    return jsonify({"ok": True})

@app.route("/api/elojelzes")
@login_required
def api_elojelzes():
    u = current_user()
    d = betolt(u)
    res = elojelzes(d)
    if not res:
        return jsonify({"ok": False, "uzenet": "Kevés adat (min. 3 nap kell)"})
    pred, slope = res
    return jsonify({"ok": True, "pred": pred, "slope": slope})

@app.route("/api/beallitas", methods=["POST"])
@login_required
def api_beallitas():
    u = current_user()
    d = betolt(u)
    data = request.json or {}
    if "napi_cel" in data:
        d["napi_cel"] = float(data["napi_cel"])
    ment(d, u)
    return jsonify({"ok": True})

@app.route("/api/bolt_vesz", methods=["POST"])
@login_required
def api_bolt_vesz():
    u = current_user()
    d = betolt(u)
    data = request.json or {}
    tema_id = data.get("tema_id")
    tema = next((t for t in TEMAK if t["id"] == tema_id), None)
    if not tema:
        return jsonify({"ok": False, "hiba": "Érvénytelen téma"})
    megvett = d.get("megvett_temak", [])
    # Ha már megvette (vagy ingyenes téma): csak alkalmazza
    if tema["id"] in megvett or tema.get("ar", 0) == 0:
        d["aktiv_tema"] = tema_id
        ment(d, u)
        return jsonify({"ok": True, "alkalmaz": True, "coins": d.get("coins", 0)})
    # Vásárlás
    if d.get("coins", 0) < tema["ar"]:
        return jsonify({"ok": False, "hiba": f"Nincs elég érme! ({tema['ar']} kell, neked: {d.get('coins',0)})"})
    d["coins"] -= tema["ar"]
    if "megvett_temak" not in d:
        d["megvett_temak"] = []
    d["megvett_temak"].append(tema["id"])
    d["aktiv_tema"] = tema_id
    ment(d, u)
    return jsonify({"ok": True, "coins": d["coins"], "megvett": d["megvett_temak"]})

@app.route("/api/export_csv")
@login_required
def api_export_csv():
    from flask import Response
    u = current_user()
    d = betolt(u)
    output = io.StringIO()
    w = __import__("csv").writer(output, delimiter=";")
    fejlec = ["Dátum", "Összeg (kg CO₂)"]
    for k, (nev, _, __) in FAKTOROK.items():
        fejlec.append(nev)
    w.writerow(fejlec)
    for nap, bej in sorted(d["bejegyzesek"].items()):
        sor = [nap, f"{nap_osszeg(bej):.3f}"]
        for k in FAKTOROK:
            sor.append(bej.get(k, 0))
        w.writerow(sor)
    output.seek(0)
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=co2_{u}.csv"}
    )

# ── Online tunnel indítás ─────────────────────────────────────────────────────

def start_ngrok_tunnel(port):
    """ngrok tunnel indítása a háttérben"""
    try:
        import subprocess, time, urllib.request
        proc = subprocess.Popen(
            ["ngrok", "http", str(port), "--log=stdout"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        time.sleep(2)
        try:
            with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=5) as r:
                data = json.loads(r.read())
                tunnels = data.get("tunnels", [])
                for t in tunnels:
                    if t.get("proto") == "https":
                        return t["public_url"]
                if tunnels:
                    return tunnels[0]["public_url"]
        except:
            pass
        return None
    except Exception as e:
        return None

def start_cloudflared_tunnel(port):
    """Cloudflare Quick Tunnel indítása"""
    try:
        import subprocess, time, re
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True
        )
        # Várjuk az URL-t a kimenetből
        for _ in range(20):
            time.sleep(1)
            line = proc.stderr.readline()
            m = re.search(r'https://[a-z0-9\-]+\.trycloudflare\.com', line)
            if m:
                return m.group(0)
        return None
    except Exception:
        return None

# ── Main ──────────────────────────────────────────────────────────────────────

def generate_ssl_cert(cert_file="cert.pem", key_file="key.pem"):
    """Önaláírt SSL tanúsítvány generálása – először cryptography csomaggal próbál,
    aztán openssl paranccsal, ha az nincs."""
    # 1. próba: cryptography csomag (pip install cryptography)
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime, ipaddress
        now = datetime.datetime.now(datetime.timezone.utc)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Helyi IP-k összegyűjtése a SAN-hoz
        import socket
        san_ips = [ipaddress.IPv4Address("127.0.0.1")]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            lan_ip = s.getsockname()[0]
            s.close()
            san_ips.append(ipaddress.IPv4Address(lan_ip))
        except:
            lan_ip = "127.0.0.1"

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, u"co2app"),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(
                x509.SubjectAlternativeName(
                    [x509.IPAddress(ip) for ip in san_ips] +
                    [x509.DNSName("localhost"), x509.DNSName("co2app.local")]
                ),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )

        with open(key_file, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(cert_file, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        print("   🔐 SSL tanúsítvány generálva (cryptography)")
        return True

    except ImportError:
        pass

    # 2. próba: openssl parancs
    ret = os.system(
        f'openssl req -x509 -newkey rsa:2048 -keyout {key_file} -out {cert_file} '
        f'-days 365 -nodes -subj "/CN=co2app" 2>/dev/null'
    )
    if ret == 0:
        print("   🔐 SSL tanúsítvány generálva (openssl)")
        return True

    print("   ⚠️  SSL tanúsítvány generálás sikertelen (telepítsd: pip install cryptography)")
    return False


if __name__ == "__main__":
    import argparse
    # Adatbázis inicializálása (táblák + JSON migráció)
    os.makedirs(DATA_DIR, exist_ok=True)
    init_db()
    p = argparse.ArgumentParser(description="CO₂ Mérő webszerver")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--http", action="store_true", help="Kényszer HTTP mód (nem ajánlott, telefon nem fog csatlakozni)")
    p.add_argument("--online", choices=["ngrok", "cloudflare", "auto"],
                   help="Online tunnel indítása (ngrok / cloudflare / auto)")
    args = p.parse_args()

    # Helyi IP
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "127.0.0.1"

    # ── HTTPS alapból (telefonok is csatlakoznak) ──────────────────────────────
    ssl_context = None
    protocol = "http"

    if not args.http:
        import ssl
        cert_file = "cert.pem"
        key_file  = "key.pem"

        # Tanúsítvány újragenerálása ha hiányzik vagy nem tölthető be
        regen = True
        if os.path.exists(cert_file) and os.path.exists(key_file):
            try:
                ctx_test = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx_test.load_cert_chain(cert_file, key_file)
                regen = False   # érvényes, meglévőt használjuk
                print("   🔐 Meglévő SSL tanúsítvány betöltve")
            except Exception:
                print("   ⚠️  Meglévő cert.pem/key.pem érvénytelen, újragenerálás...")
                regen = True

        if regen:
            # 1. próba: cryptography csomag telepítése és használata
            crypto_ok = False
            try:
                import cryptography  # noqa
                crypto_ok = True
            except ImportError:
                print("   📦 cryptography csomag telepítése (egyszer kell)...")
                ret = os.system(
                    "pip install cryptography --quiet 2>&1 || "
                    "pip3 install cryptography --quiet 2>&1"
                )
                if ret == 0:
                    try:
                        import cryptography  # noqa
                        crypto_ok = True
                    except ImportError:
                        pass

            cert_generated = generate_ssl_cert(cert_file, key_file)

            if not cert_generated:
                print("   ❌ SSL tanúsítvány generálás sikertelen!")
                print("   👉 Futtasd kézzel:")
                print("      openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj \"/CN=co2app\"")
                print("   Vagy telepítsd: pip install cryptography")
                print("   ⚠️  HTTP módra váltás – telefonról NEM fog működni!\n")

        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert_file, key_file)
            ssl_context = ctx
            protocol = "https"
            app.config["SESSION_COOKIE_SECURE"] = True
        except Exception as e:
            print(f"   ❌ SSL betöltés hiba: {e}")
            print("   ⚠️  HTTP módra váltás – telefonról NEM fog működni!")

    print(f"\n🌿 CO₂ Mérő webszerver indul...")
    print(f"   📍 Helyi elérés:    {protocol}://localhost:{args.port}")
    print(f"   📱 LAN elérés:      {protocol}://{local_ip}:{args.port}")
    if protocol == "https":
        print(f"   ⚠️  Önaláírt tanúsítvány – böngészőben: Speciális → Tovább (egyszeri elfogadás)")
    else:
        print(f"   ⚠️  HTTP mód – telefonok nem tudnak csatlakozni HTTPS nélkül!")

    # Online tunnel
    if args.online:
        import threading
        def launch_tunnel():
            import time
            time.sleep(1.5)
            url = None
            mode = args.online

            if mode in ("ngrok", "auto"):
                print("\n   🌐 ngrok tunnel indítása...")
                url = start_ngrok_tunnel(args.port)
                if url:
                    print(f"   ✅ ngrok online URL: {url}")
                    print(f"   📲 Ezt írd be a telefonon: {url}")

            if not url and mode in ("cloudflare", "auto"):
                print("\n   🌐 Cloudflare tunnel indítása...")
                url = start_cloudflared_tunnel(args.port)
                if url:
                    print(f"   ✅ Cloudflare online URL: {url}")
                    print(f"   📲 Ezt írd be a telefonon: {url}")

            if not url:
                print("\n   ⚠️  Online tunnel nem sikerült!")
                if mode in ("ngrok", "auto"):
                    print("      → ngrok telepítés: https://ngrok.com/download")
                    print("        majd: ngrok config add-authtoken <token>")
                if mode in ("cloudflare", "auto"):
                    print("      → cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
                print(f"\n   📱 LAN-on belül elérhető: {protocol}://{local_ip}:{args.port}")
            print()

        t = threading.Thread(target=launch_tunnel, daemon=True)
        t.start()

    print(f"\n   A szerver leállításához: Ctrl+C\n")
    app.run(host=args.host, port=args.port, debug=False, ssl_context=ssl_context)


# ── PWA support ───────────────────────────────────────────────────────────────

@app.route("/manifest.json")
def manifest():
    from flask import send_from_directory
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")

# ── ÚJ FUNKCIÓK v4 ────────────────────────────────────────────────────────────

@app.route("/api/heti_jelentes")
@login_required
def api_heti_jelentes():
    """Heti összefoglaló: átlag, legjobb/legrosszabb nap, kategória breakdown."""
    u = current_user()
    d = betolt(u)
    today = date.today()
    het_napok = [str(today - timedelta(days=i)) for i in range(7)]
    het_adatok = []
    for nap in het_napok:
        bej = d["bejegyzesek"].get(nap, {})
        s = nap_osszeg(bej)
        kat = kat_osszeg(bej)
        het_adatok.append({"datum": nap, "osszeg": s, "kategoriak": {k: round(v,3) for k,v in kat.items()}, "bejegyzesek": bej})

    osszegek = [x["osszeg"] for x in het_adatok if x["osszeg"] > 0]
    atlag = round(sum(osszegek)/len(osszegek), 3) if osszegek else 0
    legjobb = min(het_adatok, key=lambda x: x["osszeg"]) if osszegek else None
    legrosszabb = max(het_adatok, key=lambda x: x["osszeg"]) if osszegek else None

    # Összes kategória összesítve a hétre
    het_kat = {k: 0.0 for k in KATEGORIAK}
    for nap_adat in het_adatok:
        for k, v in nap_adat["kategoriak"].items():
            het_kat[k] = round(het_kat.get(k, 0) + v, 3)

    # Előző hét
    elozo_het = [str(today - timedelta(days=i+7)) for i in range(7)]
    elozo_osszegek = [nap_osszeg(d["bejegyzesek"].get(n, {})) for n in elozo_het if d["bejegyzesek"].get(n)]
    elozo_atlag = round(sum(elozo_osszegek)/len(elozo_osszegek), 3) if elozo_osszegek else 0

    valtozas_pct = round(((atlag - elozo_atlag) / elozo_atlag * 100), 1) if elozo_atlag else 0

    return jsonify({
        "ok": True,
        "het_adatok": het_adatok,
        "atlag": atlag,
        "legjobb": legjobb,
        "legrosszabb": legrosszabb,
        "het_kat": {k: round(v, 3) for k, v in het_kat.items()},
        "elozo_atlag": elozo_atlag,
        "valtozas_pct": valtozas_pct,
        "napi_cel": d.get("napi_cel", CEL_DEF),
    })

@app.route("/api/kihivas_kuldes", methods=["POST"])
@login_required
def api_kihivas_kuldes():
    """Kihívás küldése másik felhasználónak."""
    u = current_user()
    data = request.json or {}
    cel_nev = (data.get("cel") or "").strip()
    napok = int(data.get("napok", 7))
    if not cel_nev:
        return jsonify({"ok": False, "hiba": "Adj meg egy felhasználónevet!"})
    users = users_betolt()
    if cel_nev not in users:
        return jsonify({"ok": False, "hiba": "Nincs ilyen felhasználó!"})
    if cel_nev == u:
        return jsonify({"ok": False, "hiba": "Magad nem hívhatod ki!"})
    # Kihívás mentése célfelhasználó adataiba (külön mezőben, nem a naplóban)
    cel_d = betolt(cel_nev)
    kihivasok = cel_d.setdefault("kihivasok", {})
    kihivas_kulcs = f"{u}__{TODAY}"
    kihivasok[kihivas_kulcs] = {
        "kuldo": u,
        "napok": napok,
        "datum": TODAY,
        "elfogadva": False,
    }
    ment(cel_d, cel_nev)
    return jsonify({"ok": True, "uzenet": f"Kihívás elküldve {cel_nev}-nek!"})

@app.route("/api/kihivasok")
@login_required
def api_kihivasok():
    """A bejelentkezett felhasználóhoz érkezett kihívások listája."""
    u = current_user()
    d = betolt(u)
    kihivasok = d.get("kihivasok", {})
    lista = [
        {"kulcs": k, **v}
        for k, v in sorted(kihivasok.items(), key=lambda kv: kv[1].get("datum", ""), reverse=True)
    ]
    return jsonify({"ok": True, "lista": lista})

@app.route("/api/kihivas_elfogad", methods=["POST"])
@login_required
def api_kihivas_elfogad():
    """Kihívás elfogadása vagy elutasítása."""
    u = current_user()
    d = betolt(u)
    data = request.json or {}
    kulcs = data.get("kulcs")
    akcio = data.get("akcio", "elfogad")
    kihivasok = d.get("kihivasok", {})
    if kulcs not in kihivasok:
        return jsonify({"ok": False, "hiba": "Nincs ilyen kihívás!"})
    if akcio == "elfogad":
        kihivasok[kulcs]["elfogadva"] = True
    else:
        del kihivasok[kulcs]
    ment(d, u)
    return jsonify({"ok": True})

@app.route("/api/ranglista_json")
@login_required
def api_ranglista_json():
    """JSON ranglista API."""
    db = get_db()
    rows = db.execute(
        "SELECT u.nev, ud.xp, ud.streak, ud.legjobb_streak, ud.bejegyzesek "
        "FROM users u LEFT JOIN user_data ud ON u.nev=ud.nev "
        "ORDER BY ud.xp DESC LIMIT 50"
    ).fetchall()
    lista = []
    for row in rows:
        try:
            xp = row["xp"] or 0
            rang_nev, _, rang_prog, _ = rang_info(xp)
            bej = json.loads(row["bejegyzesek"] or "{}")
            utolso7 = sorted(bej.items())[-7:]
            atlag7 = round(sum(nap_osszeg(v) for _, v in utolso7) / len(utolso7), 2) if utolso7 else 0
            utolso30 = sorted(bej.items())[-30:]
            atlag30 = round(sum(nap_osszeg(v) for _, v in utolso30) / len(utolso30), 2) if utolso30 else 0
            lista.append({
                "nev": row["nev"],
                "xp": xp,
                "rang": rang_nev,
                "streak": row["streak"] or 0,
                "legjobb_streak": row["legjobb_streak"] or 0,
                "atlag7": atlag7,
                "atlag30": atlag30,
                "napok_szama": len(bej),
            })
        except Exception:
            pass
    return jsonify({"ok": True, "lista": lista, "sajat": current_user()})

@app.route("/api/co2_cimke", methods=["POST"])
@login_required
def api_co2_cimke():
    """CO₂ 'tápanyag-jelölés' egy termékhez/aktivitáshoz."""
    data = request.json or {}
    kulcsok = data.get("kulcsok", {})
    osszeg = sum(
        float(v) * FAKTOROK[k][1]
        for k, v in kulcsok.items()
        if k in FAKTOROK and v
    )
    reszletek = []
    for k, v in kulcsok.items():
        if k in FAKTOROK and float(v or 0):
            nev, f, kat = FAKTOROK[k]
            co2 = round(float(v) * f, 3)
            reszletek.append({"nev": nev, "ertek": float(v), "faktor": f, "co2": co2, "kat": kat})
    return jsonify({
        "ok": True,
        "osszeg": round(osszeg, 3),
        "reszletek": reszletek,
        "eu_pct": round(osszeg / EU_NAP * 100, 1),
        "par_pct": round(osszeg / PAR_NAP * 100, 1),
        "fenn_pct": round(osszeg / FENN_NAP * 100, 1),
    })

@app.route("/api/tippek_ai", methods=["POST"])
@login_required
def api_tippek_ai():
    """Személyre szabott tippek az elmúlt 7 nap adatai alapján."""
    u = current_user()
    d = betolt(u)
    today = date.today()
    het_napok = [str(today - timedelta(days=i)) for i in range(7)]
    het_kat = {k: 0.0 for k in KATEGORIAK}
    napok_szama = 0
    for nap in het_napok:
        bej = d["bejegyzesek"].get(nap, {})
        if bej:
            napok_szama += 1
            for k, v in kat_osszeg(bej).items():
                het_kat[k] = het_kat.get(k, 0) + v
    if napok_szama:
        for k in het_kat:
            het_kat[k] = round(het_kat[k] / napok_szama, 2)

    # Legrosszabb kategória meghatározása
    legrosszabb_kat = max(het_kat, key=lambda k: het_kat[k]) if any(v > 0 for v in het_kat.values()) else None

    tippek_kategoriak = {
        "🚗 Közlekedés": [
            "🚲 Próbálj biciklivel vagy tömegközlekedéssel közlekedni a rövid utakon!",
            "🚗 Carpooling: ossd meg az autóutat kollégákkal – felére csökkented az emissziót!",
            "⚡ Elektromos autó bérléssel kipróbálhatod, mennyit spórolnál évente.",
        ],
        "🏠 Otthon & Energia": [
            "🌡️ 1 fokkal lejjebb venni a fűtést ~5-10% energiát takarít meg.",
            "💡 LED izzókra váltva 75%-kal csökkented a világítási fogyasztást.",
            "🔌 Készenléti eszközök kikapcsolásával évi 10-15 kg CO₂-t spórolhatsz.",
        ],
        "🍽️ Élelmiszer": [
            "🥗 Heti 1 húsmentes nap ~330 kg CO₂-t spórol évente!",
            "🛒 Helyi piacon vásárolt zöldség CO₂ lábnyoma 4x kisebb az importáltnál.",
            "🍱 Ételpazarlás elkerülésével évi 300+ kg CO₂-t lehet megtakarítani.",
        ],
        "🛍️ Vásárlás": [
            "♻️ Második kézből vásárolva akár 80%-ot is megspórolhatsz az emissziókból!",
            "📦 Összevont csomagrendelések drasztikusan csökkentik a szállítási emissziót.",
            "👗 Slow fashion: 1 minőségi darab > 5 olcsó, hamar eldobott termék.",
        ],
        "♻️ Hulladék": [
            "🗑️ Szelektív gyűjtéssel és komposztálással akár -2 kg/nap elérhető!",
            "🌿 Komposztálás: a konyhai hulladék 50%-a így nullára csökkenthető.",
        ],
        "🌳 Kompenzáció": [
            "🌱 Egy fa 20+ éves élete alatt ~1000 kg CO₂-t köt meg – ültess egyet!",
            "☀️ Napelemes töltő vásárlásával napi szinten is kompenzálhatsz.",
        ],
    }

    tippek_list = []
    if legrosszabb_kat and legrosszabb_kat in tippek_kategoriak:
        tippek_list.extend(tippek_kategoriak[legrosszabb_kat])
    # Kiegészítő általános tippek
    for kat, t_list in tippek_kategoriak.items():
        if kat != legrosszabb_kat:
            tippek_list.extend(t_list[:1])

    random.shuffle(tippek_list)

    return jsonify({
        "ok": True,
        "tippek": tippek_list[:6],
        "legrosszabb_kat": legrosszabb_kat,
        "het_kat_atlag": het_kat,
        "napok_szama": napok_szama,
    })

@app.route("/api/osszehasonlitas")
@login_required
def api_osszehasonlitas():
    """Összehasonlítás a top felhasználókkal."""
    u = current_user()
    d = betolt(u)
    db = get_db()
    rows = db.execute(
        "SELECT u.nev, ud.bejegyzesek FROM users u "
        "LEFT JOIN user_data ud ON u.nev=ud.nev"
    ).fetchall()

    sajat_bej = d["bejegyzesek"]
    sajat_utolso30 = sorted(sajat_bej.items())[-30:]
    sajat_atlag = round(sum(nap_osszeg(v) for _, v in sajat_utolso30) / len(sajat_utolso30), 3) if sajat_utolso30 else 0

    osszes_atlag = []
    for row in rows:
        try:
            bej = json.loads(row["bejegyzesek"] or "{}")
            utolso30 = sorted(bej.items())[-30:]
            if utolso30:
                atlag = sum(nap_osszeg(v) for _, v in utolso30) / len(utolso30)
                osszes_atlag.append(atlag)
        except Exception:
            pass

    osszes_atlag.sort()
    n = len(osszes_atlag)
    percentilis = round((sum(1 for x in osszes_atlag if x > sajat_atlag) / n * 100), 0) if n > 1 else 50

    return jsonify({
        "ok": True,
        "sajat_atlag": sajat_atlag,
        "kozosseg_atlag": round(sum(osszes_atlag)/n, 3) if osszes_atlag else 0,
        "kozosseg_legjobb": round(osszes_atlag[0], 3) if osszes_atlag else 0,
        "percentilis": percentilis,
        "felhasznalok_szama": n,
    })

