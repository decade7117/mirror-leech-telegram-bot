import math
import os
import urllib.parse
from asyncio import to_thread

from pyrogram.filters import command
from pyrogram.handlers import MessageHandler

from ..core.telegram_manager import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.filters import CustomFilters

# ── In-memory API key store ───────────────────────────────────────────────────
_API_KEYS: dict = {
    "gofile":      "",
    "pixeldrain":  "",
    "transferit":  "",   # format: email:password (akun MEGA)
    "filemirage":  "",
    "buzzheavier": "",
    "player4me":   "",
    "akirabox":    "",
}

HOST_LIST     = list(_API_KEYS.keys())
SET_HOST_LIST = [f"set{h}" for h in HOST_LIST]
TEMP_DIR      = "/tmp/multi_uploader_dl"


# ── MongoDB persistence ───────────────────────────────────────────────────────
async def _db_load_keys():
    try:
        from ..core.config_manager import Config
        import motor.motor_asyncio
        client = motor.motor_asyncio.AsyncIOMotorClient(Config.DATABASE_URL)
        db     = client.mltb
        doc    = await db.multi_uploader_keys.find_one({"_id": "api_keys"})
        if doc:
            for host in HOST_LIST:
                if doc.get(host):
                    _API_KEYS[host] = doc[host]
        client.close()
    except Exception as e:
        from .. import LOGGER
        LOGGER.warning(f"multi_uploader: gagal load keys dari DB — {e}")


async def _db_save_keys():
    try:
        from ..core.config_manager import Config
        import motor.motor_asyncio
        client = motor.motor_asyncio.AsyncIOMotorClient(Config.DATABASE_URL)
        db     = client.mltb
        await db.multi_uploader_keys.update_one(
            {"_id": "api_keys"},
            {"$set": {k: v for k, v in _API_KEYS.items()}},
            upsert=True,
        )
        client.close()
    except Exception as e:
        from .. import LOGGER
        LOGGER.warning(f"multi_uploader: gagal simpan keys ke DB — {e}")


import asyncio as _asyncio
try:
    loop = _asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(_db_load_keys())
    else:
        loop.run_until_complete(_db_load_keys())
except Exception:
    pass


# ── Helpers ───────────────────────────────────────────────────────────────────
def _safe_json(r):
    try:
        text = r.text.strip()
        if not text:
            return None
        import json
        return json.loads(text)
    except Exception:
        return None


def _download_file(url: str, dest: str):
    import requests
    try:
        with requests.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
        return True, None
    except Exception as e:
        return False, str(e)


# ── Upload: Gofile ────────────────────────────────────────────────────────────
def _upload_gofile(path: str, key: str) -> str:
    import requests
    try:
        server = (
            requests.get("https://api.gofile.io/servers", timeout=30)
            .json()["data"]["servers"][0]["name"]
        )
        with open(path, "rb") as f:
            data = {"token": key} if key else {}
            r = requests.post(
                f"https://{server}.gofile.io/contents/uploadfile",
                files={"file": (os.path.basename(path), f)},
                data=data,
                timeout=600,
            )
        rj = _safe_json(r)
        if rj and rj.get("status") == "ok":
            return rj["data"]["downloadPage"]
        return f"❌ Gofile error: {r.text[:200]}"
    except Exception as e:
        return f"❌ Gofile exception: {e}"


# ── Upload: Pixeldrain ────────────────────────────────────────────────────────
def _upload_pixeldrain(path: str, key: str) -> str:
    import requests
    try:
        auth = ("", key) if key else None
        with open(path, "rb") as f:
            r = requests.post(
                "https://pixeldrain.com/api/file",
                files={"file": (os.path.basename(path), f)},
                auth=auth,
                timeout=600,
            )
        rj = _safe_json(r)
        if rj and rj.get("id"):
            return f"https://pixeldrain.com/u/{rj['id']}"
        return f"❌ Pixeldrain error: {r.text[:200]}"
    except Exception as e:
        return f"❌ Pixeldrain exception: {e}"


