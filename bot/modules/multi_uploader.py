"""
============================================================
 bot/modules/multi_uploader.py
 FITUR:
 - /gofile, /pixeldrain, /buzzheavier, /filemirage, /player4me, /akirabox
 - /transferit     → download video dari URL dan upload ke transfer.it (via Playwright)
============================================================
"""

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
DOWNLOAD_DIR  = "/app/downloads"
P4M_BASE      = "https://player4me.com"


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

def _sizeof_fmt(num_bytes: int) -> str:
    if num_bytes >= 1024 ** 3: return f"{num_bytes / 1024 ** 3:.1f} GB"
    if num_bytes >= 1024 ** 2: return f"{num_bytes / 1024 ** 2:.1f} MB"
    return f"{num_bytes / 1024:.1f} KB"

def _download_url(url: str, dest: str):
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

def _find_file_in_downloads(name: str):
    if os.path.isabs(name) and os.path.exists(name): return name
    if not os.path.isdir(DOWNLOAD_DIR): return None
    name_lower = name.lower()
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        for f in files:
            if f.lower() == name_lower: return os.path.join(root, f)
    return None


# ── Upload: Gofile ────────────────────────────────────────────────────────────
def _upload_gofile(path: str, key: str) -> str:
    import requests
    try:
        server = requests.get("https://api.gofile.io/servers", timeout=30).json()["data"]["servers"][0]["name"]
        with open(path, "rb") as f:
            data = {"token": key} if key else {}
            r = requests.post(f"https://{server}.gofile.io/contents/uploadfile", files={"file": (os.path.basename(path), f)}, data=data, timeout=600)
        rj = _safe_json(r)
        if rj and rj.get("status") == "ok": return rj["data"]["downloadPage"]
        return f"❌ Gofile error: {r.text[:200]}"
    except Exception as e:
        return f"❌ Gofile exception: {e}"


# ── Upload: Pixeldrain ────────────────────────────────────────────────────────
def _upload_pixeldrain(path: str, key: str) -> str:
    import requests
    try:
        auth = ("", key) if key else None
        with open(path, "rb") as f:
            r = requests.post("https://pixeldrain.com/api/file", files={"file": (os.path.basename(path), f)}, auth=auth, timeout=600)
        rj = _safe_json(r)
        if rj and rj.get("id"): return f"https://pixeldrain.com/u/{rj['id']}"
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
            r = requests.put(f"https://w.buzzheavier.com/{fname}", data=f, headers=headers, timeout=600)
        rj = _safe_json(r)
        if rj:
            data = rj.get("data", {})
            url  = data.get("url")
            if not url and data.get("id"): url = f"https://buzzheavier.com/{data['id']}"
            if url: return url
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
        if not srv_j or not srv_j.get("success"): return f"❌ Filemirage get server gagal: {srv_r.text[:300]}"

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
                up_r = requests.post(upload_url, headers=headers, files={"file": (filename, chunk_data, "application/octet-stream")}, data={"filename": filename, "upload_id": upload_id, "chunk_number": str(i), "total_chunks": str(total_chunks)}, timeout=600)
                if up_r.status_code not in (200, 201): return f"❌ Filemirage chunk {i}: HTTP {up_r.status_code}\n{up_r.text[:300]}"
                if not is_last: continue
                up_j = _safe_json(up_r)
                if up_j: last_rj = up_j

        url = last_rj.get("data", {}).get("url") if isinstance(last_rj.get("data"), dict) else None
        if url: return url
        return f"❌ Filemirage: selesai tapi tidak ada URL — {last_rj}"
    except Exception as e:
        return f"❌ Filemirage exception: {e}"


