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


# ── Upload functions ──────────────────────────────────────────────────────────
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
        rj = r.json()
        if r.status_code == 200 and rj.get("status") == "ok":
            return rj["data"]["downloadPage"]
        return f"❌ Gofile error: {r.text[:200]}"
    except Exception as e:
        return f"❌ Gofile exception: {e}"


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
        if r.status_code in (200, 201):
            return f"https://pixeldrain.com/u/{r.json().get('id')}"
        return f"❌ Pixeldrain error: {r.text[:200]}"
    except Exception as e:
        return f"❌ Pixeldrain exception: {e}"


def _upload_buzzheavier(path: str, key: str) -> str:
    import requests
    try:
        fname = urllib.parse.quote(os.path.basename(path), safe="")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        with open(path, "rb") as f:
            r = requests.put(
                f"https://w.buzzheavier.com/{fname}",
                data=f,
                headers=headers,
                timeout=600,
            )
        if r.status_code == 200:
            d = r.json().get("data", {})
            return d.get("url") or f"https://buzzheavier.com/f/{d.get('id','?')}"
        return f"❌ Buzzheavier error: {r.text[:200]}"
    except Exception as e:
        return f"❌ Buzzheavier exception: {e}"


def _upload_generic(path: str, endpoint: str, key: str) -> str:
    import requests
    try:
        with open(path, "rb") as f:
            r = requests.post(
                endpoint,
                files={"file": (os.path.basename(path), f)},
                data={"api_key": key},
                timeout=600,
            )
        rj = r.json()
        if r.status_code == 200:
            return (
                rj.get("data", {}).get("url")
                or rj.get("url")
                or rj.get("link")
                or str(rj)
            )
        return f"❌ HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return f"❌ Exception: {e}"


_ENDPOINTS = {
    "player4me":  "https://player4me.com/api/upload",
    "akirabox":   "https://akirabox.com/api/upload",
    "filemirage": "https://filemirage.com/api/upload",
    "transferit": "https://transfer.it/api/upload",
}


# ── Handlers ──────────────────────────────────────────────────────────────────
@new_task
async def set_api_key_cmd(_, message):
    parts = message.text.strip().split(maxsplit=1)
    host  = parts[0].lstrip("/").lower().replace("set", "", 1)
    if len(parts) < 2:
        await message.reply(f"Gunakan: <code>/set{host} API_KEY_ANDA</code>")
        return
    _API_KEYS[host] = parts[1].strip()
    await message.reply(f"✅ API Key <b>{host}</b> berhasil disimpan!")


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

    # Tentukan nama file
    fname = url.split("/")[-1].split("?")[0].strip()
    if not fname or len(fname) < 3:
        fname = "file_download"

    os.makedirs(TEMP_DIR, exist_ok=True)
    dest = os.path.join(TEMP_DIR, fname)

    # Step 1: Download
    status_msg = await message.reply(f"⬇️ Mengunduh file dari link…\n<code>{url}</code>")
    ok, err = await to_thread(_download_file, url, dest)

    if not ok:
        await status_msg.edit(f"❌ Gagal mengunduh:\n<code>{err}</code>")
        return

    # Step 2: Upload
    await status_msg.edit(f"⬆️ File diunduh ({os.path.getsize(dest)//1024} KB). Mengupload ke <b>{host}</b>…")

    key = _API_KEYS.get(host, "")

    if host == "gofile":
        link = await to_thread(_upload_gofile, dest, key)
    elif host == "pixeldrain":
        link = await to_thread(_upload_pixeldrain, dest, key)
    elif host == "buzzheavier":
        link = await to_thread(_upload_buzzheavier, dest, key)
    else:
        ep = _ENDPOINTS.get(host)
        if not ep:
            await status_msg.edit(f"❌ Host tidak dikenal: {host}")
            return
        link = await to_thread(_upload_generic, dest, ep, key)

    # Cleanup
    try:
        os.remove(dest)
    except Exception:
        pass

    await status_msg.edit(
        f"✅ <b>Upload ke {host.capitalize()} selesai!</b>\n\n🔗 {link}"
    )


# ── Register handlers ─────────────────────────────────────────────────────────
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
