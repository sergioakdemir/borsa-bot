"""Hetzner Cloud haftalik snapshot otomasyonu.

Her Pazar 23:00 (cron) sunucunun snapshot'ini alir.
Snapshot adi: borsa-botu-haftalik-YYYY-MM-DD

- Sunucu ID otomatik: Hetzner metadata servisi (fallback: HETZNER_SERVER_ID).
- Token: HETZNER_API_TOKEN (.env). Cloud API, Read & Write yetkili olmali.
- Opsiyonel rotasyon: HETZNER_SNAPSHOT_KEEP=N -> en yeni N disindaki ETIKETLI
  (otomasyon=borsa-botu-haftalik) snapshot'lar silinir. Varsayilan: silme yok.
- --dry : sadece plani yazar, API cagrisi yapmaz (token gerekmez).
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")
_API = "https://api.hetzner.cloud/v1"
_META_ID = "http://169.254.169.254/hetzner/v1/metadata/instance-id"
_LABEL_KEY = "otomasyon"
_LABEL_VAL = "borsa-botu-haftalik"


def _load_dotenv():
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _server_id():
    sid = os.environ.get("HETZNER_SERVER_ID")
    if sid:
        return sid.strip()
    try:
        r = requests.get(_META_ID, timeout=5)
        if r.ok and r.text.strip().isdigit():
            return r.text.strip()
    except Exception:
        pass
    return None


def _headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def create_snapshot(token, server_id, name):
    return requests.post(
        f"{_API}/servers/{server_id}/actions/create_image",
        headers=_headers(token),
        json={"type": "snapshot", "description": name,
              "labels": {_LABEL_KEY: _LABEL_VAL}},
        timeout=30)


def rotate(token, keep, stamp):
    r = requests.get(f"{_API}/images", headers=_headers(token),
                     params={"type": "snapshot",
                             "label_selector": f"{_LABEL_KEY}={_LABEL_VAL}"}, timeout=30)
    if not r.ok:
        print(f"[{stamp}] Rotasyon: snapshot listesi alinamadi (HTTP {r.status_code}).")
        return
    imgs = sorted(r.json().get("images", []),
                  key=lambda x: x.get("created", ""), reverse=True)
    for img in imgs[keep:]:
        d = requests.delete(f"{_API}/images/{img['id']}", headers=_headers(token), timeout=30)
        print(f"[{stamp}] Eski snapshot silindi: {img.get('description')} "
              f"(id={img['id']}) -> HTTP {d.status_code}")


def main():
    _load_dotenv()
    dry = "--dry" in sys.argv
    now = datetime.now(_TZ)
    stamp = now.strftime("%Y-%m-%d %H:%M")
    name = f"borsa-botu-haftalik-{now:%Y-%m-%d}"

    server_id = _server_id()
    if not server_id:
        print(f"[{stamp}] HATA: Sunucu ID bulunamadi (metadata + HETZNER_SERVER_ID yok).")
        return 1

    print(f"[{stamp}] Plan: sunucu={server_id} snapshot adi='{name}'")
    if dry:
        print(f"[{stamp}] DRY-RUN: API cagrisi yapilmadi.")
        return 0

    token = os.environ.get("HETZNER_API_TOKEN")
    if not token:
        print(f"[{stamp}] HATA: HETZNER_API_TOKEN ayarli degil (.env).")
        return 1

    r = create_snapshot(token, server_id, name)
    if r.status_code in (200, 201):
        data = r.json()
        img = data.get("image", {})
        act = data.get("action", {})
        print(f"[{stamp}] OK: snapshot olusturuluyor. image_id={img.get('id')} "
              f"ad='{name}' action_durum={act.get('status')}")
    else:
        print(f"[{stamp}] HATA: snapshot olusturulamadi (HTTP {r.status_code}): {r.text[:300]}")
        return 1

    keep = int(os.environ.get("HETZNER_SNAPSHOT_KEEP", "0") or 0)
    if keep > 0:
        rotate(token, keep, stamp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
