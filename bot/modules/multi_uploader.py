"""
============================================================
 bot/modules/multi_uploader.py
 FITUR:
 - /gofile, /pixeldrain, /buzzheavier, /filemirage, /player4me, /akirabox
 - /transferit     → download video dari URL dan upload ke transfer.it (Hybrid: Playwright login -> API Upload)
 - /cancelup       → Membatalkan proses download/upload yang sedang berjalan
============================================================
"""

import math
import os
import inspect
import urllib.parse
from asyncio import to_thread
import asyncio
import time

from pyrogram.filters import command
from pyrogram.handlers import MessageHandler
from pyrogram.types import Message

from ..core.telegram_manager import TgClient
from ..helper.ext_utils.bot_utils import new_task
from ..helper.telegram_helper.filters import CustomFilters

# ── In-memory API key store & Cancel Flag ─────────────────────────────────────
_API_KEYS: dict = {
    "gofile":      "",
    "pixeldrain":  "",
    "transferit":  "",
    "filemirage":  "",
    "buzzheavier": "",
    "player4me":   "",
    "akirabox":    "",
}

# Menyimpan status pembatalan per user
_CANCEL_TASKS: dict = {}

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
    _loop = asyncio.get_event_loop()
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

def _download_url(url: str, dest: str, user_id: int):
    import requests
    try:
        with requests.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if _CANCEL_TASKS.get(user_id):
                        return False, "Dibatalkan oleh pengguna (/cancelup)."
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


# ── Upload: Gofile (Live Progress Bar) ────────────────────────────────────────
async def _upload_gofile(path: str, key: str, user_id: int, status_msg: Message) -> str:
    import requests
    if _CANCEL_TASKS.get(user_id): return "❌ Dibatalkan oleh pengguna."
    try:
        server = requests.get("https://api.gofile.io/servers", timeout=30).json()["data"]["servers"][0]["name"]
        url = f"https://{server}.gofile.io/contents/uploadfile"
        filename = os.path.basename(path)
        total_size = os.path.getsize(path)
        boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
        
        shared_prog = {"uploaded": 0, "total": total_size, "done": False, "result": None}

        def sync_upload_chunk():
            try:
                def file_gen():
                    head = (f"--{boundary}\r\n"
                            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                            f"Content-Type: application/octet-stream\r\n\r\n").encode('utf-8')
                    yield head
                    shared_prog['uploaded'] += len(head)
                    
                    if key:
                        token_field = f"--{boundary}\r\nContent-Disposition: form-data; name=\"token\"\r\n\r\n{key}\r\n".encode('utf-8')
                        yield token_field
                        shared_prog['uploaded'] += len(token_field)

                    with open(path, "rb") as f:
                        while chunk := f.read(262144):
                            if _CANCEL_TASKS.get(user_id): break
                            yield chunk
                            shared_prog['uploaded'] += len(chunk)
                            
                    tail = f"\r\n--{boundary}--\r\n".encode('utf-8')
                    yield tail
                    shared_prog['uploaded'] += len(tail)

                headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
                r = requests.post(url, data=file_gen(), headers=headers, timeout=600)
                rj = _safe_json(r)
                if rj and rj.get("status") == "ok":
                    shared_prog['result'] = rj["data"]["downloadPage"]
                else:
                    shared_prog['result'] = f"❌ Gofile error: {r.text[:200]}"
            except Exception as e:
                shared_prog['result'] = f"❌ Gofile exception: {e}"
            finally:
                shared_prog['done'] = True

        async def progress_updater():
            start_time = time.time()
            while not shared_prog['done']:
                await asyncio.sleep(3)
                if shared_prog['done']: break
                uploaded, total = shared_prog['uploaded'], shared_prog['total']
                percent = (uploaded / total) * 100 if total > 0 else 0
                elapsed = time.time() - start_time
                speed = uploaded / elapsed if elapsed > 0 else 0
                bar = "█" * int(15 * percent / 100) + "▒" * (15 - int(15 * percent / 100))
                text = f"⬆️ **Mengupload ke GOFILE**\n📁 `{filename}`\n[{bar}] {percent:.1f}%\n**Processed:** {_sizeof_fmt(uploaded)} / {_sizeof_fmt(total)}\n**Speed:** {_sizeof_fmt(speed)}/s"
                try: await status_msg.edit(text)
                except: pass

        updater_task = asyncio.create_task(progress_updater())
        await asyncio.to_thread(sync_upload_chunk)
        await updater_task
        return shared_prog['result'] or "❌ Gagal mengunggah ke GoFile."
    except Exception as e:
        return f"❌ Gofile exception: {e}"


