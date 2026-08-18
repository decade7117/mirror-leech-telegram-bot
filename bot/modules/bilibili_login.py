"""
============================================================
 bot/modules/bilibili_login.py
 FITUR:
 - /bililogin      → simpan cookies bilibili.tv (upload file JSON)
 - /biliaccounts   → lihat semua akun yang sudah login
 - /bililogout     → logout / hapus akun
 - /biliupload     → upload video via direct URL ke bilibili.tv
 - /biliset        → atur default tags, judul, deskripsi, mode
 - /bilicancel     → batalkan sesi login yang aktif
 - /cancelbili     → batalkan proses upload bilibili yang sedang berjalan
============================================================
"""

import asyncio
import json
import os
import time
import uuid
import math
import httpx
from pathlib import Path

from pyrogram import filters
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
)

from .. import LOGGER
from ..core.telegram_manager import TgClient
from ..helper.telegram_helper.filters import CustomFilters
from ..helper.ext_utils.bot_utils import new_task

BILI_DIR = Path("/app/bili_accounts")
BILI_DIR.mkdir(parents=True, exist_ok=True)
BILI_SETTINGS_FILE = BILI_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "tags": ["anime", "indonesia"],
    "title_prefix": "",
    "desc": "",
    "copyright": 1,
    "account_mode": "round_robin", 
}

_login_sessions: dict = {}
_CANCEL_BILI: dict = {}
_RETRY_TASKS: dict = {}  
bili_upload_lock = asyncio.Lock()

def load_settings() -> dict:
    if BILI_SETTINGS_FILE.exists():
        try: return json.loads(BILI_SETTINGS_FILE.read_text())
        except: pass
    return DEFAULT_SETTINGS.copy()

def save_settings(s: dict):
    BILI_SETTINGS_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))

def list_accounts() -> list[dict]:
    accounts = []
    for f in sorted(BILI_DIR.glob("cookies_*.json")):
        name = f.stem.replace("cookies_", "")
        try:
            data = json.loads(f.read_text())
            valid = bool(data)
        except Exception:
            valid = False
        accounts.append({"name": name, "path": str(f), "valid": valid})
    return accounts

def next_account_name() -> str:
    return f"akun{len(list_accounts()) + 1}"

def get_cookie_path(name: str) -> Path:
    return BILI_DIR / f"cookies_{name}.json"

def _sizeof_fmt(num_bytes: int) -> str:
    if num_bytes >= 1024 ** 3: return f"{num_bytes / 1024 ** 3:.2f} GB"
    if num_bytes >= 1024 ** 2: return f"{num_bytes / 1024 ** 2:.2f} MB"
    return f"{num_bytes / 1024:.2f} KB"

async def _download_video_async(url: str, user_id: int, shared_prog: dict) -> tuple[str | None, str | None]:
    """Download menggunakan httpx murni untuk mencegah stuck & mendeteksi file korup (VPN Drop)"""
    try:
        filename = url.rstrip("/").split("/")[-1].split("?")[0] or f"vid_{int(time.time())}.mp4"
        tmp_path = f"/tmp/{filename}"
        
        shared_prog["status"] = "Downloading"
        limits = httpx.Timeout(60.0, connect=30.0)
        
        async with httpx.AsyncClient(follow_redirects=True, timeout=limits) as client:
            async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                resp.raise_for_status()
                total_size = int(resp.headers.get("Content-Length", 0))
                shared_prog["dl_total"] = total_size
                shared_prog["dl_downloaded"] = 0
                
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=1024*1024):
                        if _CANCEL_BILI.get(user_id):
                            return None, "Dibatalkan oleh pengguna."
                        f.write(chunk)
                        shared_prog["dl_downloaded"] += len(chunk)
                        
                # Validasi Anti-Korup: Pastikan ukuran yang didownload = Content Length
                if total_size > 0 and shared_prog["dl_downloaded"] < total_size:
                    raise Exception(f"Download terputus sebelum selesai (Hanya dpt {shared_prog['dl_downloaded']} dari {total_size} bytes). Koneksi/VPN mungkin mati.")
                    
        return tmp_path, None
    except Exception as e:
        return None, str(e)

