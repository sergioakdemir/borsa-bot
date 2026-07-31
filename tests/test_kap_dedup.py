"""KAP bildirimi dedup penceresi: gun-asiri tekrar kesiliyor mu? (izole DB, ag yok)

31 Tem 2026 denetiminde bulundu: dedup yalniz alert_levels_today ile yapiliyordu,
yani pencere GUN ICI idi ve gece yarisi sifirlaniyordu. _kap_key bildirim basina
KARARLI bir hash uretti gi icin ayni bildirim ertesi gun yeniden "gorulmemis"
sayilip tekrar gonderildi. Olcum (20-31 Tem): 40 bildirim 3+ ayri gunde, 9'u
4 gun ust uste (PGSUS / ISCTR / RTALB 28->31 Tem).

Calistir:  ./venv/bin/python tests/test_kap_dedup.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.db import database as db

# ONEMLI: gercek data/borsa.db'ye DOKUNMA. get_conn() modul-global DB_PATH'i
# HER CAGRIDA okudugu icin burada yeniden atamak yeterli.
_tmp = tempfile.mkdtemp(prefix="kapdedup-")
_gercek = db.DB_PATH
db.DB_PATH = Path(_tmp) / "test.db"
assert db.DB_PATH != _gercek

SONUC = []


def kontrol(ad, kosul, detay=""):
    SONUC.append(kosul)
    print(f"[{'GECTI' if kosul else 'KALDI'}] {ad} {detay}")


bugun = date(2026, 7, 31)
KEY = "KAP:abc123def456"
TKR = "PGSUS"

# --- 4 gun once gonderilmis bir bildirim (28 Tem) -------------------------
db.record_alert(TKR, (bugun - timedelta(days=3)).isoformat(), KEY, 1.0)

kontrol("T1 3 gun once gonderilen bildirim 5g penceresinde GORULUYOR",
        db.alert_seen_since(TKR, KEY, bugun=bugun.isoformat()) is True,
        "-> tekrar gonderilmeyecek")

kontrol("T1b eski gun-ici kontrol ayni bildirimi GORMUYOR (arizanin sebebi)",
        KEY not in db.alert_levels_today(TKR, bugun.isoformat()),
        "-> eski kodda bugun tekrar giderdi")

# --- pencere disi: 9 gun once ---------------------------------------------
ESKI = "KAP:eskiaaaaaaa1"
db.record_alert(TKR, (bugun - timedelta(days=9)).isoformat(), ESKI, 1.0)
kontrol("T2 9 gun once gonderilen bildirim pencere DISI (tekrar gidebilir)",
        db.alert_seen_since(TKR, ESKI, bugun=bugun.isoformat()) is False)

# --- sinir: tam 5 gun once (pencere ICI) ve 5 gun+1 (DISI) ----------------
S1, S2 = "KAP:sinir00000a", "KAP:sinir00000b"
db.record_alert(TKR, (bugun - timedelta(days=4)).isoformat(), S1, 1.0)   # 5 gunluk pencere ici
db.record_alert(TKR, (bugun - timedelta(days=5)).isoformat(), S2, 1.0)   # disi
kontrol("T3 sinir: 4 gun once ICERIDE",
        db.alert_seen_since(TKR, S1, bugun=bugun.isoformat()) is True)
kontrol("T3b sinir: 5 gun once DISARIDA",
        db.alert_seen_since(TKR, S2, bugun=bugun.isoformat()) is False)

# --- ayni gun tekrari da bu pencere kesiyor (ayri kontrole gerek yok) -----
AYNI = "KAP:aynigun0001"
db.record_alert(TKR, bugun.isoformat(), AYNI, 1.0)
kontrol("T4 ayni gun tekrari da kesiliyor",
        db.alert_seen_since(TKR, AYNI, bugun=bugun.isoformat()) is True)

# --- baska hisse / baska bildirim etkilenmiyor ---------------------------
kontrol("T5 baska hisse ayni anahtarla etkilenmiyor",
        db.alert_seen_since("ISCTR", KEY, bugun=bugun.isoformat()) is False)
kontrol("T5b ayni hisse baska bildirim etkilenmiyor",
        db.alert_seen_since(TKR, "KAP:bambaska0001", bugun=bugun.isoformat()) is False)

print(f"\n{sum(SONUC)}/{len(SONUC)} test gecti")
sys.exit(0 if all(SONUC) else 1)