# ── Upload: Pixeldrain (Live Progress Bar) ────────────────────────────────────
async def _upload_pixeldrain(path: str, key: str, user_id: int, status_msg: Message) -> str:
    import requests
    if _CANCEL_TASKS.get(user_id): return "❌ Dibatalkan oleh pengguna."
    try:
        filename = os.path.basename(path)
        total_size = os.path.getsize(path)
        auth = ("", key) if key else None
        
        shared_prog = {"uploaded": 0, "total": total_size, "done": False, "result": None}

        def sync_upload_chunk():
            try:
                def file_gen():
                    with open(path, "rb") as f:
                        while chunk := f.read(262144):
                            if _CANCEL_TASKS.get(user_id): break
                            yield chunk
                            shared_prog['uploaded'] += len(chunk)

                r = requests.post("https://pixeldrain.com/api/file", data=file_gen(), auth=auth, headers={"Content-Disposition": f'attachment; filename="{filename}"'}, timeout=600)
                rj = _safe_json(r)
                if rj and rj.get("id"):
                    shared_prog['result'] = f"https://pixeldrain.com/u/{rj['id']}"
                else:
                    shared_prog['result'] = f"❌ Pixeldrain error: {r.text[:200]}"
            except Exception as e:
                shared_prog['result'] = f"❌ Pixeldrain exception: {e}"
            finally:
                shared_prog['done'] = True

        async def progress_updater():
            start_time = time.time()
            while not shared_prog['done']:
                await asyncio.sleep(3)
                if shared_prog['done']: break
                uploaded, total = shared_prog['uploaded'], shared_prog['total']
                percent = (uploaded / total) * 100 if total > 0 else 0
                elapsed = time.time() - start_time
                speed = uploaded / elapsed if elapsed > 0 else 0
                bar = "█" * int(15 * percent / 100) + "▒" * (15 - int(15 * percent / 100))
                text = f"⬆️ **Mengupload ke PIXELDRAIN**\n📁 `{filename}`\n[{bar}] {percent:.1f}%\n**Processed:** {_sizeof_fmt(uploaded)} / {_sizeof_fmt(total)}\n**Speed:** {_sizeof_fmt(speed)}/s"
                try: await status_msg.edit(text)
                except: pass

        updater_task = asyncio.create_task(progress_updater())
        await asyncio.to_thread(sync_upload_chunk)
        await updater_task
        return shared_prog['result'] or "❌ Gagal Pixeldrain."
    except Exception as e:
        return f"❌ Pixeldrain exception: {e}"