# ── Upload: Transfer.it (Menggunakan Playwright) ──────────────────────────────
def _upload_transferit(path: str, key: str) -> str:
    """
    Fungsi ini berjalan sinkron untuk membungkus kode asinkron dari Playwright
    agar kompatibel dengan arsitektur to_thread di multi_mirror_cmd
    """
    import asyncio
    from playwright.async_api import async_playwright
    from .. import LOGGER

    async def _run_playwright():
        LOGGER.info(f"[Transfer.it] Memulai upload Playwright untuk: {os.path.basename(path)}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
            page = await context.new_page()
            try:
                await page.goto("https://transfer.it/")
                
                # FIX: Menggunakan .first agar Playwright tidak bingung memilih elemen
                LOGGER.info("[Transfer.it] Memasukkan file...")
                await page.locator("input[type='file']").first.set_input_files(path)
                
                LOGGER.info("[Transfer.it] Menekan tombol Transfer...")
                transfer_btn = page.locator("button:has-text('Transfer')").first
                await transfer_btn.wait_for(state="visible", timeout=15000)
                await transfer_btn.click()
                
                LOGGER.info("[Transfer.it] Menunggu proses upload selesai...")
                await page.locator("text='Completed!'").first.wait_for(state="visible", timeout=3600000)

                LOGGER.info("[Transfer.it] Mengambil link...")
                link_element = page.locator("input[readonly]").first
                if await link_element.count() > 0:
                    return await link_element.input_value()
                return "❌ Upload selesai, tapi gagal mengekstrak link dari layar."
            except Exception as e:
                err_msg = f"❌ Gagal upload ke Transfer.it: {str(e)}"
                LOGGER.error(err_msg)
                return err_msg
            finally:
                await browser.close()

    return asyncio.run(_run_playwright())


# ── Upload: Player4me ─────────────────────────────────────────────────────────
def _p4m_headers(key: str) -> dict:
    return {"api-token": key, "Accept": "application/json"}

def _p4m_advance_upload_url(url: str, key: str, name: str = None) -> str:
    import requests
    import time
    headers = _p4m_headers(key)
    payload = {"url": url}
    if name: payload["name"] = name
    try:
        r = requests.post(f"{P4M_BASE}/api/v1/video/advance-upload", json=payload, headers=headers, timeout=30)
        rj = _safe_json(r)
        if r.status_code not in (200, 201) or not rj: return f"❌ Player4me advance-upload gagal: HTTP {r.status_code} — {r.text[:300]}"

        task_id = rj.get("id")
        if not task_id: return f"❌ Player4me: tidak dapat task ID — {rj}"

        max_wait, interval, waited = 1800, 15, 0
        while waited < max_wait:
            time.sleep(interval)
            waited += interval
            st_r = requests.get(f"{P4M_BASE}/api/v1/video/advance-upload/{task_id}", headers=headers, timeout=30)
            sj = _safe_json(st_r)
            if not sj: continue
            status = sj.get("status", "")
            if status == "Completed":
                videos = sj.get("videos", [])
                if videos:
                    vid_id = videos[0] if isinstance(videos[0], str) else videos[0].get("id", "")
                    if vid_id: return f"https://player4me.com/video/{vid_id}"
                return f"✅ Player4me selesai (task {task_id}) tapi video ID tidak tersedia"
            elif status in ("Failed", "Error"): return f"❌ Player4me task gagal: {sj.get('error', 'unknown')}"

        return f"❌ Player4me timeout 30 menit. Task ID: {task_id} — cek manual di dashboard"
    except Exception as e:
        return f"❌ Player4me exception: {e}"

def _p4m_tus_upload(path: str, key: str) -> str:
    import base64
    import requests
    headers, CHUNK = _p4m_headers(key), 52_428_800
    try:
        ep_r = requests.get(f"{P4M_BASE}/api/v1/video/upload", headers=headers, timeout=30)
        ep_j = _safe_json(ep_r)
        if not ep_j or ep_r.status_code != 200: return f"❌ Player4me TUS endpoint gagal: {ep_r.text[:200]}"

        tus_url, access_token = ep_j.get("tusUrl", "").rstrip("/") + "/", ep_j.get("accessToken", "")
        if not tus_url or not access_token: return f"❌ Player4me: tidak dapat tusUrl/accessToken — {ep_j}"

        filename, file_size = os.path.basename(path), os.path.getsize(path)
        metadata = f"accessToken {base64.b64encode(access_token.encode()).decode()},filename {base64.b64encode(filename.encode()).decode()},filetype {base64.b64encode('video/mp4'.encode()).decode()}"

        create_r = requests.post(tus_url, headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(file_size), "Upload-Metadata": metadata, "Content-Length": "0", "api-token": key}, timeout=30)
        if create_r.status_code != 201: return f"❌ Player4me TUS create gagal: HTTP {create_r.status_code} — {create_r.text[:200]}"

        upload_url = create_r.headers.get("Location", "")
        if not upload_url: return "❌ Player4me TUS: tidak dapat upload location"

        offset = 0
        with open(path, "rb") as fh:
            while offset < file_size:
                chunk = fh.read(CHUNK)
                patch_r = requests.patch(upload_url, data=chunk, headers={"Tus-Resumable": "1.0.0", "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream", "Content-Length": str(len(chunk)), "api-token": key}, timeout=600)
                if patch_r.status_code not in (200, 204): return f"❌ Player4me TUS chunk gagal offset {offset}: HTTP {patch_r.status_code}"
                offset += len(chunk)

        vid_id = upload_url.rstrip("/").split("/")[-1]
        if vid_id: return f"https://player4me.com/video/{vid_id}"
        return "✅ Player4me upload selesai! Cek dashboard untuk link video."
    except Exception as e:
        return f"❌ Player4me TUS exception: {e}"

