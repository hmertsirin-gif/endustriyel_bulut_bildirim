#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Endüstriyel Bulut Panel İzleyici
---------------------------------
panel.endustriyelbulut.com sitesindeki parametreleri periyodik olarak
çeker, config.json içindeki eşik kurallarını ve dorse basıncı rejim
değişimi kurallarını kontrol eder, ihlal varsa Telegram'a bildirim gönderir.

Kullanım:
    python monitor.py

Kurulum:
    pip install -r requirements.txt
    config.example.json dosyasini config.json olarak kopyala, doldur.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

# --------------------------------------------------------------------------
# Sabitler
# --------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

DEGER_RE = re.compile(r"^\s*(-?[\d.,]+)\s*([^\d\s].*)?$")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config():
    if not CONFIG_PATH.exists():
        print(
            f"HATA: {CONFIG_PATH} bulunamadı.\n"
            f"config.example.json dosyasını config.json olarak kopyalayıp "
            f"kendi bilgilerinle doldurman gerekiyor."
        )
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Loglama
# --------------------------------------------------------------------------

def setup_logging(log_path):
    logger = logging.getLogger("monitor")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# --------------------------------------------------------------------------
# Veritabanı
# --------------------------------------------------------------------------

def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            istasyon TEXT NOT NULL,
            parametre TEXT NOT NULL,
            deger REAL,
            birim TEXT,
            site_zaman TEXT,
            fetched_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_lookup "
        "ON readings (istasyon, parametre, fetched_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_state (
            rule_key TEXT PRIMARY KEY,
            active INTEGER NOT NULL DEFAULT 0,
            last_change_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS regime_alert_log (
            rule_key TEXT NOT NULL,
            saat_damgasi TEXT NOT NULL,
            PRIMARY KEY (rule_key, saat_damgasi)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS heartbeat (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_sent_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS info_notice_log (
            rule_key TEXT PRIMARY KEY,
            last_sent_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def save_readings(conn, rows, fetched_at):
    conn.executemany(
        """
        INSERT INTO readings (istasyon, parametre, deger, birim, site_zaman, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (r["istasyon"], r["parametre"], r["deger"], r["birim"], r["site_zaman"], fetched_at)
            for r in rows
        ],
    )
    conn.commit()


def get_alert_active(conn, rule_key):
    cur = conn.execute("SELECT active FROM alert_state WHERE rule_key = ?", (rule_key,))
    row = cur.fetchone()
    return bool(row[0]) if row else False


def set_alert_active(conn, rule_key, active):
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO alert_state (rule_key, active, last_change_at)
        VALUES (?, ?, ?)
        ON CONFLICT(rule_key) DO UPDATE SET active = excluded.active, last_change_at = excluded.last_change_at
        """,
        (rule_key, int(active), now),
    )
    conn.commit()


def regime_already_alerted(conn, rule_key, saat_damgasi):
    cur = conn.execute(
        "SELECT 1 FROM regime_alert_log WHERE rule_key = ? AND saat_damgasi = ?",
        (rule_key, saat_damgasi),
    )
    return cur.fetchone() is not None


def mark_regime_alerted(conn, rule_key, saat_damgasi):
    conn.execute(
        "INSERT OR IGNORE INTO regime_alert_log (rule_key, saat_damgasi) VALUES (?, ?)",
        (rule_key, saat_damgasi),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Site ile iletişim
# --------------------------------------------------------------------------

def new_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        }
    )
    return s


def apply_cookie_login(session, config, logger):
    """
    config icinde site.session_cookie (veya SESSION_COOKIE ortam degiskeni)
    doluysa, otomatik login POST akisini tamamen atlayip bu cookie'yi
    dogrudan kullanir.

    Bu, bazi bulut/CI sunucularinin (GitHub Actions gibi) IP'lerinin site
    tarafindan bot/yabanci trafik olarak algilanip farkli/eksik bir login
    sayfasi gormesi durumunda kullanilir: kullanici kendi normal tarayicisindan
    BIR KEZ giris yapar, o oturumun cookie degerini alir, config/secrets
    icine koyar. Site her istekte oturum suresini kendiliginden uzattigi
    icin (session sliding expiry), duzenli araliklarla (ornegin 5 dk'da bir)
    istek atildigi surece bu oturum pratikte hic dusmez.

    Basariliysa True, session_cookie tanimli degilse False doner.
    """
    cookie_str = config.get("site", {}).get("session_cookie", "").strip()
    if not cookie_str:
        return False

    count = 0
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            session.cookies.set(k.strip(), v.strip())
            count += 1

    if count == 0:
        logger.warning("session_cookie tanimli ama gecerli bir cookie parse edilemedi.")
        return False

    logger.info(f"Elle alinan session cookie ile giris yapildi ({count} cookie), login POST akisi atlandi.")
    return True


def login(session, config, logger):
    base = config["site"]["base_url"]
    login_page_url = f"{base}/auth/login"

    resp = session.get(login_page_url, timeout=20)

    m = re.search(r'name="csrf-token"\s+content="([^"]+)"', resp.text)
    if not m:
        logger.error("Login sayfasından csrf-token bulunamadı.")
        logger.error(f"TEŞHİS -> HTTP durum kodu: {resp.status_code}")
        logger.error(f"TEŞHİS -> Gerçek URL (yönlendirme olduysa değişir): {resp.url}")
        logger.error(f"TEŞHİS -> Content-Type: {resp.headers.get('content-type')}")
        logger.error(f"TEŞHİS -> Cevap uzunluğu: {len(resp.text)} karakter")
        logger.error(f"TEŞHİS -> Cevabın ilk 500 karakteri:\n{resp.text[:500]}")
        return False
    token = m.group(1)

    payload = {
        "companyId": config["site"]["company_id"],
        "companyEmail": config["site"]["email"],
        "companyPassword": config["site"]["password"],
        "_token": token,
    }

    resp = session.post(login_page_url, data=payload, timeout=20, allow_redirects=True)

    # Basarili giriste site /dashboard veya benzeri bir sayfaya yonlendirir.
    # Basarisiz giriste tekrar /auth/login'e doner.
    final_url = resp.url
    if "/auth/login" in final_url:
        logger.error("Login başarısız görünüyor (tekrar login sayfasına yönlendirildi).")
        return False

    logger.info("Login başarılı.")
    return True


def fetch_parameters(session, config, logger):
    base = config["site"]["base_url"]
    url = f"{base}/data/parameters"

    params = {
        "draw": 1,
        "start": 0,
        "length": 500,  # toplam parametre sayisindan (62) fazla, hepsini tek seferde al
        "search[value]": "",
        "search[regex]": "false",
        "order[0][column]": 0,
        "order[0][dir]": "asc",
    }
    # DataTables kolon tanimlari - site bunlari zorunlu kilabiliyor
    columns = ["adi", "isim", "pid", "grup1", "savedata_sure", "sondata", "sondatatarih", "durum", "islem"]
    for i, col in enumerate(columns):
        params[f"columns[{i}][data]"] = col
        params[f"columns[{i}][name]"] = col
        params[f"columns[{i}][searchable]"] = "true"
        params[f"columns[{i}][orderable]"] = "true"
        params[f"columns[{i}][search][value]"] = ""
        params[f"columns[{i}][search][regex]"] = "false"

    resp = session.get(url, params=params, timeout=30, headers={"X-Requested-With": "XMLHttpRequest"})

    if resp.status_code != 200 or "text/html" in resp.headers.get("content-type", ""):
        logger.warning("Parametre isteği beklenmedik cevap döndürdü, session düşmüş/geçersiz olabilir.")
        logger.warning(f"TEŞHİS -> HTTP durum kodu: {resp.status_code}, Content-Type: {resp.headers.get('content-type')}")
        logger.warning(f"TEŞHİS -> Cevabın ilk 300 karakteri: {resp.text[:300]}")
        return None

    try:
        data = resp.json()
    except ValueError:
        logger.warning("Parametre cevabı JSON değil, session düşmüş olabilir.")
        return None

    rows = []
    for item in data.get("data", []):
        deger, birim = parse_deger(item.get("sondata", ""))
        rows.append(
            {
                "istasyon": item.get("adi", "").strip(),
                "parametre": item.get("isim", "").strip(),
                "deger": deger,
                "birim": birim,
                "site_zaman": item.get("sondatatarih", ""),
                "parametre_id": item.get("id"),
            }
        )
    return rows


def parse_deger(raw):
    """'4,95 Bar' -> (4.95, 'Bar')   '1.001 M3' -> (1001.0, 'M3')"""
    if not raw:
        return None, None
    m = DEGER_RE.match(raw.strip())
    if not m:
        return None, None
    num_str = m.group(1)
    unit = (m.group(2) or "").strip()

    if "," in num_str and "." in num_str:
        # 1.001,50 gibi: nokta binlik ayraci, virgul ondalik
        num_str = num_str.replace(".", "").replace(",", ".")
    elif "," in num_str:
        # 4,95 -> ondalik virgul
        num_str = num_str.replace(",", ".")
    elif num_str.count(".") > 1:
        # 3.640.662 gibi: hepsi binlik ayiraci
        num_str = num_str.replace(".", "")
    elif "." in num_str:
        # Tek nokta varsa: 3 haneli ise muhtemelen binlik ayiraci (1.001 M3)
        parts = num_str.split(".")
        if len(parts[-1]) == 3 and len(parts) == 2:
            num_str = num_str.replace(".", "")
        # aksi halde oldugu gibi birak (ondalik nokta)

    try:
        return float(num_str), unit
    except ValueError:
        return None, unit


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------

def send_telegram(config, text, logger):
    token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=15)
        if resp.status_code != 200:
            logger.error(f"Telegram gönderim hatası: {resp.status_code} {resp.text}")
    except requests.RequestException as e:
        logger.error(f"Telegram gönderim istisnası: {e}")


# --------------------------------------------------------------------------
# Eşik kontrolü
# --------------------------------------------------------------------------

def check_thresholds(conn, config, readings, logger):
    rules = config.get("esik_kurallari", [])
    if not rules:
        return

    by_key = {}
    for r in readings:
        by_key.setdefault((r["istasyon"], r["parametre"]), []).append(r)

    for rule in rules:
        if not rule.get("aktif", True):
            continue

        hedef_istasyon = rule.get("istasyon", "").strip()
        hedef_parametre = rule["parametre"].strip()

        matches = []
        for (ist, prm), rows in by_key.items():
            if prm.lower() != hedef_parametre.lower():
                continue
            if hedef_istasyon and ist.lower() != hedef_istasyon.lower():
                continue
            matches.extend(rows)

        if not matches:
            logger.warning(
                f"TEŞHİS -> Kural '{rule['aciklama']}' hiçbir veriyle eşleşmedi "
                f"(istasyon='{hedef_istasyon or 'TÜMÜ'}', parametre='{hedef_parametre}'). "
                f"İsim yazımını kontrol et (Türkçe karakterler dahil)."
            )

        for r in matches:
            if r["deger"] is None:
                continue

            rule_key = f"esik::{rule['aciklama']}::{r['istasyon']}"
            ihlal = False
            if rule["kosul"] == "altinda" and r["deger"] < rule["deger"]:
                ihlal = True
            elif rule["kosul"] == "ustunde" and r["deger"] > rule["deger"]:
                ihlal = True

            logger.info(
                f"TEŞHİS -> {r['istasyon']} | {r['parametre']} = {r['deger']} {r['birim']} "
                f"| kural: {rule['kosul']} {rule['deger']} | ihlal: {ihlal}"
            )

            was_active = get_alert_active(conn, rule_key)
            logger.info(f"TEŞHİS -> alarm_durumu (daha once aktif miydi?): {was_active}")

            if ihlal and was_active:
                logger.info(
                    "TEŞHİS -> İhlal var AMA bu alarm zaten aktif (daha önce bildirilmiş), "
                    "spam yapmamak için TEKRAR mesaj GÖNDERİLMİYOR. "
                    "(Eşiği değiştirdiysen ve yine de mesaj bekliyorsan, bu normal davranış "
                    "değildir - bana haber ver, alarm durumunu sıfırlayalım.)"
                )

            if ihlal and not was_active:
                msg = (
                    f"🔴 UYARI: {r['istasyon']}\n"
                    f"{r['parametre']}: {r['deger']} {r['birim']}\n"
                    f"Kural: {rule['aciklama']} (eşik: {rule['kosul']} {rule['deger']})\n"
                    f"Site zamanı: {r['site_zaman']}"
                )
                logger.warning(msg.replace("\n", " | "))
                send_telegram(config, msg, logger)
                set_alert_active(conn, rule_key, True)

            elif not ihlal and was_active:
                msg = (
                    f"🟢 NORMALE DÖNDÜ: {r['istasyon']}\n"
                    f"{r['parametre']}: {r['deger']} {r['birim']}\n"
                    f"Kural: {rule['aciklama']}"
                )
                logger.info(msg.replace("\n", " | "))
                send_telegram(config, msg, logger)
                set_alert_active(conn, rule_key, False)


# --------------------------------------------------------------------------
# Rejim değişimi kontrolü (ör. dorse basıncı)
# --------------------------------------------------------------------------

def check_regime(conn, config, logger):
    rules = config.get("rejim_izleme", [])
    if not rules:
        return

    now = datetime.now()
    current_hour_bucket = now.strftime("%Y-%m-%d %H:00")

    for rule in rules:
        if not rule.get("aktif", True):
            continue

        hedef_istasyon = rule.get("istasyon", "").strip()
        hedef_parametre = rule["parametre"].strip()
        n_saat = rule.get("karsilastirma_saat_sayisi", 3)
        carpan_esigi = rule.get("carpan_esigi", 2.5)
        min_mutlak_fark = rule.get("minimum_mutlak_fark", 0)

        q = """
            SELECT istasyon, fetched_at, deger
            FROM readings
            WHERE parametre = ?
              AND fetched_at >= ?
              AND deger IS NOT NULL
        """
        args = [hedef_parametre, (now - timedelta(hours=n_saat + 1)).isoformat()]
        if hedef_istasyon:
            q += " AND istasyon = ?"
            args.append(hedef_istasyon)
        q += " ORDER BY istasyon, fetched_at ASC"

        cur = conn.execute(q, args)
        rows = cur.fetchall()

        by_station = {}
        for istasyon, fetched_at, deger in rows:
            by_station.setdefault(istasyon, []).append((fetched_at, deger))

        for istasyon, points in by_station.items():
            hourly = _hourly_last_values(points)
            if len(hourly) < n_saat + 1:
                continue  # yeterli gecmis yok henuz

            hours_sorted = sorted(hourly.keys())
            last_hours = hours_sorted[-(n_saat + 1):]
            diffs = []
            for i in range(1, len(last_hours)):
                prev_v = hourly[last_hours[i - 1]]
                cur_v = hourly[last_hours[i]]
                diffs.append(abs(cur_v - prev_v))

            if len(diffs) < 2:
                continue

            last_diff = diffs[-1]
            onceki_diffs = diffs[:-1]
            onceki_ort = sum(onceki_diffs) / len(onceki_diffs)

            rule_key = f"rejim::{rule['aciklama']}::{istasyon}"

            rejim_degisti = False
            if last_diff >= min_mutlak_fark and onceki_ort > 0:
                if last_diff >= onceki_ort * carpan_esigi:
                    rejim_degisti = True
            elif last_diff >= min_mutlak_fark and onceki_ort == 0:
                rejim_degisti = True

            if rejim_degisti and not regime_already_alerted(conn, rule_key, current_hour_bucket):
                msg = (
                    f"⚠️ REJİM DEĞİŞİMİ: {istasyon}\n"
                    f"{hedef_parametre}\n"
                    f"Son saatteki değişim: {last_diff:.2f}\n"
                    f"Önceki {len(onceki_diffs)} saat ortalama değişim: {onceki_ort:.2f}\n"
                    f"Kural: {rule['aciklama']}"
                )
                logger.warning(msg.replace("\n", " | "))
                send_telegram(config, msg, logger)
                mark_regime_alerted(conn, rule_key, current_hour_bucket)


def _hourly_last_values(points):
    """[(fetched_at_iso, deger), ...] -> {saat_bucket: o saatteki son deger}"""
    hourly = {}
    for fetched_at, deger in points:
        try:
            dt = datetime.fromisoformat(fetched_at)
        except ValueError:
            continue
        bucket = dt.strftime("%Y-%m-%d %H:00")
        hourly[bucket] = deger  # son deger kalir (siralama ASC oldugu icin)
    return hourly


# --------------------------------------------------------------------------
# Heartbeat ("sistem calisiyor") mesaji
# --------------------------------------------------------------------------

def get_last_heartbeat(conn):
    cur = conn.execute("SELECT last_sent_at FROM heartbeat WHERE id = 1")
    row = cur.fetchone()
    return row[0] if row else None


def set_last_heartbeat(conn, iso_time):
    conn.execute(
        """
        INSERT INTO heartbeat (id, last_sent_at) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET last_sent_at = excluded.last_sent_at
        """,
        (iso_time,),
    )
    conn.commit()


def check_heartbeat(conn, config, logger):
    """
    Alarm olsun olmasin, duzenli araliklarla (varsayilan 60 dakika) 'sistem
    calisiyor' mesaji gonderir. Boylece kullanici sessizligin 'her sey
    normal' mi yoksa 'sistem durdu' mu oldugunu ayirt edebilir.
    """
    hb_config = config.get("heartbeat", {})
    if not hb_config.get("aktif", True):
        return

    interval_minutes = hb_config.get("interval_dakika", 60)
    now = datetime.now()

    last_str = get_last_heartbeat(conn)
    last_dt = None
    if last_str:
        try:
            last_dt = datetime.fromisoformat(last_str)
        except ValueError:
            last_dt = None

    if last_dt and (now - last_dt) < timedelta(minutes=interval_minutes):
        return  # henuz zamani gelmedi

    cur = conn.execute("SELECT rule_key FROM alert_state WHERE active = 1")
    active_rules = [row[0] for row in cur.fetchall()]

    if active_rules:
        satirlar = []
        for rk in active_rules:
            parts = rk.split("::")
            if len(parts) == 3:
                _, aciklama, istasyon = parts
                satirlar.append(f"  • {istasyon} — {aciklama}")
            else:
                satirlar.append(f"  • {rk}")
        detay = "\n".join(satirlar)
        msg = (
            f"✅ Sistem çalışıyor ({now.strftime('%d-%m-%Y %H:%M')})\n"
            f"Şu an {len(active_rules)} aktif alarm var:\n{detay}"
        )
    else:
        msg = (
            f"✅ Sistem çalışıyor ({now.strftime('%d-%m-%Y %H:%M')})\n"
            f"Her şey normal, aktif alarm yok."
        )

    send_telegram(config, msg, logger)
    set_last_heartbeat(conn, now.isoformat(timespec="seconds"))
    logger.info("Saatlik durum mesajı (heartbeat) gönderildi.")


# --------------------------------------------------------------------------
# Dorse takibi: aktif peronu belirleme + bosalma tahmini
# --------------------------------------------------------------------------

def get_last_notice(conn, rule_key):
    cur = conn.execute("SELECT last_sent_at FROM info_notice_log WHERE rule_key = ?", (rule_key,))
    row = cur.fetchone()
    return row[0] if row else None


def set_last_notice(conn, rule_key, iso_time):
    conn.execute(
        """
        INSERT INTO info_notice_log (rule_key, last_sent_at) VALUES (?, ?)
        ON CONFLICT(rule_key) DO UPDATE SET last_sent_at = excluded.last_sent_at
        """,
        (rule_key, iso_time),
    )
    conn.commit()


def check_dorse_takip(conn, config, logger):
    """
    Bir istasyonun birden fazla dorse peronu (ornegin '1.Dorse Basınç' ve
    '2.Dorse Basınç') arasindan HANGISININ su an aktif (kullanimda) oldugunu,
    gecmiste EN SON hangisinin 'dolu esigi'ne (varsayilan 180 Bar) ulastigina
    bakarak belirler - vana ic kacagi yuzunden iki peronun da benzer dusuk
    degerler gostermesi durumunda bile dogru peronu ayirt edebilmek icin.

    Aktif peronun son dolumdan bu yana ortalama dusus hizini (Bar/saat)
    hesaplayip, 'bosaltma esigi'ne (varsayilan 20 Bar) ne zaman inecegini
    tahmin eder ve periyodik olarak (varsayilan 60 dakikada bir) bilgi
    mesaji gonderir. Aktif peron degistiginde (digeri yeniden doldugunda)
    otomatik olarak yeni aktif peronu takip etmeye baslar.
    """
    rules = config.get("dorse_takip", [])
    if not rules:
        return

    now = datetime.now()

    for rule in rules:
        if not rule.get("aktif", True):
            continue

        istasyon = rule["istasyon"]
        peronlar = rule["peronlar"]
        dolu_esigi = rule.get("dolu_esigi", 180)
        bosaltma_esigi = rule.get("bosaltma_esigi", 20)
        interval_dk = rule.get("bildirim_araligi_dakika", 60)
        min_veri_saat = rule.get("min_veri_saat", 1)

        rule_key = f"dorse::{rule['aciklama']}::{istasyon}"

        last_str = get_last_notice(conn, rule_key)
        if last_str:
            try:
                last_dt = datetime.fromisoformat(last_str)
                if (now - last_dt) < timedelta(minutes=interval_dk):
                    continue
            except ValueError:
                pass

        histories = {}
        fill_times = {}
        current_values = {}

        for peron in peronlar:
            cur = conn.execute(
                """
                SELECT fetched_at, deger FROM readings
                WHERE istasyon = ? AND parametre = ? AND deger IS NOT NULL
                ORDER BY fetched_at ASC
                """,
                (istasyon, peron),
            )
            rows = cur.fetchall()
            histories[peron] = rows
            if rows:
                current_values[peron] = rows[-1][1]

            last_fill = None
            prev_deger = None
            for fetched_at, deger in rows:
                # 'dolum ani' = degerin esigin ALTINDAN USTUNE gectigi an (yukseliş kenari).
                # Sadece 'hala yuksek' olmasi degil, YENİ doldurulmus olmasi onemli.
                if deger >= dolu_esigi and (prev_deger is None or prev_deger < dolu_esigi):
                    last_fill = fetched_at
                prev_deger = deger
            fill_times[peron] = last_fill

        candidates = [(p, t) for p, t in fill_times.items() if t is not None]
        belirsiz = False
        if candidates:
            active_peron = max(candidates, key=lambda x: x[1])[0]
        elif current_values:
            active_peron = max(current_values, key=current_values.get)
            belirsiz = True
        else:
            logger.info(f"TEŞHİS -> Dorse takip '{rule['aciklama']}': henüz hiç veri yok.")
            continue

        rows = histories[active_peron]
        if belirsiz:
            window_rows = rows
        else:
            fill_time = fill_times[active_peron]
            window_rows = [r for r in rows if r[0] >= fill_time]

        belirsizlik_notu = ""
        if belirsiz:
            belirsizlik_notu = (
                f"\n⚠️ Henüz {dolu_esigi} Bar civarında bir dolum gözlemlenmedi, "
                f"aktif peron tahmini şu an en yüksek basınçlı olana göre yapılıyor."
            )

        if len(window_rows) < 2:
            logger.info(f"TEŞHİS -> Dorse takip '{rule['aciklama']}': yeterli veri yok, bekleniyor.")
            set_last_notice(conn, rule_key, now.isoformat(timespec="seconds"))
            continue

        ilk_zaman = datetime.fromisoformat(window_rows[0][0])
        ilk_deger = window_rows[0][1]
        son_zaman = datetime.fromisoformat(window_rows[-1][0])
        son_deger = window_rows[-1][1]

        gecen_saat = (son_zaman - ilk_zaman).total_seconds() / 3600
        if gecen_saat < min_veri_saat:
            logger.info(
                f"TEŞHİS -> Dorse takip '{rule['aciklama']}': henüz {gecen_saat:.2f} saatlik veri var, "
                f"en az {min_veri_saat} saat bekleniyor."
            )
            set_last_notice(conn, rule_key, now.isoformat(timespec="seconds"))
            continue

        hiz = (ilk_deger - son_deger) / gecen_saat if gecen_saat > 0 else 0

        if hiz <= 0:
            msg = (
                f"🛢️ Dorse Takibi: {istasyon}\n"
                f"Aktif peron (tahmini): {active_peron} — şu an {son_deger:.2f} Bar\n"
                f"Son {gecen_saat:.1f} saatte anlamlı bir düşüş gözlenmedi, "
                f"bitiş süresi şu an hesaplanamıyor."
                f"{belirsizlik_notu}"
            )
        elif son_deger <= bosaltma_esigi:
            msg = (
                f"🛢️ Dorse Takibi: {istasyon}\n"
                f"Aktif peron: {active_peron} — şu an {son_deger:.2f} Bar\n"
                f"⚠️ Zaten {bosaltma_esigi} Bar eşiğinde/altında — değişim zamanı gelmiş olabilir."
                f"{belirsizlik_notu}"
            )
        else:
            kalan_saat = (son_deger - bosaltma_esigi) / hiz
            tahmini_zaman = now + timedelta(hours=kalan_saat)
            msg = (
                f"🛢️ Dorse Takibi: {istasyon}\n"
                f"Aktif peron: {active_peron} — şu an {son_deger:.2f} Bar\n"
                f"Ortalama tüketim hızı: {hiz:.2f} Bar/saat\n"
                f"Tahmini {bosaltma_esigi} Bar'a düşme süresi: ~{kalan_saat:.1f} saat "
                f"(tahmini zaman: {tahmini_zaman.strftime('%d-%m-%Y %H:%M')})"
                f"{belirsizlik_notu}"
            )

        send_telegram(config, msg, logger)
        set_last_notice(conn, rule_key, now.isoformat(timespec="seconds"))
        logger.info(f"Dorse takip bildirimi gönderildi: {rule['aciklama']} -> aktif peron: {active_peron}")


# --------------------------------------------------------------------------
# Ana döngü
# --------------------------------------------------------------------------

def apply_env_overrides(config):
    """
    GitHub Actions gibi ortamlarda sifre/token gibi hassas bilgileri
    repo'ya hic yazmadan, ortam degiskenlerinden (Secrets) enjekte etmek icin.
    Bir ortam degiskeni tanimliysa config.json'daki karsiligini ezer.
    """
    env_map = {
        "SITE_BASE_URL": ("site", "base_url"),
        "SITE_COMPANY_ID": ("site", "company_id"),
        "SITE_EMAIL": ("site", "email"),
        "SITE_PASSWORD": ("site", "password"),
        "SITE_SESSION_COOKIE": ("site", "session_cookie"),
        "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
        "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    }
    for env_key, (section, field) in env_map.items():
        val = os.environ.get(env_key)
        if val:
            config.setdefault(section, {})[field] = val
    return config


def run_single_check(session, config, conn, logger):
    """
    Tek bir cek-kaydet-kontrol-et dongusu. Hem surekli calisan lokal modda
    (dongu icinde tekrar tekrar), hem de --once modunda (GitHub Actions,
    tek seferlik) kullanilir. Basarili olursa True, basarisiz olursa
    (ornegin session dusmus) False doner.
    """
    readings = fetch_parameters(session, config, logger)

    if readings is None:
        return False

    fetched_at = datetime.now().isoformat(timespec="seconds")
    save_readings(conn, readings, fetched_at)

    check_thresholds(conn, config, readings, logger)
    check_regime(conn, config, logger)
    check_dorse_takip(conn, config, logger)
    check_heartbeat(conn, config, logger)

    logger.info(f"{len(readings)} parametre okundu ve kontrol edildi.")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Tek kontrol yapip cik (GitHub Actions gibi zamanlanmis/cron ortamlari icin). "
             "Bu bayrak verilmezse script surekli calisan bir dongu olarak calisir (lokal PC icin).",
    )
    args = parser.parse_args()

    config = load_config()
    config = apply_env_overrides(config)

    logger = setup_logging(config.get("log_path", "monitor.log"))
    conn = init_db(config.get("database_path", "history.db"))

    session = new_session()
    using_cookie_mode = apply_cookie_login(session, config, logger)
    if not using_cookie_mode:
        if not login(session, config, logger):
            logger.error("İlk girişte başarısız olundu, çıkılıyor.")
            sys.exit(1)

    def try_reauth():
        """Cookie modundaysa normal login zaten calismiyor demektir (site
        bu ortamin IP'sini engelliyor) - tekrar denemenin anlami yok, sadece
        acik bir uyari verip cikariz. Normal moddaysa tekrar login dener."""
        if using_cookie_mode:
            logger.error(
                "Oturum (session_cookie) gecersiz/suresi dolmus gorunuyor. "
                "Tekrar login POST denemesi bu ortamda calismiyor (bilinen IP engeli). "
                "Kendi tarayicindan tekrar giris yapip yeni cookie degerini "
                "SITE_SESSION_COOKIE secret'ina guncellemen gerekiyor."
            )
            return False
        new_session_obj = new_session()
        if login(new_session_obj, config, logger):
            return new_session_obj
        return False

    if args.once:
        ok = run_single_check(session, config, conn, logger)
        if not ok:
            logger.warning("Veri alınamadı, yeniden giriş deneniyor.")
            result = try_reauth()
            if result:
                session = result
                ok = run_single_check(session, config, conn, logger)
        if not ok:
            logger.error("Tek seferlik kontrol başarısız oldu.")
            sys.exit(1)
        logger.info("Tek seferlik kontrol tamamlandı (--once modu).")
        return

    interval = config.get("polling_interval_seconds", 60)
    logger.info(f"İzleme başladı. Her {interval} saniyede bir kontrol edilecek.")

    consecutive_failures = 0

    while True:
        try:
            ok = run_single_check(session, config, conn, logger)
            if not ok:
                consecutive_failures += 1
                logger.warning(f"Veri alınamadı ({consecutive_failures}. deneme). Yeniden giriş deneniyor.")
                result = try_reauth()
                if result:
                    session = result
                    consecutive_failures = 0
            else:
                consecutive_failures = 0

        except requests.RequestException as e:
            logger.error(f"Ağ hatası: {e}")
        except Exception as e:
            logger.exception(f"Beklenmeyen hata: {e}")

        time.sleep(interval)


if __name__ == "__main__":
    main()