# ── Upload: Buzzheavier (Live Progress Bar) ───────────────────────────────────
async def _upload_buzzheavier(path: str, key: str, user_id: int, status_msg: Message) -> str:
    import requests
    if _CANCEL_TASKS.get(user_id): return "❌ Dibatalkan oleh pengguna."
    try:
        fname = urllib.parse.quote(os.path.basename(path), safe="")
        total_size = os.path.getsize(path)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        
        shared_prog = {"uploaded": 0, "total": total_size, "done": False, "result": None}

        def sync_upload_chunk():
            try:
                def file_gen():
                    with open(path, "rb") as f:
                        while chunk := f.read(262144):
                            if _CANCEL_TASKS.get(user_id): break
                            yield chunk
                            shared_prog['uploaded'] += len(chunk)

                r = requests.put(f"https://w.buzzheavier.com/{fname}", data=file_gen(), headers=headers, timeout=600)
                rj = _safe_json(r)
                if rj:
                    data = rj.get("data", {})
                    url = data.get("url")
                    if not url and data.get("id"): url = f"https://buzzheavier.com/{data['id']}"
                    if url:
                        shared_prog['result'] = url
                        return
                shared_prog['result'] = f"❌ Buzzheavier HTTP {r.status_code}: {r.text[:300]}"
            except Exception as e:
                shared_prog['result'] = f"❌ Buzzheavier exception: {e}"
            finally:
                shared_prog['done'] = True

        async def progress_updater():
            start_time = time.time()
            while not shared_prog['done']:
                await asyncio.sleep(3)
                if shared_prog['done']: break
                uploaded, total = shared_prog['uploaded'], shared_prog['total']
                percent = (uploaded / total) * 100 if total > 0 else 0
                elapsed = time.time() - start_time
                speed = uploaded / elapsed if elapsed > 0 else 0
                bar = "█" * int(15 * percent / 100) + "▒" * (15 - int(15 * percent / 100))
                text = f"⬆️ **Mengupload ke BUZZHEAVIER**\n📁 `{os.path.basename(path)}`\n[{bar}] {percent:.1f}%\n**Processed:** {_sizeof_fmt(uploaded)} / {_sizeof_fmt(total)}\n**Speed:** {_sizeof_fmt(speed)}/s"
                try: await status_msg.edit(text)
                except: pass

        updater_task = asyncio.create_task(progress_updater())
        await asyncio.to_thread(sync_upload_chunk)
        await updater_task
        return shared_prog['result'] or "❌ Gagal Buzzheavier."
    except Exception as e:
        return f"❌ Buzzheavier exception: {e}"


# ── Upload: Filemirage (Live Progress Bar) ────────────────────────────────────
async def _upload_filemirage(path: str, key: str, user_id: int, status_msg: Message) -> str:
    import requests
    if _CANCEL_TASKS.get(user_id): return "❌ Dibatalkan oleh pengguna."
    try:
        filename = os.path.basename(path)
        file_size = os.path.getsize(path)
        CHUNK_SIZE = 100 * 1024 * 1024
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        
        shared_prog = {"uploaded": 0, "total": file_size, "done": False, "result": None}
        
        def sync_upload_fm():
            try:
                srv_r = requests.get("https://filemirage.com/api/servers", headers=headers, timeout=30)
                srv_j = _safe_json(srv_r)
                if not srv_j or not srv_j.get("success"):
                    shared_prog['result'] = f"❌ Filemirage get server gagal: {srv_r.text[:300]}"
                    return

                server = srv_j["data"]["server"].rstrip("/")
                upload_id = srv_j["data"]["upload_id"]
                total_chunks = max(1, math.ceil(file_size / CHUNK_SIZE))
                upload_url = f"{server}/upload.php"
                last_rj = {}

                with open(path, "rb") as fh:
                    for i in range(total_chunks):
                        if _CANCEL_TASKS.get(user_id): break
                        chunk_data = fh.read(CHUNK_SIZE)
                        up_r = requests.post(upload_url, headers=headers, files={"file": (filename, chunk_data, "application/octet-stream")}, data={"filename": filename, "upload_id": upload_id, "chunk_number": str(i), "total_chunks": str(total_chunks)}, timeout=600)
                        shared_prog['uploaded'] += len(chunk_data)
                        if i == total_chunks - 1:
                            up_j = _safe_json(up_r)
                            if up_j: last_rj = up_j

                url = last_rj.get("data", {}).get("url") if isinstance(last_rj.get("data"), dict) else None
                if url: shared_prog['result'] = url
                else: shared_prog['result'] = f"❌ Filemirage: selesai tapi tidak ada URL — {last_rj}"
            except Exception as e:
                shared_prog['result'] = f"❌ Filemirage exception: {e}"
            finally:
                shared_prog['done'] = True

        async def progress_updater():
            start_time = time.time()
            while not shared_prog['done']:
                await asyncio.sleep(3)
                if shared_prog['done']: break
                uploaded, total = shared_prog['uploaded'], shared_prog['total']
                percent = (uploaded / total) * 100 if total > 0 else 0
                elapsed = time.time() - start_time
                speed = uploaded / elapsed if elapsed > 0 else 0
                bar = "█" * int(15 * percent / 100) + "▒" * (15 - int(15 * percent / 100))
                text = f"⬆️ **Mengupload ke FILEMIRAGE**\n📁 `{filename}`\n[{bar}] {percent:.1f}%\n**Processed:** {_sizeof_fmt(uploaded)} / {_sizeof_fmt(total)}\n**Speed:** {_sizeof_fmt(speed)}/s"
                try: await status_msg.edit(text)
                except: pass

        updater_task = asyncio.create_task(progress_updater())
        await asyncio.to_thread(sync_upload_fm)
        await updater_task
        return shared_prog['result'] or "❌ Gagal Filemirage."
    except Exception as e:
        return f"❌ Filemirage exception: {e}"


