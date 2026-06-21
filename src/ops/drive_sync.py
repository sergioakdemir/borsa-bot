"""Google Drive'a dosya yukleme (audit raporu yedegi).

Servis hesabi (service account) ile calisir. .env:
  GOOGLE_DRIVE_CREDENTIALS_JSON : servis hesabi JSON'u (dosya YOLU veya inline JSON)
  GOOGLE_DRIVE_FOLDER_ID        : hedef klasor ID'si

Baglanti/kimlik/kutuphane yoksa SESSIZCE atlar (hata firlatmaz) — cagiran taraf
upload() doner ve True/False ile sonucu ogrenir, ama exception almaz.
"""
import json
import os
from pathlib import Path

_SCOPES = ["https://www.googleapis.com/auth/drive"]


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


def _credentials():
    """Servis hesabi kimligi (env'den dosya yolu veya inline JSON). Yoksa None."""
    raw = os.environ.get("GOOGLE_DRIVE_CREDENTIALS_JSON")
    if not raw:
        return None
    try:
        from google.oauth2 import service_account
    except ImportError:
        return None
    try:
        p = Path(raw)
        if p.exists():
            return service_account.Credentials.from_service_account_file(
                str(p), scopes=_SCOPES)
        info = json.loads(raw)            # inline JSON
        return service_account.Credentials.from_service_account_info(
            info, scopes=_SCOPES)
    except Exception:
        return None


def available() -> bool:
    """Kimlik + klasor + kutuphane hazir mi?"""
    _load_dotenv()
    return bool(_credentials() and os.environ.get("GOOGLE_DRIVE_FOLDER_ID"))


def upload(local_path, drive_name=None, verbose: bool = False) -> bool:
    """local_path'teki dosyayi Drive klasorune yukler. Ayni adli dosya varsa
    gunceller. Basarisizsa/yapilandirma yoksa SESSIZCE False doner."""
    _load_dotenv()
    try:
        local_path = Path(local_path)
        if not local_path.exists():
            return False
        folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
        creds = _credentials()
        if not creds or not folder_id:
            if verbose:
                print("  [drive] yapilandirma yok (creds/folder) - atlandi")
            return False
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload

        service = build("drive", "v3", credentials=creds, cache_discovery=False)
        name = drive_name or local_path.name
        media = MediaFileUpload(str(local_path), mimetype="text/plain", resumable=False)

        # Ayni adli dosya klasorde varsa guncelle (yoksa yeni olustur).
        # supportsAllDrives/includeItemsFromAllDrives: Shared Drive uyumu (servis
        # hesabinin kendi deposu olmadigindan yedek bir Shared Drive klasorune yazilir).
        q = (f"name = '{name}' and '{folder_id}' in parents and trashed = false")
        existing = service.files().list(
            q=q, fields="files(id)", pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
        files = existing.get("files", [])
        if files:
            service.files().update(fileId=files[0]["id"], media_body=media,
                                   supportsAllDrives=True).execute()
            if verbose:
                print(f"  [drive] guncellendi: {name}")
        else:
            service.files().create(
                body={"name": name, "parents": [folder_id]},
                media_body=media, fields="id", supportsAllDrives=True).execute()
            if verbose:
                print(f"  [drive] yuklendi: {name}")
        return True
    except Exception as e:
        if verbose:
            print(f"  [drive] atlandi: {type(e).__name__}: {str(e)[:80]}")
        return False


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "logs/latest_audit.log"
    print("available:", available())
    print("upload:", upload(path, verbose=True))
