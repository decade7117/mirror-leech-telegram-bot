import math
import os
import urllib.parse
from asyncio import to_thread

from pyrogram.filters import command
from pyrogram.handlers import MessageHandler

from ..core.telegram_manager import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.filters import CustomFilters

# ── Storage API key per-sesi ──────────────────────────────────────────────────
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

TEMP_DIR = "/tmp/multi_uploader_dl"


def _safe_json(r):
    """Parse JSON safely, return None if response is empty or not JSON."""
    try:
        text = r.text.strip()
        if not text:
            return None
        import json
        return json.loads(text)
    except Exception:
        return None


# ── Download ──────────────────────────────────────────────────────────────────
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
    """
    PUT https://w.buzzheavier.com/<filename>
    Response: {"code": 201, "data": {"id": "xxxx", ...}}
    URL hasil: https://buzzheavier.com/<id>
    """
    import requests
    try:
        fname = urllib.parse.quote(os.path.basename(path), safe="")
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"

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
            # Ada field url langsung atau konstruksi dari id
            url = data.get("url")
            if not url and data.get("id"):
                url = f"https://buzzheavier.com/{data['id']}"
            if url:
                return url
        return f"❌ Buzzheavier HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return f"❌ Buzzheavier exception: {e}"


# ── Upload: Filemirage ────────────────────────────────────────────────────────
def _upload_filemirage(path: str, key: str) -> str:
    """
    1. GET https://filemirage.com/api/servers
       Header: Authorization: Bearer <token>
       Response: { success: true, data: { server: "https://...", upload_id: "..." } }
    2. POST <server>/upload.php
       fields: filename, upload_id, chunk_number, total_chunks, file
    """
    import requests

    CHUNK_SIZE = 100 * 1024 * 1024  # 100 MB

    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        # Step 1 — get server
        srv_r = requests.get(
            "https://filemirage.com/api/servers",
            headers=headers,
            timeout=30,
        )
        srv_j = _safe_json(srv_r)
        if not srv_j:
            return (
                f"❌ Filemirage: response server kosong\n"
                f"HTTP {srv_r.status_code}\n{srv_r.text[:300]}"
            )
        if not srv_j.get("success"):
            return f"❌ Filemirage get server gagal: {srv_j.get('message', str(srv_j)[:200])}"

        server       = srv_j["data"]["server"].rstrip("/")
        upload_id    = srv_j["data"]["upload_id"]
        filename     = os.path.basename(path)
        file_size    = os.path.getsize(path)
        total_chunks = max(1, math.ceil(file_size / CHUNK_SIZE))
        upload_url   = f"{server}/upload.php"

        # Step 2 — upload chunk(s)
        last_rj = {}
        with open(path, "rb") as fh:
            for i in range(total_chunks):
                chunk_data = fh.read(CHUNK_SIZE)
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
                up_j = _safe_json(up_r)
                if not up_j:
                    return (
                        f"❌ Filemirage chunk {i}: response kosong\n"
                        f"HTTP {up_r.status_code}\n{up_r.text[:300]}"
                    )
                if not up_j.get("success"):
                    return f"❌ Filemirage chunk {i} gagal: {up_j.get('message', str(up_j)[:200])}"
                last_rj = up_j

        result_url = last_rj.get("data", {}).get("url")
        if result_url:
            return result_url
        return f"❌ Filemirage: selesai tapi URL tidak ada — {last_rj}"

    except Exception as e:
        return f"❌ Filemirage exception: {e}"