# ── Upload: Player4me (Live Progress Bar) ─────────────────────────────────────
async def _upload_player4me(path: str, key: str, user_id: int, status_msg: Message) -> str:
    import base64
    import requests
    if not key: return "❌ Player4me butuh API key.\nGunakan: /setplayer4me API_TOKEN"
    if _CANCEL_TASKS.get(user_id): return "❌ Dibatalkan oleh pengguna."
    
    try:
        filename = os.path.basename(path)
        file_size = os.path.getsize(path)
        shared_prog = {"uploaded": 0, "total": file_size, "done": False, "result": None}

        def sync_upload_p4m():
            try:
                headers = {"api-token": key, "Accept": "application/json"}
                ep_r = requests.get(f"{P4M_BASE}/api/v1/video/upload", headers=headers, timeout=30)
                ep_j = _safe_json(ep_r)
                if not ep_j or ep_r.status_code != 200:
                    shared_prog['result'] = f"❌ Player4me TUS endpoint gagal: {ep_r.text[:200]}"
                    return

                tus_url = ep_j.get("tusUrl", "").rstrip("/") + "/"
                access_token = ep_j.get("accessToken", "")
                
                metadata = f"accessToken {base64.b64encode(access_token.encode()).decode()},filename {base64.b64encode(filename.encode()).decode()},filetype {base64.b64encode('video/mp4'.encode()).decode()}"
                create_r = requests.post(tus_url, headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(file_size), "Upload-Metadata": metadata, "Content-Length": "0", "api-token": key}, timeout=30)
                
                upload_url = create_r.headers.get("Location", "")
                if not upload_url:
                    shared_prog['result'] = "❌ Player4me TUS: tidak dapat upload location"
                    return

                offset = 0
                with open(path, "rb") as fh:
                    while offset < file_size:
                        if _CANCEL_TASKS.get(user_id): break
                        chunk = fh.read(52428800) # 50MB
                        requests.patch(upload_url, data=chunk, headers={"Tus-Resumable": "1.0.0", "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream", "Content-Length": str(len(chunk)), "api-token": key}, timeout=600)
                        offset += len(chunk)
                        shared_prog['uploaded'] = offset

                vid_id = upload_url.rstrip("/").split("/")[-1]
                if vid_id: shared_prog['result'] = f"https://player4me.com/video/{vid_id}"
                else: shared_prog['result'] = "✅ Player4me upload selesai! Cek dashboard."
            except Exception as e:
                shared_prog['result'] = f"❌ Player4me TUS exception: {e}"
            finally:
                shared_prog['done'] = True

        async def progress_updater():
            start_time = time.time()
            while not shared_prog['done']:
                await asyncio.sleep(3)
                if shared_prog['done']: break
                uploaded, total = shared_prog['uploaded'], shared_prog['total']
                percent = (uploaded / total) * 100 if total > 0 else 0
                elapsed = time.time() - start_time
                speed = uploaded / elapsed if elapsed > 0 else 0
                bar = "█" * int(15 * percent / 100) + "▒" * (15 - int(15 * percent / 100))
                text = f"⬆️ **Mengupload ke PLAYER4ME**\n📁 `{filename}`\n[{bar}] {percent:.1f}%\n**Processed:** {_sizeof_fmt(uploaded)} / {_sizeof_fmt(total)}\n**Speed:** {_sizeof_fmt(speed)}/s"
                try: await status_msg.edit(text)
                except: pass

        updater_task = asyncio.create_task(progress_updater())
        await asyncio.to_thread(sync_upload_p4m)
        await updater_task
        return shared_prog['result'] or "❌ Gagal Player4me."
    except Exception as e:
        return f"❌ Player4me exception: {e}"