async def _do_upload_playwright(video_path: str, account: dict, title: str, tags: str, desc: str, custom_cover: str, user_id: int, shared_prog: dict) -> tuple[bool, str]:
    try:
        cookies = json.loads(Path(account["path"]).read_text())
        if isinstance(cookies, list): cookies = {c["name"]: c["value"] for c in cookies}
    except Exception as e: return False, f"Error cookie: {e}"

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    filename = Path(video_path).name
    filesize = os.path.getsize(video_path)

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/143.0.0.0 Mobile Safari/537.36",
        "Origin": "https://studio.bilibili.tv",
        "Referer": "https://studio.bilibili.tv/",
        "Cookie": cookie_header,
    }

    CHUNK_SIZE = 10 * 1024 * 1024
    acc_name = account['name']
    limits = httpx.Timeout(120.0, connect=30.0)

    async with httpx.AsyncClient(timeout=limits, follow_redirects=True) as client:
        # 1. Preupload
        try:
            r = await client.get("https://api.bilibili.tv/preupload", params={"name": filename, "size": filesize, "r": "upos", "profile": "iup/bup", "ssl": "0", "version": "2.10.0", "build": "2100000", "biz": "UGC"}, headers=base_headers)
            pre = r.json()
            if pre.get("OK") != 1: return False, f"Preupload error: {pre}"
        except Exception as e: return False, f"Preupload error: {e}"

        upload_url = pre["endpoint"] + pre["upos_uri"].replace("upos://", "/")
        if upload_url.startswith("//"): upload_url = "https:" + upload_url
        upos_headers = {**base_headers, "X-Upos-Auth": pre["auth"], "Content-Type": "application/octet-stream"}

        # 2. Init
        upload_id = None
        for attempt in range(1, 4):
            try:
                r = await client.post(upload_url, params={"uploads": "", "output": "json"}, headers={**upos_headers, "Content-Type": "application/json"}, content=b"")
                upload_id = r.json().get("upload_id") or r.json().get("uploadId")
                if upload_id: break
            except Exception as e:
                if attempt == 3: return False, f"Init upload timeout: {e}"
                await asyncio.sleep(2)

        # 3. Chunks
        total_chunks = (filesize + CHUNK_SIZE - 1) // CHUNK_SIZE
        parts = []
        with open(video_path, "rb") as f:
            for chunk_idx in range(total_chunks):
                if _CANCEL_BILI.get(user_id): return False, "Upload dibatalkan."
                
                start, end = chunk_idx * CHUNK_SIZE, min((chunk_idx + 1) * CHUNK_SIZE, filesize)
                f.seek(start)
                chunk_data = f.read(end - start)
                
                # Toleransi 5x percobaan per chunk jika user sedang ganti VPN
                for attempt in range(5):
                    try:
                        r = await client.put(upload_url, params={"partNumber": chunk_idx+1, "uploadId": upload_id, "chunk": chunk_idx, "chunks": total_chunks, "size": end-start, "start": start, "end": end, "total": filesize}, content=chunk_data, headers=upos_headers, timeout=120)
                        if r.status_code in (200, 204): 
                            shared_prog["up_progress"][acc_name]["uploaded"] += len(chunk_data)
                            break
                    except Exception as e:
                        if attempt == 4: return False, f"Gagal upload chunk {chunk_idx+1}: {e}"
                        await asyncio.sleep(4)
                
                parts.append({"partNumber": chunk_idx + 1, "eTag": "etag"})

        # 4. Complete
        try:
            r = await client.post(upload_url, params={"output": "json", "name": filename, "profile": "iup/bup", "uploadId": upload_id, "biz_id": str(pre.get("biz_id", "")), "biz": "UGC"}, json={"parts": parts}, headers={**upos_headers, "Content-Type": "application/json; charset=UTF-8"}, timeout=60)
            complete_data = r.json()
        except Exception as e: return False, f"Complete error: {e}"

        # 5. Submit
        video_key = complete_data.get("key", "").strip("/")
        filename_only = video_key.replace(".mp4", "")
        submit_params = {"lang_id": "3", "platform": "web", "lang": "en_US", "s_locale": "en_US", "timezone": "GMT+07:00", "csrf": cookies.get("bili_jct", "") or cookies.get("joy_jct", "")}
        final_cover = custom_cover if custom_cover else "https://p.bstarstatic.com/ugc/a81bfcb06c220955768404166a1f856b.jpg"
        submit_data = {"title": title[:80], "cover": final_cover, "desc": desc, "no_reprint": True, "filename": filename_only, "playlist_id": "", "visibility": 0, "subtitle_id": None, "subtitle_lang_id": None, "from_spmid": "333.1011", "copyright": 1, "tag": tags or "anime"}

        try:
            r = await client.post("https://api.bilibili.tv/intl/videoup/web2/add", params=submit_params, json=submit_data, headers={**base_headers, "Content-Type": "application/json"}, timeout=60)
            try: res = r.json()
            except Exception: return False, f"API Error: HTTP {r.status_code}"
            if res.get("code") == 0: return True, "Upload & Submit Berhasil! ✅"
            return False, f"Submit gagal: {res}"
        except Exception as e:
            return False, f"Submit Exception: {e}"

