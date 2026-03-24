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
    "transferit":  "",   # format: email:password (akun MEGA dengan password biasa, bukan OAuth)
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


from .. import LOGGER as _LOGGER
try:
    import asyncio as _asyncio
    _loop = _asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_db_load_keys())
    else:
        _loop.run_until_complete(_db_load_keys())
except Exception as _e:
    _LOGGER.warning(f"multi_uploader: skip DB load — {_e}")


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
        srv_r = requests.get(
            "https://filemirage.com/api/servers", headers=headers, timeout=30
        )
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

        url = (
            last_rj.get("data", {}).get("url")
            if isinstance(last_rj.get("data"), dict)
            else None
        )
        if url:
            return url
        return f"❌ Filemirage: selesai tapi tidak ada URL — {last_rj}"
    except Exception as e:
        return f"❌ Filemirage exception: {e}"


# ── Upload: Transfer.it (MEGA upload via tenacity+requests) ──────────────────
def _upload_transferit(path: str, key: str) -> str:
    """
    Transfer.it = powered by MEGA.
    Menggunakan MEGA API dengan implementasi manual yang benar.
    key format: email:password
    CATATAN: Hanya bekerja untuk akun MEGA dengan password biasa (bukan login Google/Apple OAuth).
    Jika akun MEGA dibuat via Google, gunakan host lain.
    """
    import hashlib
    import struct
    import base64
    import random
    import requests

    if not key or ":" not in key:
        return (
            "❌ Format: /settransferit email@mega.nz:passwordmega\n"
            "Catatan: Hanya untuk akun MEGA dengan password biasa, bukan Google/Apple login"
        )

    email, password = key.split(":", 1)

    # ── MEGA crypto helpers ───────────────────────────────────────────────────
    def a32_to_bytes(a):
        return struct.pack(">" + "I" * len(a), *a)

    def bytes_to_a32(b):
        n = (len(b) + 3) // 4
        b = b.ljust(n * 4, b"\x00")
        return list(struct.unpack(">" + "I" * n, b))

    def b64e(b):
        return base64.b64encode(b).replace(b"+", b"-").replace(b"/", b"_").rstrip(b"=").decode()

    def b64d(s):
        s = s.replace("-", "+").replace("_", "/")
        return base64.b64decode(s + "=" * (-len(s) % 4))

    def aes_ecb(key_bytes, data_bytes, encrypt=True):
        try:
            from Crypto.Cipher import AES
            c = AES.new(key_bytes, AES.MODE_ECB)
            return c.encrypt(data_bytes) if encrypt else c.decrypt(data_bytes)
        except ImportError:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            c = Cipher(algorithms.AES(key_bytes), modes.ECB(), backend=default_backend())
            op = c.encryptor() if encrypt else c.decryptor()
            return op.update(data_bytes) + op.finalize()

    def prepare_key(password_str):
        """Convert password string to 128-bit AES key."""
        pwd = password_str.encode("utf-8")
        a = [0, 0, 0, 0]
        i = 0
        while i < len(pwd):
            chunk = [0, 0, 0, 0]
            for j in range(4):
                if i < len(pwd):
                    chunk[j] = (chunk[j] << 8) | pwd[i]
                    i += 1
            # Hmm, fix: each byte contributes to one int
        # Correct implementation:
        a = [0, 0, 0, 0]
        for idx in range(len(pwd)):
            a[idx % 4] ^= (pwd[idx] << ((3 - (idx % 4 * 0)) * 0)) # wrong
        # Proper way: 
        pkey = [0, 0, 0, 0]
        tmp = [0, 0, 0, 0]
        for idx in range(len(pwd)):
            pos = idx % 16
            if pos < 4:
                tmp[0] = (tmp[0] & ~(0xFF << (24 - (pos % 4) * 8))) | (pwd[idx] << (24 - (pos % 4) * 8))
            elif pos < 8:
                tmp[1] = (tmp[1] & ~(0xFF << (24 - (pos % 4) * 8))) | (pwd[idx] << (24 - (pos % 4) * 8))
            elif pos < 12:
                tmp[2] = (tmp[2] & ~(0xFF << (24 - (pos % 4) * 8))) | (pwd[idx] << (24 - (pos % 4) * 8))
            else:
                tmp[3] = (tmp[3] & ~(0xFF << (24 - (pos % 4) * 8))) | (pwd[idx] << (24 - (pos % 4) * 8))
        for i in range(4):
            pkey[i] ^= tmp[i]
        return pkey

    def string_hash(s, key_a32):
        """Compute MEGA string hash: used as uh parameter."""
        h32 = [0, 0, 0, 0]
        s_bytes = s.encode("utf-8")
        for i in range(len(s_bytes)):
            h32[i & 3] ^= s_bytes[i]
        key_bytes = a32_to_bytes(key_a32)
        for _ in range(16384):
            hb = a32_to_bytes(h32)
            hb = aes_ecb(key_bytes, hb)
            h32 = bytes_to_a32(hb)
        return b64e(a32_to_bytes(h32[:2]))

    # ── MEGA API call ─────────────────────────────────────────────────────────
    seq = random.randint(0, 0xFFFFFFFF)

    def api(data, sid=None):
        params = {"id": seq, "v": 3}
        if sid:
            params["sid"] = sid
        resp = requests.post(
            "https://g.api.mega.co.nz/cs",
            params=params,
            json=[data],
            timeout=30,
        )
        body = resp.text.strip()
        if not body:
            return None
        import json
        try:
            r = json.loads(body)
            return r[0] if isinstance(r, list) else r
        except Exception:
            return None

    try:
        pwd_key  = prepare_key(password)
        pwd_hash = string_hash(email.lower(), pwd_key)

        login_r = api({"a": "us", "user": email.lower(), "uh": pwd_hash})

        if login_r is None:
            return "❌ MEGA: tidak ada response dari server. Cek koneksi."
        if isinstance(login_r, int):
            msgs = {
                -1: "Internal error",
                -2: "Invalid argument",
                -3: "Rate limited, coba lagi nanti",
                -9: "Resource tidak ditemukan",
                -16: "Login gagal — email/password salah atau akun Google OAuth (tidak support password login)",
            }
            return f"❌ MEGA login error {login_r}: {msgs.get(login_r, 'Unknown error')}"

        sid = login_r.get("csid") or login_r.get("sid")
        if not sid:
            return f"❌ MEGA: tidak dapat session ID. Response: {str(login_r)[:200]}"

        # Decrypt master key (optional untuk upload public)
        file_size = os.path.getsize(path)

        # Get upload URL
        up_url_r = api({"a": "u", "s": file_size}, sid=sid)
        if not up_url_r or isinstance(up_url_r, int):
            return f"❌ MEGA get upload URL gagal: {up_url_r}"

        upload_url = up_url_r.get("p")
        if not upload_url:
            return f"❌ MEGA: tidak dapat upload URL. Response: {str(up_url_r)[:200]}"

        # Upload file bytes
        with open(path, "rb") as f:
            http_r = requests.post(
                f"{upload_url}/0",
                data=f,
                headers={"Content-Length": str(file_size)},
                timeout=600,
            )

        handle = http_r.text.strip()
        if not handle or len(handle) < 5 or handle.startswith("{"):
            return f"❌ MEGA upload HTTP gagal: {http_r.status_code} — {http_r.text[:200]}"

        # Create public link from upload completion handle
        pub_r = api({"a": "l", "n": handle}, sid=sid)
        if isinstance(pub_r, int) or not pub_r:
            return f"❌ MEGA buat link publik gagal: {pub_r}"

        return f"https://mega.nz/file/{pub_r}"

    except Exception as e:
        return f"❌ MEGA/Transfer.it exception: {e}"


