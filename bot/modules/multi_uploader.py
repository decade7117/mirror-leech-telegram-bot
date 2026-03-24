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


# Load keys saat startup — dijadwalkan ke event loop yang sudah running
from .. import LOGGER as _LOGGER
try:
    import asyncio as _asyncio
    _loop = _asyncio.get_event_loop()
    if _loop.is_running():
        _loop.create_task(_db_load_keys())
    else:
        _loop.run_until_complete(_db_load_keys())
except Exception as _e:
    _LOGGER.warning(f"multi_uploader: skip DB load on import — {_e}")


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


# ── Upload: Transfer.it via MEGA API langsung (tanpa mega.py) ────────────────
def _upload_transferit(path: str, key: str) -> str:
    """
    Transfer.it = powered by MEGA.
    Upload ke MEGA via MEGA API secara manual (HTTP, tanpa library mega.py
    yang tidak kompatibel Python 3.11+).
    key format: email:password (akun MEGA kamu)
    Gunakan: /settransferit email@mega.com:passwordmega
    """
    import hashlib
    import json
    import random
    import requests
    import struct

    if not key or ":" not in key:
        return (
            "❌ Transfer.it butuh akun MEGA\n"
            "Gunakan: /settransferit email@mega.com:passwordmega"
        )

    email, password = key.split(":", 1)

    # ── MEGA helper functions ─────────────────────────────────────────────────
    def _b64decode(s):
        import base64
        s = s.replace("-", "+").replace("_", "/")
        pad = 4 - len(s) % 4
        if pad != 4:
            s += "=" * pad
        return base64.b64decode(s)

    def _b64encode(b):
        import base64
        return base64.b64encode(b).replace(b"+", b"-").replace(b"/", b"_").replace(b"=", b"").decode()

    def _prepare_key(password_bytes):
        password_aes = [0, 0, 0, 0]
        key = [0, 0, 0, 0]
        for i in range(0, len(password_bytes), 4):
            chunk = 0
            for j in range(4):
                if i + j < len(password_bytes):
                    chunk = (chunk << 8) | password_bytes[i + j]
                else:
                    chunk = chunk << 8
            password_aes[i // 4 % 4] ^= chunk
        return password_aes

    def _str_to_a32(s):
        if isinstance(s, str):
            s = s.encode("utf-8")
        n = (len(s) + 3) // 4
        return list(struct.unpack(">" + "I" * n, s.ljust(n * 4, b"\x00")))

    def _a32_to_str(a):
        return struct.pack(">" + "I" * len(a), *a)

    def _hash_password(password):
        password_bytes = password.encode("utf-8")
        pkey = _prepare_key(password_bytes)
        # AES ECB encrypt using pycryptodome (tersedia di python)
        try:
            from Crypto.Cipher import AES
        except ImportError:
            # fallback: cryptography library
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend

            def aes_ecb_encrypt(key_bytes, data):
                cipher = Cipher(algorithms.AES(key_bytes), modes.ECB(), backend=default_backend())
                enc = cipher.encryptor()
                return enc.update(data) + enc.finalize()
        else:
            def aes_ecb_encrypt(key_bytes, data):
                cipher = AES.new(key_bytes, AES.MODE_ECB)
                return cipher.encrypt(data)

        key_bytes = _a32_to_str(pkey)
        hash_val  = [0, 0, 0, 0]
        p_bytes   = password.encode("utf-8")
        for i in range(len(p_bytes)):
            hash_val[i & 3] ^= p_bytes[i]

        aes_hash = [0, 0]
        h        = _a32_to_str(hash_val)
        for _ in range(16384):
            h = aes_ecb_encrypt(key_bytes, h)
        result = struct.unpack(">II", h[:8])
        return _b64encode(struct.pack(">II", result[0], result[1]))

    def _decrypt_key(key_b64, master_key_a32):
        try:
            from Crypto.Cipher import AES
            def aes_ecb_decrypt(key_bytes, data):
                return AES.new(key_bytes, AES.MODE_ECB).decrypt(data)
        except ImportError:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            def aes_ecb_decrypt(key_bytes, data):
                c = Cipher(algorithms.AES(key_bytes), modes.ECB(), backend=default_backend())
                d = c.decryptor()
                return d.update(data) + d.finalize()

        enc = _b64decode(key_b64)
        mk  = _a32_to_str(master_key_a32)
        dec = b""
        for i in range(0, len(enc), 16):
            dec += aes_ecb_decrypt(mk, enc[i:i+16])
        return list(struct.unpack(">" + "I" * (len(dec) // 4), dec))

    # ── MEGA API request ──────────────────────────────────────────────────────
    seq_no = random.randint(0, 0xFFFFFFFF)

    def api_req(data, sid=None):
        params = {"id": seq_no}
        if sid:
            params["sid"] = sid
        r = requests.post(
            "https://g.api.mega.co.nz/cs",
            params=params,
            json=[data],
            timeout=30,
        )
        resp = r.json()
        if isinstance(resp, list):
            return resp[0]
        return resp

    try:
        # Step 1 — login
        pw_hash = _hash_password(password)
        login_r = api_req({
            "a": "us",
            "user": email,
            "uh": pw_hash,
        })

        if isinstance(login_r, int):
            return f"❌ MEGA login gagal (error {login_r}). Cek email/password."

        sid        = login_r.get("csid") or login_r.get("sid")
        master_key = _decrypt_key(login_r["k"], _str_to_a32(pw_hash))

        if not sid:
            return "❌ MEGA: tidak dapat session ID setelah login"

        # Step 2 — upload file
        filename  = os.path.basename(path)
        file_size = os.path.getsize(path)

        # Get upload URL
        up_r = api_req({"a": "u", "s": file_size}, sid=sid)
        if isinstance(up_r, int):
            return f"❌ MEGA get upload URL gagal (error {up_r})"

        upload_url = up_r.get("p")
        if not upload_url:
            return f"❌ MEGA: tidak dapat upload URL — {up_r}"

        # Upload file
        with open(path, "rb") as f:
            http_r = requests.post(
                f"{upload_url}/0",
                data=f,
                headers={"Content-Length": str(file_size)},
                timeout=600,
            )

        completion_handle = http_r.text.strip()
        if not completion_handle or len(completion_handle) < 5:
            return f"❌ MEGA upload gagal: {http_r.text[:200]}"

        # Step 3 — commit file node
        import os as _os
        import time
        node_key = [
            random.randint(0, 0xFFFFFFFF) for _ in range(6)
        ]

        # Create public link
        pub_r = api_req({
            "a": "l",
            "n": completion_handle,
        }, sid=sid)

        if isinstance(pub_r, int) or not pub_r:
            return f"❌ MEGA: gagal buat link publik (error {pub_r})"

        return f"https://mega.nz/file/{pub_r}"

    except Exception as e:
        return f"❌ MEGA exception: {e}"


# ── Upload: Player4me (cloudscraper bypass Cloudflare) ───────────────────────
def _upload_player4me(path: str, key: str) -> str:
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    except Exception:
        import requests
        scraper = requests.Session()

    try:
        filename = os.path.basename(path)
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
        hint = "email@mega.com:passwordmega" if host == "transferit" else "API_KEY_ANDA"
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