# ── Upload: Buzzheavier ───────────────────────────────────────────────────────
def _upload_buzzheavier(path: str, key: str) -> str:
    import requests
    try:
        fname   = urllib.parse.quote(os.path.basename(path), safe="")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        with open(path, "rb") as f:
            r = requests.put(
                f"https://w.buzzheavier.com/{fname}",
                data=f,
                headers=headers,
                timeout=600,
            )
        rj = _safe_json(r)
        if rj:
            data = rj.get("data", {})
            url  = data.get("url")
            if not url and data.get("id"):
                url = f"https://buzzheavier.com/{data['id']}"
            if url:
                return url
        return f"❌ Buzzheavier HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return f"❌ Buzzheavier exception: {e}"


# ── Upload: Filemirage ────────────────────────────────────────────────────────
def _upload_filemirage(path: str, key: str) -> str:
    import requests

    CHUNK_SIZE = 100 * 1024 * 1024
    headers    = {"Authorization": f"Bearer {key}"} if key else {}

    try:
        srv_r = requests.get("https://filemirage.com/api/servers", headers=headers, timeout=30)
        srv_j = _safe_json(srv_r)
        if not srv_j or not srv_j.get("success"):
            return f"❌ Filemirage get server gagal: {srv_r.text[:300]}"

        server       = srv_j["data"]["server"].rstrip("/")
        upload_id    = srv_j["data"]["upload_id"]
        filename     = os.path.basename(path)
        file_size    = os.path.getsize(path)
        total_chunks = max(1, math.ceil(file_size / CHUNK_SIZE))
        upload_url   = f"{server}/upload.php"
        last_rj      = {}

        with open(path, "rb") as fh:
            for i in range(total_chunks):
                chunk_data = fh.read(CHUNK_SIZE)
                is_last    = (i == total_chunks - 1)
                up_r = requests.post(
                    upload_url,
                    headers=headers,
                    files={"file": (filename, chunk_data, "application/octet-stream")},
                    data={
                        "filename":     filename,
                        "upload_id":    upload_id,
                        "chunk_number": str(i),
                        "total_chunks": str(total_chunks),
                    },
                    timeout=600,
                )
                if up_r.status_code not in (200, 201):
                    return f"❌ Filemirage chunk {i}: HTTP {up_r.status_code}\n{up_r.text[:300]}"
                if not is_last:
                    continue
                up_j = _safe_json(up_r)
                if up_j:
                    last_rj = up_j

        url = last_rj.get("data", {}).get("url") if isinstance(last_rj.get("data"), dict) else None
        if url:
            return url
        return f"❌ Filemirage: selesai tapi tidak ada URL — {last_rj}"
    except Exception as e:
        return f"❌ Filemirage exception: {e}"


# ── Upload: Transfer.it via MEGA ─────────────────────────────────────────────
def _upload_transferit(path: str, key: str) -> str:
    """
    Transfer.it = powered by MEGA.
    Upload langsung ke MEGA, hasilnya link MEGA yang bisa diakses publik.
    key format: email:password (akun MEGA kamu)
    Gunakan: /settransferit email@mega.com:passwordmega
    """
    if not key or ":" not in key:
        return (
            "❌ Transfer.it butuh akun MEGA\n"
            "Gunakan: /settransferit email@mega.com:passwordmega"
        )

    email, password = key.split(":", 1)

    try:
        from mega import Mega
        mega = Mega()
        m    = mega.login(email, password)
        file = m.upload(path)
        link = m.get_upload_link(file)
        return link
    except ImportError:
        return "❌ Library mega.py belum terinstall. Pastikan sudah rebuild Docker dengan --build"
    except Exception as e:
        return f"❌ MEGA upload exception: {e}"


# ── Upload: Player4me (via cloudscraper bypass Cloudflare) ───────────────────
def _upload_player4me(path: str, key: str) -> str:
    """
    Player4me pakai Cloudflare yang blokir upload file besar via requests biasa.
    Solusi: pakai cloudscraper untuk bypass CF, dan coba remote URL import dulu.
    """
    try:
        import cloudscraper
    except ImportError:
        import requests as cloudscraper

    try:
        scraper  = cloudscraper.create_scraper() if hasattr(cloudscraper, 'create_scraper') else cloudscraper.Session()
        filename = os.path.basename(path)

        # Coba remote upload dulu (kirim URL, server player4me yang download)
        # Ini menghindari 413 sama sekali
        # (Fitur ini ada di beberapa platform, mungkin tidak ada di player4me)

        # Upload langsung dengan cloudscraper
        with open(path, "rb") as f:
            r = scraper.post(
                "https://player4me.com/api/upload",
                files={"file": (filename, f, "application/octet-stream")},
                data={"api_key": key},
                timeout=600,
            )

        rj = _safe_json(r)
        if rj and r.status_code == 200:
            return (
                rj.get("data", {}).get("url")
                or rj.get("url")
                or rj.get("link")
                or str(rj)
            )
        return f"❌ Player4me HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return f"❌ Player4me exception: {e}"


