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
    "transferit":  "",
    "filemirage":  "",
    "buzzheavier": "",
    "player4me":   "",
    "akirabox":    "",
}

HOST_LIST     = list(_API_KEYS.keys())
SET_HOST_LIST = [f"set{h}" for h in HOST_LIST]
TEMP_DIR      = "/tmp/multi_uploader_dl"

# ── MongoDB persistence ───────────────────────────────────────────────────────
# Simpan/load API keys dari MongoDB agar tidak hilang saat docker rebuild

async def _db_load_keys():
    """Load API keys dari MongoDB saat bot start."""
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
    """Simpan semua API keys ke MongoDB."""
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


# Load keys dari DB saat module pertama kali diimport
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


# ── Upload: Transfer.it ───────────────────────────────────────────────────────
def _upload_transferit(path: str, key: str) -> str:
    """
    Transfer.it = Laravel. Coba 3 strategi login berurutan.
    key format: email:password
    """
    import re
    import requests
    from urllib.parse import unquote

    if not key or ":" not in key:
        return (
            "❌ Transfer.it butuh email:password\n"
            "Gunakan: /settransferit email@kamu.com:passwordkamu"
        )

    email, password = key.split(":", 1)

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })

    def extract_csrf(html, cookies):
        # 1. Cookie XSRF-TOKEN (Laravel standard)
        tok = cookies.get("XSRF-TOKEN")
        if tok:
            return unquote(tok)
        # 2. hidden _token
        m = re.search(r'name=["\']_token["\'][^>]*value=["\']([^"\']{10,})["\']', html)
        if m:
            return m.group(1)
        # 3. meta csrf-token
        m = re.search(r'<meta[^>]+name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']', html)
        if m:
            return m.group(1)
        # 4. JS window._token atau csrfToken
        m = re.search(r'["\']?(?:_token|csrfToken)["\']?\s*[:=]\s*["\']([^"\']{10,})["\']', html)
        if m:
            return m.group(1)
        return ""

    try:
        # ── Strategi 1: Form-based login ─────────────────────────────────────
        login_page = session.get("https://transfer.it/login", timeout=30)
        csrf = extract_csrf(login_page.text, session.cookies)

        login_r = session.post(
            "https://transfer.it/login",
            data={"_token": csrf, "email": email, "password": password},
            headers={
                "Referer":      "https://transfer.it/login",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-CSRF-TOKEN": csrf,
                "Accept":       "text/html,application/xhtml+xml,*/*;q=0.8",
            },
            timeout=30,
            allow_redirects=True,
        )

        csrf2 = extract_csrf(login_r.text, session.cookies) or csrf

        logged_in = (
            "logout" in login_r.text.lower()
            or "/dashboard" in login_r.url
            or (login_r.url.rstrip("/") not in (
                "https://transfer.it/login",
                "https://transfer.it",
            ))
        )

        # ── Strategi 2: JSON API login (kalau form gagal) ─────────────────────
        if not logged_in:
            session2 = requests.Session()
            session2.headers.update(session.headers)
            api_login = session2.post(
                "https://transfer.it/api/login",
                json={"email": email, "password": password},
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=30,
            )
            aj = _safe_json(api_login)
            if aj and (aj.get("token") or aj.get("access_token") or aj.get("data")):
                token = aj.get("token") or aj.get("access_token") or aj.get("data", {}).get("token")
                # Upload via Bearer token
                filename = os.path.basename(path)
                with open(path, "rb") as f:
                    up_r = session2.post(
                        "https://transfer.it/api/upload",
                        files={"file": (filename, f)},
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        timeout=600,
                    )
                rj = _safe_json(up_r)
                if rj:
                    url = rj.get("url") or rj.get("link") or rj.get("download_url")
                    if url:
                        return url
                return f"❌ Transfer.it API login OK tapi upload gagal: {up_r.text[:300]}"

            # ── Strategi 3: Skip CSRF, langsung POST ─────────────────────────
            session3 = requests.Session()
            session3.headers.update(session.headers)
            session3.get("https://transfer.it/", timeout=30)  # set cookies
            login3 = session3.post(
                "https://transfer.it/login",
                data={"email": email, "password": password},
                headers={
                    "Referer": "https://transfer.it/login",
                    "Accept":  "application/json",
                },
                timeout=30,
                allow_redirects=True,
            )
            lj = _safe_json(login3)
            if lj and lj.get("success"):
                session = session3
                csrf2 = extract_csrf(login3.text, session3.cookies) or ""
                logged_in = True
            else:
                # Tampilkan info debug untuk diagnosa
                return (
                    f"❌ Transfer.it: semua strategi login gagal\n"
                    f"URL akhir: {login_r.url}\n"
                    f"Status: {login_r.status_code}\n"
                    f"Potongan HTML: {login_r.text[200:400]}"
                )

        # ── Upload setelah login berhasil ─────────────────────────────────────
        filename = os.path.basename(path)
        with open(path, "rb") as f:
            up_r = session.post(
                "https://transfer.it/upload",
                files={"file": (filename, f)},
                data={"_token": csrf2},
                headers={
                    "Referer":      "https://transfer.it/",
                    "X-CSRF-TOKEN": csrf2,
                    "Accept":       "application/json, */*",
                },
                timeout=600,
            )

        rj = _safe_json(up_r)
        if rj:
            url = rj.get("url") or rj.get("link") or rj.get("download_url")
            if url:
                return url
            return f"❌ Transfer.it JSON tapi tidak ada URL: {str(rj)[:300]}"

        m = re.search(r'https://transfer\.it/\S+', up_r.text)
        if m:
            return m.group(0).rstrip('",\'')

        return (
            f"❌ Transfer.it upload gagal\n"
            f"HTTP {up_r.status_code}\n"
            f"{up_r.text[:300]}"
        )

    except Exception as e:
        return f"❌ Transfer.it exception: {e}"


# ── Upload: Player4me ─────────────────────────────────────────────────────────
def _upload_player4me(path: str, key: str) -> str:
    import requests
    try:
        with open(path, "rb") as f:
            r = requests.post(
                "https://player4me.com/api/upload",
                files={"file": (os.path.basename(path), f)},
                data={"api_key": key},
                timeout=600,
            )
        rj = _safe_json(r)
        if rj and r.status_code == 200:
            return rj.get("data", {}).get("url") or rj.get("url") or rj.get("link") or str(rj)
        return f"❌ Player4me HTTP {r.status_code}: {r.text[:200]}"
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
        hint = "email:password" if host == "transferit" else "API_KEY_ANDA"
        await message.reply(f"Gunakan: <code>/set{host} {hint}</code>")
        return
    _API_KEYS[host] = parts[1].strip()
    await _db_save_keys()   # simpan ke MongoDB
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
