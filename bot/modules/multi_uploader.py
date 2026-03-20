import os
import urllib.parse
from asyncio import to_thread

from pyrogram.filters import command
from pyrogram.handlers import MessageHandler

from .. import DOWNLOAD_DIR, LOGGER
from ..core.telegram_manager import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.filters import CustomFilters
from ..helper.telegram_helper.message_utils import edit_message, send_message

# ── In-memory API key store ──────────────────────────────────────────────────
_API_KEYS: dict[str, str] = {
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


# ── Sync download helper (run in thread) ─────────────────────────────────────
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


# ── Sync upload helpers (run in thread) ──────────────────────────────────────
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
        if r.status_code == 200 and r.json().get("status") == "ok":
            return r.json()["data"]["downloadPage"]
        return f"❌ Gofile gagal: {r.text[:300]}"
    except Exception as e:
        return f"❌ Gofile error: {e}"


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
        return f"❌ Pixeldrain gagal: {r.text[:300]}"
    except Exception as e:
        return f"❌ Pixeldrain error: {e}"


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
        return f"❌ Buzzheavier gagal: {r.text[:300]}"
    except Exception as e:
        return f"❌ Buzzheavier error: {e}"


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
        rj = r.json()
        if r.status_code == 200:
            return (
                rj.get("data", {}).get("url")
                or rj.get("url")
                or str(rj)
            )
        return f"❌ Player4me gagal: {r.text[:300]}"
    except Exception as e:
        return f"❌ Player4me error: {e}"


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
        rj = r.json()
        if r.status_code == 200:
            return rj.get("data", {}).get("url") or rj.get("url") or str(rj)
        return f"❌ Akirabox gagal: {r.text[:300]}"
    except Exception as e:
        return f"❌ Akirabox error: {e}"


def _upload_filemirage(path: str, key: str) -> str:
    import requests
    try:
        with open(path, "rb") as f:
            r = requests.post(
                "https://filemirage.com/api/upload",
                files={"file": (os.path.basename(path), f)},
                data={"api_key": key},
                timeout=600,
            )
        rj = r.json()
        if r.status_code == 200:
            return rj.get("data", {}).get("url") or rj.get("url") or str(rj)
        return f"❌ Filemirage gagal: {r.text[:300]}"
    except Exception as e:
        return f"❌ Filemirage error: {e}"


def _upload_transferit(path: str, key: str) -> str:
    import requests
    try:
        with open(path, "rb") as f:
            r = requests.post(
                "https://transfer.it/api/upload",
                files={"file": (os.path.basename(path), f)},
                data={"api_key": key},
                timeout=600,
            )
        rj = r.json()
        if r.status_code == 200:
            return rj.get("data", {}).get("url") or rj.get("url") or str(rj)
        return f"❌ Transfer.it gagal: {r.text[:300]}"
    except Exception as e:
        return f"❌ Transfer.it error: {e}"


_UPLOAD_FUNCS = {
    "gofile":      _upload_gofile,
    "pixeldrain":  _upload_pixeldrain,
    "buzzheavier": _upload_buzzheavier,
    "player4me":   _upload_player4me,
    "akirabox":    _upload_akirabox,
    "filemirage":  _upload_filemirage,
    "transferit":  _upload_transferit,
}


# ── Telegram command handlers ─────────────────────────────────────────────────
@new_task
async def set_api_key_cmd(_, message):
    """Handler for /setgofile, /setpixeldrain, etc."""
    parts = message.text.strip().split(maxsplit=1)
    host  = parts[0].lstrip("/").lower().replace("set", "", 1)

    if len(parts) < 2:
        await send_message(
            message,
            f"⚙️ Gunakan: <code>/set{host} [API_KEY_ANDA]</code>",
        )
        return

    _API_KEYS[host] = parts[1].strip()
    await send_message(
        message,
        f"✅ API Key untuk <b>{host}</b> berhasil disimpan untuk sesi ini!",
    )


@new_task
async def multi_mirror_cmd(_, message):
    """Handler for /gofile, /pixeldrain, /buzzheavier, etc."""
    parts = message.text.strip().split(maxsplit=1)
    host  = parts[0].lstrip("/").lower()

    if len(parts) < 2:
        await send_message(
            message,
            (
                f"🔗 Kirim link yang ingin di-upload ke <b>{host}</b>:\n"
                f"<code>/{host} https://example.com/file.mkv</code>"
            ),
        )
        return

    url = parts[1].strip()
    if not url.startswith(("http://", "https://")):
        await send_message(message, "❌ URL tidak valid. Harus diawali <code>https://</code>")
        return

    # ── Step 1: Download ──────────────────────────────────────────────────────
    fname = url.split("/")[-1].split("?")[0].strip()
    if not fname or "." not in fname:
        fname = "download_temp"
    dest = os.path.join(DOWNLOAD_DIR, fname)

    status_msg = await send_message(message, f"⬇️ Mengunduh file dari link…")
    ok, err = await to_thread(_download_file, url, dest)

    if not ok:
        await edit_message(status_msg, f"❌ Gagal mengunduh:\n<code>{err}</code>")
        return

    # ── Step 2: Upload ────────────────────────────────────────────────────────
    await edit_message(status_msg, f"⬆️ File diunduh. Mengupload ke <b>{host}</b>…")

    api_key = _API_KEYS.get(host, "")
    func    = _UPLOAD_FUNCS.get(host)

    if not func:
        await edit_message(status_msg, f"❌ Host tidak dikenal: <code>{host}</code>")
        return

    link = await to_thread(func, dest, api_key)

    # ── Step 3: Cleanup ───────────────────────────────────────────────────────
    try:
        os.remove(dest)
    except Exception:
        pass

    await edit_message(
        status_msg,
        (
            f"✅ <b>Upload ke {host.capitalize()} selesai!</b>\n\n"
            f"🔗 Link: {link}"
        ),
    )


# ── Self-register handlers saat module di-import ─────────────────────────────
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

LOGGER.info("multi_uploader: handlers registered ✓")