# ── Upload: Akirabox ──────────────────────────────────────────────────────────
def _upload_akirabox(path: str, key: str) -> str:
    import requests
    try:
        with open(path, "rb") as f:
            r = requests.post(
                "https://akirabox.com/api/upload",
                files={"file": (os.path.basename(path), f)},
                data={"api_key": key},
                timeout=600,
            )
        rj = _safe_json(r)
        if rj and r.status_code == 200:
            return rj.get("data", {}).get("url") or rj.get("url") or rj.get("link") or str(rj)
        return f"❌ Akirabox HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return f"❌ Akirabox exception: {e}"


# ── Routing ───────────────────────────────────────────────────────────────────
_UPLOAD_FUNCS = {
    "gofile":      _upload_gofile,
    "pixeldrain":  _upload_pixeldrain,
    "buzzheavier": _upload_buzzheavier,
    "filemirage":  _upload_filemirage,
    "transferit":  _upload_transferit,
    "player4me":   _upload_player4me,
    "akirabox":    _upload_akirabox,
}


# ── Telegram handlers ─────────────────────────────────────────────────────────
@new_task
async def set_api_key_cmd(_, message):
    parts = message.text.strip().split(maxsplit=1)
    host  = parts[0].lstrip("/").lower().replace("set", "", 1)
    if len(parts) < 2:
        if host == "transferit":
            hint = "email@mega.com:passwordmega"
        else:
            hint = "API_KEY_ANDA"
        await message.reply(f"Gunakan: <code>/set{host} {hint}</code>")
        return
    _API_KEYS[host] = parts[1].strip()
    await _db_save_keys()
    await message.reply(f"✅ Credentials <b>{host}</b> berhasil disimpan ke database!")


@new_task
async def multi_mirror_cmd(_, message):
    parts = message.text.strip().split(maxsplit=1)
    host  = parts[0].lstrip("/").lower()

    if len(parts) < 2:
        await message.reply(
            f"🔗 Kirim link file yang ingin diupload ke <b>{host}</b>:\n"
            f"<code>/{host} https://example.com/file.mkv</code>"
        )
        return

    url = parts[1].strip()
    if not url.startswith(("http://", "https://")):
        await message.reply("❌ URL tidak valid, harus diawali https://")
        return

    fname = url.split("/")[-1].split("?")[0].strip()
    if not fname or len(fname) < 3:
        fname = "file_download"

    os.makedirs(TEMP_DIR, exist_ok=True)
    dest = os.path.join(TEMP_DIR, fname)

    status_msg = await message.reply(f"⬇️ Mengunduh file…\n<code>{url}</code>")
    ok, err    = await to_thread(_download_file, url, dest)

    if not ok:
        await status_msg.edit(f"❌ Gagal mengunduh:\n<code>{err}</code>")
        return

    size_kb  = os.path.getsize(dest) // 1024
    size_str = f"{size_kb // 1024} MB" if size_kb > 1024 else f"{size_kb} KB"
    await status_msg.edit(f"⬆️ File diunduh ({size_str}). Mengupload ke <b>{host}</b>…")

    key  = _API_KEYS.get(host, "")
    func = _UPLOAD_FUNCS.get(host)

    if not func:
        await status_msg.edit(f"❌ Host tidak dikenal: <code>{host}</code>")
        return

    link = await to_thread(func, dest, key)

    try:
        os.remove(dest)
    except Exception:
        pass

    await status_msg.edit(f"✅ <b>Upload ke {host.capitalize()} selesai!</b>\n\n🔗 {link}")


# ── Register handlers ─────────────────────────────────────────────────────────
TgClient.bot.add_handler(
    MessageHandler(set_api_key_cmd, filters=command(SET_HOST_LIST) & CustomFilters.sudo)
)
TgClient.bot.add_handler(
    MessageHandler(multi_mirror_cmd, filters=command(HOST_LIST) & CustomFilters.authorized)
)