def _upload_player4me(path: str, key: str) -> str:
    if not key: return "❌ Player4me butuh API key.\nGunakan: /setplayer4me API_TOKEN"
    return _p4m_tus_upload(path, key)


# ── Upload: Akirabox ──────────────────────────────────────────────────────────
def _upload_akirabox(path: str, key: str) -> str:
    import requests
    try:
        with open(path, "rb") as f:
            r = requests.post("https://akirabox.com/api/upload", files={"file": (os.path.basename(path), f)}, data={"api_key": key}, timeout=600)
        rj = _safe_json(r)
        if rj and r.status_code == 200: return rj.get("data", {}).get("url") or rj.get("url") or rj.get("link") or str(rj)
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
            "gofile":     "API_TOKEN (opsional)",
            "pixeldrain": "API_KEY",
            "filemirage": "API_TOKEN",
            "player4me":  "API_TOKEN (dari player4me.com/account/api)",
            "akirabox":   "API_KEY",
            "buzzheavier":"API_KEY (opsional)",
            "transferit": "tidak butuh API_KEY",
        }
        await message.reply(f"Gunakan: <code>/set{host} {hints.get(host, 'API_KEY')}</code>")
        return
    _API_KEYS[host] = parts[1].strip()
    await _db_save_keys()
    await message.reply(f"✅ Credentials <b>{host}</b> berhasil disimpan ke database!")