async def _core_bili_upload_loop(status_msg, url, title, desc, tags_str, custom_cover, target_accounts, user_id, mode):
    shared_prog = {
        "done": False, 
        "status": "Starting", 
        "dl_total": 0, 
        "dl_downloaded": 0, 
        "up_progress": {}
    }

    async def progress_updater():
        last_text = ""
        start_time = time.time()
        while not shared_prog["done"]:
            await asyncio.sleep(4)
            if shared_prog["done"]: break
            
            elapsed = time.time() - start_time
            text = f"🔄 **Proses Bilibili**\n📝 `{title}`\n\n"
            
            if shared_prog["status"] == "Downloading":
                dl = shared_prog["dl_downloaded"]
                tot = shared_prog["dl_total"]
                pct = (dl / tot) * 100 if tot > 0 else 0
                speed = dl / elapsed if elapsed > 0 else 0
                bar = "█" * int(15 * pct / 100) + "▒" * (15 - int(15 * pct / 100))
                text += f"⬇️ **Mendownload Video:**\n[{bar}] {pct:.1f}%\n"
                text += f"Ukuran: {_sizeof_fmt(dl)} / {_sizeof_fmt(tot)}\n"
                text += f"Speed: {_sizeof_fmt(speed)}/s"
                
            elif shared_prog["status"] == "Uploading":
                if mode == "queue_oneall":
                    text += f"🚀 **Upload Paralel ({len(target_accounts)} Akun):**\n"
                else:
                    text += f"🚀 **Upload Sekuensial:**\n"
                    
                for acc_name, prog in shared_prog["up_progress"].items():
                    pct = (prog["uploaded"] / prog["total"]) * 100 if prog["total"] > 0 else 0
                    if pct == 100:
                        text += f"✅ {acc_name}: Selesai\n"
                    else:
                        text += f"⏳ {acc_name}: {pct:.1f}%\n"
            
            text += "\n<i>Ketik /cancelbili untuk batal</i>"
            
            if text != last_text:
                try: 
                    await status_msg.edit(text)
                    last_text = text
                except: pass

    updater_task = asyncio.create_task(progress_updater())
    
    video_path, err = await _download_video_async(url, user_id, shared_prog)
    
    if not video_path: 
        shared_prog["done"] = True
        await updater_task
        return None, target_accounts, err

    shared_prog["status"] = "Uploading"
    results = []
    failed_accounts = []
    
    try:
        if mode == "queue_oneall":
            tasks = []
            for acc in target_accounts:
                shared_prog["up_progress"][acc["name"]] = {"uploaded": 0, "total": os.path.getsize(video_path)}
                tasks.append(_do_upload_playwright(video_path, acc, title, tags_str, desc, custom_cover, user_id, shared_prog))
            
            res_list = await asyncio.gather(*tasks, return_exceptions=True)
            
            for acc, res in zip(target_accounts, res_list):
                if isinstance(res, Exception):
                    failed_accounts.append(acc)
                    results.append(f"❌ <b>{acc['name']}</b>: Exception {res}")
                else:
                    ok, detail = res
                    emoji = "✅" if ok else "❌"
                    results.append(f"{emoji} <b>{acc['name']}</b>: {detail}")
                    if not ok: failed_accounts.append(acc)
        else:
            for i, acc in enumerate(target_accounts):
                if _CANCEL_BILI.get(user_id):
                    results.append("🛑 Upload dibatalkan manual.")
                    break
                
                shared_prog["up_progress"][acc["name"]] = {"uploaded": 0, "total": os.path.getsize(video_path)}
                ok, detail = await _do_upload_playwright(video_path, acc, title, tags_str, desc, custom_cover, user_id, shared_prog)
                
                emoji = "✅" if ok else "❌"
                results.append(f"{emoji} <b>{acc['name']}</b>: {detail}")
                
                if not ok: failed_accounts.append(acc)
                if i < len(target_accounts) - 1: await asyncio.sleep(2)
                
    finally:
        shared_prog["done"] = True
        await updater_task
        if video_path and os.path.exists(video_path): os.unlink(video_path)

    return results, failed_accounts, None