# ── Upload: Transfer.it (web scraping) ────────────────────────────────────────
def _upload_transferit(path: str, key: str) -> str:
    """
    Transfer.it tidak punya API resmi.
    key format: email:password
    Simpan pakai: /settransferit email@kamu.com:passwordkamu
    """
    import re
    import requests

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
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    })

    try:
        # Step 1 — ambil CSRF dari halaman login
        login_page = session.get("https://transfer.it/login", timeout=30)
        csrf = ""
        m = re.search(
            r'name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
            login_page.text,
        )
        if not m:
            m = re.search(
                r'<meta name=["\']csrf-token["\'] content=["\']([^"\']+)["\']',
                login_page.text,
            )
        if m:
            csrf = m.group(1)

        # Step 2 — POST login
        session.post(
            "https://transfer.it/login",
            data={"_token": csrf, "email": email, "password": password},
            headers={"Referer": "https://transfer.it/login"},
            timeout=30,
            allow_redirects=True,
        )

        # Step 3 — ambil CSRF baru setelah login
        home = session.get("https://transfer.it/", timeout=30)
        if "logout" not in home.text.lower():
            return "❌ Transfer.it: login gagal, cek email/password"

        m2 = re.search(
            r'name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
            home.text,
        )
        if not m2:
            m2 = re.search(
                r'<meta name=["\']csrf-token["\'] content=["\']([^"\']+)["\']',
                home.text,
            )
        csrf2 = m2.group(1) if m2 else csrf

        # Step 4 — upload
        filename = os.path.basename(path)
        with open(path, "rb") as f:
            up_r = session.post(
                "https://transfer.it/upload",
                files={"file": (filename, f)},
                data={"_token": csrf2},
                headers={
                    "Referer": "https://transfer.it/",
                    "X-CSRF-TOKEN": csrf2,
                },
                timeout=600,
            )

        rj = _safe_json(up_r)
        if rj:
            url = rj.get("url") or rj.get("link") or rj.get("download_url")
            if url:
                return url
            return f"❌ Transfer.it JSON tapi tidak ada URL: {str(rj)[:300]}"

        # Fallback cari link di HTML
        m3 = re.search(r'https://transfer\.it/\S+', up_r.text)
        if m3:
            return m3.group(0).rstrip('",\'')

        return (
            f"❌ Transfer.it: tidak bisa parse response\n"
            f"HTTP {up_r.status_code}\n{up_r.text[:300]}"
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
            return (
                rj.get("data", {}).get("url")
                or rj.get("url")
                or rj.get("link")
                or str(rj)
            )
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
            return (
                rj.get("data", {}).get("url")
                or rj.get("url")
                or rj.get("link")
                or str(rj)
            )
        return f"❌ Akirabox HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return f"❌ Akirabox exception: {e}"


# ── Routing table ─────────────────────────────────────────────────────────────
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
    await message.reply(f"✅ Credentials <b>{host}</b> berhasil disimpan!")


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

    # Step 1: Download
    status_msg = await message.reply(
        f"⬇️ Mengunduh file dari link…\n<code>{url}</code>"
    )
    ok, err = await to_thread(_download_file, url, dest)

    if not ok:
        await status_msg.edit(f"❌ Gagal mengunduh:\n<code>{err}</code>")
        return

    size_kb = os.path.getsize(dest) // 1024
    size_str = f"{size_kb // 1024} MB" if size_kb > 1024 else f"{size_kb} KB"

    # Step 2: Upload
    await status_msg.edit(
        f"⬆️ File diunduh ({size_str}). Mengupload ke <b>{host}</b>…"
    )

    key  = _API_KEYS.get(host, "")
    func = _UPLOAD_FUNCS.get(host)

    if not func:
        await status_msg.edit(f"❌ Host tidak dikenal: <code>{host}</code>")
        return

    link = await to_thread(func, dest, key)

    # Cleanup
    try:
        os.remove(dest)
    except Exception:
        pass

    await status_msg.edit(
        f"✅ <b>Upload ke {host.capitalize()} selesai!</b>\n\n🔗 {link}"
    )


# ── Self-register handlers ────────────────────────────────────────────────────
TgClient.bot.add_handler(
    MessageHandler(
        set_api_key_cmd,
        filters=command(SET_HOST_LIST) & CustomFilters.sudo,
    )
)
TgClient.bot.add_handler(
    MessageHandler(
        multi_mirror_cmd,
        filters=command(HOST_LIST) & CustomFilters.authorized,
    )
)