# ── Upload: Transfer.it (Hybrid Playwright Login + API Murni) ─────────────────
async def _upload_transferit(path: str, key: str, user_id: int, status_msg: Message) -> str:
    import requests
    import os
    import time
    from playwright.async_api import async_playwright
    from .. import LOGGER

    if _CANCEL_TASKS.get(user_id): return "❌ Dibatalkan oleh pengguna."
    
    token = key
    # 1. Jika ada ":" berarti email:password, jalankan Playwright untuk login!
    if ":" in key:
        email, pwd = key.split(":", 1)
        await status_msg.edit("🔐 **Transfer.it:** Menggunakan Playwright untuk Auto-Login dan Ekstrak Token/SID...")
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = await browser.new_context()
                page = await context.new_page()
                
                await page.goto("https://transfer.it/login")
                await page.fill("input[type='email']", email)
                await page.fill("input[type='password']", pwd)
                await page.click("button[type='submit']")
                await page.wait_for_timeout(5000)
                
                cookies = await context.cookies()
                for c in cookies:
                    if c['name'] in ['sid', 'token', 'session']:
                        token = c['value']
                        break
                
                if token == key:
                    token = await page.evaluate("localStorage.getItem('token') || localStorage.getItem('sid')")
                    
                await browser.close()
        except Exception as e:
            return f"❌ Gagal Auto-Login Playwright: {e}"

        if not token or token == key:
            return "❌ Gagal mengekstrak Token/SID dari Transfer.it. Cek Email/Password Anda."

    # 2. Upload Murni via API (Sangat Cepat + Live Progress)
    filename = os.path.basename(path)
    total_size = os.path.getsize(path)
    shared_prog = {"uploaded": 0, "total": total_size, "done": False, "result": None}

    def sync_upload_chunk():
        try:
            boundary = '----WebKitFormBoundaryTransferItAPI'
            def file_gen():
                head = (f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                        f"Content-Type: application/octet-stream\r\n\r\n").encode('utf-8')
                yield head
                shared_prog['uploaded'] += len(head)
                
                with open(path, "rb") as f:
                    while chunk := f.read(262144): # 256KB chunks
                        if _CANCEL_TASKS.get(user_id): break
                        yield chunk
                        shared_prog['uploaded'] += len(chunk)
                        
                tail = f"\r\n--{boundary}--\r\n".encode('utf-8')
                yield tail
                shared_prog['uploaded'] += len(tail)

            headers = {
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Authorization': f'Bearer {token}',
                'Cookie': f'sid={token}'
            }
            
            r = requests.post("https://transfer.it/api/upload", data=file_gen(), headers=headers, timeout=600)
            rj = _safe_json(r)
            
            if r.status_code in [200, 201] and rj:
                link = rj.get('url') or rj.get('link') or rj.get('data', {}).get('url')
                shared_prog['result'] = link if link else f"Sukses, tapi URL tidak ditemukan: {rj}"
            else:
                shared_prog['result'] = f"❌ Transfer.it API Error {r.status_code}: {r.text[:200]}"
        except Exception as e:
            shared_prog['result'] = f"❌ Transfer.it Exception: {e}"
        finally:
            shared_prog['done'] = True

    async def progress_updater():
        start_time = time.time()
        while not shared_prog['done']:
            await asyncio.sleep(3)
            if shared_prog['done']: break
            uploaded, total = shared_prog['uploaded'], shared_prog['total']
            percent = (uploaded / total) * 100 if total > 0 else 0
            elapsed = time.time() - start_time
            speed = uploaded / elapsed if elapsed > 0 else 0
            bar = "█" * int(15 * percent / 100) + "▒" * (15 - int(15 * percent / 100))
            text = (f"⬆️ **Mengupload ke TRANSFER.IT (API Mode)**\n"
                    f"📁 `{filename}`\n"
                    f"[{bar}] {percent:.1f}%\n"
                    f"**Processed:** {_sizeof_fmt(uploaded)} / {_sizeof_fmt(total)}\n"
                    f"**Speed:** {_sizeof_fmt(speed)}/s")
            try: await status_msg.edit(text)
            except: pass

    updater_task = asyncio.create_task(progress_updater())
    await asyncio.to_thread(sync_upload_chunk)
    await updater_task
    
    return shared_prog['result'] or "❌ Gagal mengunggah ke Transfer.it."