@new_task
async def bili_login_cmd(client, message: Message):
    user_id = message.from_user.id
    text = message.text or message.caption or ""
    args = text.split(maxsplit=1)
    akun_name = args[1].strip() if len(args) > 1 else next_account_name()
    _login_sessions[user_id] = {"akun_name": akun_name, "waiting_file": True}

    await message.reply(
        f"📂 <b>Upload Cookies Bilibili</b>\n\n👤 Akun: <b>{akun_name}</b>\n\n"
        "Kirim file <code>cookies.json</code> kamu sekarang.\n"
        "⏱ Menunggu file selama 5 menit... (/bilicancel untuk batal)"
    )
    asyncio.get_event_loop().create_task(_expire_login_session(user_id, client, message.chat.id))

async def _expire_login_session(user_id: int, client, chat_id: int):
    await asyncio.sleep(300)
    session = _login_sessions.get(user_id)
    if session and session.get("waiting_file"):
        _login_sessions.pop(user_id, None)
        await client.send_message(chat_id, "⏱ Sesi login timeout. Ketik /bililogin untuk coba lagi.")

@new_task
async def bili_receive_cookie_file(client, message: Message):
    user_id = message.from_user.id
    session = _login_sessions.get(user_id)
    if not session or not session.get("waiting_file"): return
    if not message.document or not (message.document.file_name or "").endswith(".json"):
        return await message.reply("❌ Kirim sebagai file <code>.json</code>.")

    akun_name = session["akun_name"]
    cookie_path = get_cookie_path(akun_name)
    status_msg = await message.reply(f"⏳ Memproses cookies <b>{akun_name}</b>...")
    tmp_path = f"/tmp/cookies_{user_id}.json"
    await client.download_media(message, file_name=tmp_path)

    try:
        data = json.loads(Path(tmp_path).read_text(encoding="utf-8"))
        if isinstance(data, list): data = {item["name"]: item["value"] for item in data if isinstance(item, dict) and "name" in item and "value" in item}
        cookie_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        await status_msg.edit(f"✅ <b>Cookies {akun_name} disimpan!</b>\nTotal akun: {len(list_accounts())}")
    except Exception as e:
        await status_msg.edit(f"❌ JSON tidak valid:\n<code>{e}</code>")
    finally:
        _login_sessions.pop(user_id, None)
        if os.path.exists(tmp_path): os.unlink(tmp_path)