@new_task
async def multi_mirror_cmd(_, message):
    parts = message.text.strip().split(maxsplit=1)
    host  = parts[0].lstrip("/").lower()

    func = _UPLOAD_FUNCS.get(host)
    if not func:
        await message.reply(f"❌ Host tidak dikenal: <code>{host}</code>")
        return

    if len(parts) < 2:
        await message.reply(
            f"🔗 <b>Upload ke {host.capitalize()}</b>\n\n"
            f"<b>Cara 1</b> — Link langsung:\n"
            f"<code>/{host} https://example.com/file.mkv</code>\n\n"
            f"<b>Cara 2</b> — Path lokal:\n"
            f"<code>/{host} /app/downloads/namafile.mkv</code>\n\n"
            f"<b>Cara 3</b> — Nama file saja:\n"
            f"<code>/{host} namafile.mkv</code>"
        )
        return

    arg    = parts[1].strip()
    is_url = arg.startswith(("http://", "https://"))
    is_abs = arg.startswith("/")

    status_msg = await message.reply("🔍 Memproses…")

    if host == "player4me" and is_url:
        key = _API_KEYS.get("player4me", "")
        if not key:
            await status_msg.edit("❌ Player4me butuh API key.\nGunakan: /setplayer4me API_TOKEN")
            return
        await status_msg.edit(
            f"📤 Mengirim URL ke server Player4me…\n"
            f"(Server Player4me yang download filenya)\n"
            f"<code>{arg}</code>\n\n"
            f"<i>Bot akan poll status setiap 15 detik…</i>"
        )
        fname = arg.split("/")[-1].split("?")[0].strip() or None
        link  = await to_thread(_p4m_advance_upload_url, arg, key, fname)
        await status_msg.edit(f"✅ <b>Upload ke Player4me selesai!</b>\n\n🔗 {link}")
        return

    need_del = False
    dest     = None

    if is_url:
        fname = arg.split("/")[-1].split("?")[0].strip()
        if not fname or len(fname) < 3:
            fname = "file_download"
        os.makedirs(TEMP_DIR, exist_ok=True)
        dest = os.path.join(TEMP_DIR, fname)
        await status_msg.edit(f"⬇️ Mengunduh file dari URL…\n<code>{arg}</code>")
        ok, err = await to_thread(_download_url, arg, dest)
        if not ok:
            await status_msg.edit(f"❌ Gagal mengunduh:\n<code>{err}</code>")
            return
        need_del = True

    elif is_abs:
        dest = arg
        if not os.path.exists(dest):
            await status_msg.edit(f"❌ File tidak ditemukan: <code>{dest}</code>")
            return

    else:
        await status_msg.edit(f"🔍 Mencari <code>{arg}</code> di folder downloads…")
        found = await to_thread(_find_file_in_downloads, arg)
        if not found:
            files_list = []
            if os.path.isdir(DOWNLOAD_DIR):
                for root, dirs, files in os.walk(DOWNLOAD_DIR):
                    for f in files[:10]:
                        files_list.append(f"• <code>{f}</code>")
            hint = "\n".join(files_list) if files_list else "<i>Folder downloads kosong</i>"
            await status_msg.edit(
                f"❌ File <code>{arg}</code> tidak ditemukan.\n\nFile tersedia:\n{hint}"
            )
            return
        dest = found

    size_str = _sizeof_fmt(os.path.getsize(dest))
    
    if host == "transferit":
        await status_msg.edit(f"⬆️ Mengupload ke <b>Transfer.it</b> (Via Playwright)…\n⏳ Harap bersabar, proses ini memakan waktu\n📁 <code>{os.path.basename(dest)}</code> ({size_str})")
    else:
        await status_msg.edit(f"⬆️ Mengupload ke <b>{host.capitalize()}</b>…\n📁 <code>{os.path.basename(dest)}</code> ({size_str})")

    key  = _API_KEYS.get(host, "")
    link = await to_thread(func, dest, key)

    if need_del:
        try:
            os.remove(dest)
        except Exception:
            pass

    if link.startswith("http"):
        await status_msg.edit(
            f"✅ <b>Upload ke {host.capitalize()} selesai!</b>\n\n"
            f"📁 {os.path.basename(dest)} ({size_str})\n"
            f"🔗 {link}"
        )
    else:
        await status_msg.edit(link)


# ── Register handlers ─────────────────────────────────────────────────────────
TgClient.bot.add_handler(
    MessageHandler(set_api_key_cmd, filters=command(SET_HOST_LIST) & CustomFilters.sudo)
)
TgClient.bot.add_handler(
    MessageHandler(multi_mirror_cmd, filters=command(HOST_LIST) & CustomFilters.authorized)
)
