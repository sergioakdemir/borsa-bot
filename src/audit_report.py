"""Gunluk denetim (audit) raporu.

O gunun ozetini logs/latest_audit.log dosyasina yazar:
  - bugun verilen kararlar (decisions)
  - paper trading / model portfoy durumu
  - acik uyari sayilari
Ardindan dosyayi Google Drive'a yedekler (borsa-bot-audit-YYYY-MM-DD.log).
Drive baglantisi yoksa sessizce atlanir.

Calistir: python -m src.audit_report
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TZ = ZoneInfo("Europe/Istanbul")
ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "logs" / "latest_audit.log"


def build_report() -> str:
    from src.db import database as db
    now = datetime.now(_TZ)
    today = now.date().isoformat()
    lines = [f"BORSA-BOT AUDIT RAPORU — {now:%Y-%m-%d %H:%M:%S}",
             "=" * 52, ""]

    # Bugunku kararlar
    try:
        rows = [r for r in db.list_decisions(limit=200) if r.get("tarih") == today]
    except Exception:
        rows = []
    lines.append(f"BUGUNKU KARARLAR ({len(rows)}):")
    if rows:
        for r in rows:
            lines.append(f"  {r.get('ticker'):7} {r.get('karar'):12} "
                         f"puan={r.get('puan')} risk={r.get('risk')} "
                         f"sonuc={r.get('sonuc') or '-'}")
    else:
        lines.append("  (bugun karar yok)")
    lines.append("")

    # Model portfoy
    try:
        from src.portfolio import model
        s = model.summary()
        lines.append("MODEL PORTFOY:")
        lines.append(f"  toplam deger {s['toplam_deger_tl']:.0f} TL · "
                     f"getiri %{s['getiri_yuzde']} · acik {s['acik_sayisi']} · "
                     f"kapali {s['kapali_sayisi']} · BIST100 fark "
                     f"%{s.get('bist100_fark_yuzde')}")
    except Exception as e:
        lines.append(f"MODEL PORTFOY: alinamadi ({type(e).__name__})")
    lines.append("")

    # Paper trading
    try:
        from src.portfolio import paper
        p = paper.summary()
        lines.append(f"PAPER TRADING: islem {p['islem_sayisi']} · "
                     f"acik {p['acik_sayisi']} · kapali {p['kapali_sayisi']} · "
                     f"basari %{p.get('basari_orani_%')} · "
                     f"toplam k/z {p['toplam_kz_tl']:.0f} TL")
    except Exception as e:
        lines.append(f"PAPER TRADING: alinamadi ({type(e).__name__})")
    lines.append("")

    # Bugunku uyarilar
    try:
        alerts = db.alerts_between(today, today)
        lines.append(f"BUGUNKU UYARILAR: {len(alerts)}")
    except Exception:
        lines.append("BUGUNKU UYARILAR: alinamadi")

    # Eski audit.log'un son satirlari (varsa)
    eski = ROOT / "logs" / "audit.log"
    if eski.exists():
        try:
            tail = eski.read_text(encoding="utf-8").splitlines()[-15:]
            lines += ["", "SON AUDIT.LOG SATIRLARI:"]
            lines += [f"  {t}" for t in tail]
        except Exception:
            pass

    return "\n".join(lines) + "\n"


def run(verbose: bool = True) -> str:
    rapor = build_report()
    LATEST.parent.mkdir(exist_ok=True)
    LATEST.write_text(rapor, encoding="utf-8")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] audit raporu yazildi: {LATEST}")

    # Google Drive'a yedekle (yoksa sessizce atla)
    try:
        from src.ops import drive_sync
        today = datetime.now(_TZ).date().isoformat()
        ok = drive_sync.upload(LATEST, drive_name=f"borsa-bot-audit-{today}.log",
                               verbose=verbose)
        if verbose:
            print(f"  Drive yedek: {'OK' if ok else 'atlandi (yapilandirma yok/hata)'}")
    except Exception as e:
        if verbose:
            print(f"  Drive yedek atlandi: {type(e).__name__}")
    return rapor


if __name__ == "__main__":
    run()