@new_task
async def bili_accounts_cmd(client, message: Message):
    accounts, settings = list_accounts(), load_settings()
    if not accounts: return await message.reply("📭 Belum ada akun. Gunakan /bililogin")

    lines = ["<b>📋 Daftar Akun Bilibili</b>\n"]
    for i, acc in enumerate(accounts, 1):
        lines.append(f"{i}. {'✅' if acc['valid'] else '⚠️'} <b>{acc['name']}</b>")
    lines.append(f"\n🏷 Tags: {', '.join(f'#{t}' for t in settings.get('tags', [])) or '-'}")
    lines.append(f"🔄 Mode: <b>{settings.get('account_mode', 'round_robin')}</b>")
    await message.reply("\n".join(lines), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Login Baru", callback_data="bili_new_login"), InlineKeyboardButton("⚙️ Set", callback_data="bili_settings")]]))

@new_task
async def bili_logout_cmd(client, message: Message):
    text = message.text or message.caption or ""
    args = text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("Gunakan: <code>/bililogout [nama_akun]</code>")
    name = args[1].strip()
    cookie_path = get_cookie_path(name)
    if cookie_path.exists():
        cookie_path.unlink()
        await message.reply(f"✅ Akun <b>{name}</b> dihapus.")
    else: await message.reply(f"❌ Akun <b>{name}</b> tidak ada.")

@new_task
async def bili_set_cmd(client, message: Message):
    text = message.text or message.caption or ""
    args = text.split(maxsplit=2)
    settings = load_settings()
    if len(args) < 3: return await message.reply("Cara pakai:\n/biliset tags anime,gaming\n/biliset mode queue_oneall\n/biliset prefix Judul")
    key, val = args[1].lower(), args[2].strip()
    
    if key == "tags":
        settings["tags"] = [t.strip().lstrip("#") for t in val.split(",") if t.strip()]
        await message.reply(f"✅ Tags diset: {' '.join(f'#{t}' for t in settings['tags'])}")
    elif key == "mode":
        if val not in ("all", "round_robin", "queue_oneall"): return await message.reply("❌ Mode harus 'all', 'round_robin', atau 'queue_oneall'")
        settings["account_mode"] = val
        await message.reply(f"✅ Mode akun diset: <b>{val}</b>")
    elif key == "prefix":
        settings["title_prefix"] = val
        await message.reply(f"✅ Prefix judul diset: {val}")
    elif key == "desc":
        settings["desc"] = val
        await message.reply(f"✅ Deskripsi default diset: {val}")
    else: return await message.reply(f"❌ Pengaturan '{key}' tidak dikenali.")
    save_settings(settings)

@new_task
async def cancel_bili_cmd(client, message: Message):
    _CANCEL_BILI[message.from_user.id] = True
    await message.reply("🛑 Pembatalan Bilibili dikirim. Menunggu proses saat ini berhenti...")

@new_task
async def bili_upload_cmd(client, message: Message):
    accounts = [a for a in list_accounts() if a["valid"]]
    if not accounts: return await message.reply("❌ Belum ada akun Bilibili valid!")

    user_id = message.from_user.id
    _CANCEL_BILI[user_id] = False
    settings = load_settings()
    text = message.text or message.caption or ""
    args = text.split(maxsplit=1)
    if len(args) < 2: return await message.reply("❌ Format: <code>/biliupload &lt;url&gt; | &lt;judul&gt; | &lt;desc&gt; | &lt;cover&gt;</code>")

    parts = [p.strip() for p in args[1].strip().split("|")]
    url = parts[0]
    custom_title = parts[1] if len(parts) > 1 and parts[1] else None
    custom_desc  = parts[2] if len(parts) > 2 and parts[2] else None
    custom_cover = parts[3] if len(parts) > 3 and parts[3] else None

    if not url.startswith("http"): return await message.reply("❌ URL video tidak valid.")

    url_filename = url.rstrip("/").split("/")[-1].split("?")[0]
    title = custom_title or (url_filename.rsplit(".", 1)[0] if "." in url_filename else url_filename)
    if settings.get("title_prefix"): title = f"{settings['title_prefix']} {title}"
    desc = custom_desc or settings.get("desc", "")
    tags_str = ",".join(settings.get("tags", []))
    
    mode = settings.get("account_mode", "round_robin")
    if mode in ("all", "queue_oneall"): target_accounts = accounts
    else: target_accounts = [accounts[int(time.time()) % len(accounts)]]

    if bili_upload_lock.locked(): status_msg = await message.reply(f"⏳ <b>Menunggu antrean Bilibili...</b>\n📝 {title}")
    else: status_msg = await message.reply(f"🔄 Memulai Bilibili...\n📝 {title}")

    async with bili_upload_lock:
        results, failed_accounts, err_msg = await _core_bili_upload_loop(status_msg, url, title, desc, tags_str, custom_cover, target_accounts, user_id, mode)

    if results is None:
        if _CANCEL_BILI.get(user_id):
            await status_msg.edit(f"🛑 <b>Download dibatalkan manual:</b> {err_msg}")
            return
            
        task_id = str(uuid.uuid4())[:8]
        _RETRY_TASKS[task_id] = {
            "url": url, "title": title, "desc": desc, "tags_str": tags_str, 
            "custom_cover": custom_cover, "failed_accounts": failed_accounts, "user_id": user_id, "mode": mode
        }
        
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔄 Ulangi Semua ({len(failed_accounts)} Akun)", callback_data=f"bili_retry_{task_id}")]])
        await status_msg.edit(f"❌ <b>Download gagal/Batal:</b> {err_msg}", reply_markup=reply_markup)
        return

    text_result = f"📊 <b>Hasil Upload Bilibili</b>\n📝 {title}\n\n" + "\n".join(results)

    if failed_accounts:
        task_id = str(uuid.uuid4())[:8]
        _RETRY_TASKS[task_id] = {
            "url": url, "title": title, "desc": desc, "tags_str": tags_str, 
            "custom_cover": custom_cover, "failed_accounts": failed_accounts, "user_id": user_id, "mode": mode
        }
        
        reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔄 Ulangi yang Gagal ({len(failed_accounts)} Akun)", callback_data=f"bili_retry_{task_id}")]])
        await status_msg.edit(text_result, reply_markup=reply_markup)
    else:
        await status_msg.edit(text_result)

@new_task
async def bili_cancel_cmd(client, message: Message):
    user_id = message.from_user.id
    if user_id in _login_sessions:
        del _login_sessions[user_id]
        await message.reply("✅ Sesi login batal.")
    else: await message.reply("Tidak ada sesi login aktif.")

@new_task
async def bili_callback(client, query: CallbackQuery):
    if query.data == "bili_new_login":
        await query.answer(); await query.message.reply("Gunakan: <code>/bililogin akun1</code>")
    elif query.data == "bili_settings":
        await query.answer(); await query.message.reply("Gunakan: /biliset")
    
    elif query.data.startswith("bili_retry_"):
        task_id = query.data.split("_")[-1]
        task = _RETRY_TASKS.get(task_id)
        
        if not task:
            return await query.answer("❌ Data retry sudah kedaluwarsa.", show_alert=True)
            
        await query.answer(f"🔄 Memulai ulang untuk {len(task['failed_accounts'])} akun...")
        
        url = task["url"]
        title = task["title"]
        desc = task["desc"]
        tags_str = task["tags_str"]
        custom_cover = task["custom_cover"]
        target_accounts = task["failed_accounts"]
        user_id = query.from_user.id
        mode = task.get("mode", "all")
        
        _CANCEL_BILI[user_id] = False
        
        if bili_upload_lock.locked(): 
            await query.message.edit(f"{query.message.text}\n\n⏳ <b>Menunggu antrean retry Bilibili...</b>")
            
        async with bili_upload_lock:
            results, new_failed_accounts, err_msg = await _core_bili_upload_loop(query.message, url, title, desc, tags_str, custom_cover, target_accounts, user_id, mode)
            
        if results is None:
            if _CANCEL_BILI.get(user_id):
                await query.message.edit(f"🛑 <b>Retry dibatalkan manual:</b> {err_msg}")
                _RETRY_TASKS.pop(task_id, None)
                return
                
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔄 Ulangi Download (Sisa {len(target_accounts)} Akun)", callback_data=f"bili_retry_{task_id}")]])
            await query.message.edit(f"❌ <b>Download gagal lagi:</b> {err_msg}", reply_markup=markup)
            return
        
        text_result = f"📊 <b>Hasil Retry Bilibili</b>\n📝 {title}\n\n" + "\n".join(results)
        
        if new_failed_accounts:
            _RETRY_TASKS[task_id]["failed_accounts"] = new_failed_accounts
            markup = InlineKeyboardMarkup([[InlineKeyboardButton(f"🔄 Ulangi Sisa {len(new_failed_accounts)} Akun Gagal", callback_data=f"bili_retry_{task_id}")]])
            await query.message.edit(text_result, reply_markup=markup)
        else:
            await query.message.edit(text_result)
            _RETRY_TASKS.pop(task_id, None)

TgClient.bot.add_handler(MessageHandler(bili_login_cmd, filters=filters.command("bililogin") & CustomFilters.authorized))
TgClient.bot.add_handler(MessageHandler(bili_receive_cookie_file, filters=filters.document & CustomFilters.authorized))
TgClient.bot.add_handler(MessageHandler(bili_accounts_cmd, filters=filters.command("biliaccounts") & CustomFilters.authorized))
TgClient.bot.add_handler(MessageHandler(bili_logout_cmd, filters=filters.command("bililogout") & CustomFilters.authorized))
TgClient.bot.add_handler(MessageHandler(bili_set_cmd, filters=filters.command("biliset") & CustomFilters.authorized))
TgClient.bot.add_handler(MessageHandler(bili_upload_cmd, filters=filters.command("biliupload") & CustomFilters.authorized))
TgClient.bot.add_handler(MessageHandler(bili_cancel_cmd, filters=filters.command("bilicancel") & CustomFilters.authorized))
TgClient.bot.add_handler(MessageHandler(cancel_bili_cmd, filters=filters.command("cancelbili") & CustomFilters.authorized))
TgClient.bot.add_handler(CallbackQueryHandler(bili_callback, filters=filters.regex(r"^bili_")))
LOGGER.info("bilibili_login: ✅ semua handler Bilibili berhasil didaftarkan")