# ── Upload: Akirabox (Standard) ───────────────────────────────────────────────
def _upload_akirabox(path: str, key: str, user_id: int) -> str:
    import requests
    if _CANCEL_TASKS.get(user_id): return "❌ Dibatalkan oleh pengguna."
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


# ── Telegram Handlers ─────────────────────────────────────────────────────────
@new_task
async def set_api_key_cmd(_, message):
    parts = message.text.strip().split(maxsplit=1)
    host  = parts[0].lstrip("/").lower().replace("set", "", 1)
    if len(parts) < 2:
        await message.reply(f"Gunakan: <code>/set{host} API_KEY</code>")
        return
    _API_KEYS[host] = parts[1].strip()
    await _db_save_keys()
    await message.reply(f"✅ Credentials <b>{host}</b> berhasil disimpan!")

@new_task
async def cancel_upload_cmd(_, message: Message):
    _CANCEL_TASKS[message.from_user.id] = True
    await message.reply("🛑 Pembatalan dikirim...")

@new_task
async def multi_mirror_cmd(_, message):
    parts = message.text.strip().split(maxsplit=1)
    host  = parts[0].lstrip("/").lower()
    user_id = message.from_user.id
    _CANCEL_TASKS[user_id] = False

    func = _UPLOAD_FUNCS.get(host)
    if not func: return

    if len(parts) < 2:
        await message.reply(f"Gunakan: <code>/{host} <url_atau_path></code>")
        return

    arg = parts[1].strip()
    is_url = arg.startswith(("http://", "https://"))
    is_abs = arg.startswith("/")
    status_msg = await message.reply("🔍 Memproses…")

    need_del = False
    dest = None

    if is_url:
        fname = arg.split("/")[-1].split("?")[0].strip() or "file_download"
        os.makedirs(TEMP_DIR, exist_ok=True)
        dest = os.path.join(TEMP_DIR, fname)
        await status_msg.edit(f"⬇️ Mengunduh dari URL…\n<code>{arg}</code>")
        ok, err = await to_thread(_download_url, arg, dest, user_id)
        if not ok:
            await status_msg.edit(f"❌ Gagal download: {err}")
            return
        need_del = True
    elif is_abs:
        dest = arg
        if not os.path.exists(dest):
            await status_msg.edit(f"❌ File tidak ada: {dest}")
            return
    else:
        dest = await to_thread(_find_file_in_downloads, arg)
        if not dest:
            await status_msg.edit(f"❌ File <code>{arg}</code> tidak ditemukan di downloads.")
            return

    size_str = _sizeof_fmt(os.path.getsize(dest))
    key = _API_KEYS.get(host, "")

    if inspect.iscoroutinefunction(func):
        link = await func(dest, key, user_id, status_msg)
    else:
        link = await to_thread(func, dest, key, user_id)

    if need_del:
        try: os.remove(dest)
        except: pass

    if link and link.startswith("http"):
        await status_msg.edit(f"✅ <b>Upload ke {host.capitalize()} selesai!</b>\n\n📁 {os.path.basename(dest)} ({size_str})\n🔗 {link}")
    else:
        await status_msg.edit(link)


TgClient.bot.add_handler(MessageHandler(set_api_key_cmd, filters=command(SET_HOST_LIST) & CustomFilters.sudo))
TgClient.bot.add_handler(MessageHandler(multi_mirror_cmd, filters=command(HOST_LIST) & CustomFilters.authorized))
TgClient.bot.add_handler(MessageHandler(cancel_upload_cmd, filters=command("cancelup") & CustomFilters.authorized))