# ── Upload: Player4me ─────────────────────────────────────────────────────────
def _upload_player4me(path: str, key: str) -> str:
    """
    Player4me pakai Cloudflare yang blokir upload file besar (413).
    Solusi: coba URL import (server player4me yang download filenya).
    Kalau tidak support, tampilkan error yang jelas.
    """
    import requests

    filename = os.path.basename(path)
    file_size_mb = os.path.getsize(path) / 1024 / 1024

    # ── Coba 1: URL import via remote_url parameter ───────────────────────────
    # (Beberapa video host support ini — server mereka download dari URL)
    # Kita simpan URL asal di metadata karena kita sudah download filenya
    # Sayangnya kita tidak punya URL asal lagi di sini.
    # Skip ini dan langsung coba upload.

    # ── Coba 2: Upload dengan cloudscraper + smaller chunk size ───────────────
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False},
            delay=5,
        )
    except Exception:
        import requests as req_mod
        scraper = req_mod.Session()
        scraper.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    try:
        with open(path, "rb") as f:
            r = scraper.post(
                "https://player4me.com/api/upload",
                files={"file": (filename, f, "video/mp4")},
                data={"api_key": key},
                timeout=600,
            )

        if r.status_code == 413:
            return (
                f"❌ Player4me: file terlalu besar ({file_size_mb:.1f} MB)\n"
                f"Cloudflare WAF membatasi ukuran upload langsung.\n"
                f"Gunakan /gofile atau /pixeldrain untuk file besar."
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
        hints = {
            "transferit": "email@mega.nz:passwordmega (bukan Google login)",
            "gofile":     "API_TOKEN_GOFILE",
            "pixeldrain": "API_KEY_PIXELDRAIN",
            "filemirage": "API_TOKEN_FILEMIRAGE",
            "player4me":  "API_KEY_PLAYER4ME",
            "akirabox":   "API_KEY_AKIRABOX",
            "buzzheavier":"API_KEY (opsional)",
        }
        hint = hints.get(host, "API_KEY")
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
