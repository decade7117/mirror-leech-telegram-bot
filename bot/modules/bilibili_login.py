"""
============================================================
 bot/modules/bilibili_login.py
 FITUR:
 - /bililogin      → simpan cookies bilibili.tv (upload file JSON)
 - /biliaccounts   → lihat semua akun yang sudah login
 - /bililogout     → logout / hapus akun
 - /biliupload     → upload video via direct URL ke bilibili.tv
 - /biliset        → atur default tags, judul, deskripsi
 - /bilicancel     → batalkan sesi login yang aktif
 - /cancelbili     → batalkan proses upload bilibili yang sedang berjalan
============================================================
"""

import asyncio
import json
import os
import time
import urllib.request
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
_CANCEL_BILI: dict = {}  # Flag untuk membatalkan proses upload
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

def _download_from_url(url: str, user_id: int) -> tuple[str | None, str | None]:
    try:
        filename = url.rstrip("/").split("/")[-1].split("?")[0] or f"vid_{int(time.time())}.mp4"
        tmp_path = f"/tmp/{filename}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            with open(tmp_path, "wb") as f:
                while True:
                    if _CANCEL_BILI.get(user_id):
                        return None, "Dibatalkan oleh pengguna."
                    chunk = resp.read(1024 * 1024)
                    if not chunk: break
                    f.write(chunk)
        return tmp_path, None
    except Exception as e:
        return None, str(e)

async def _do_upload_playwright(video_path: str, account: dict, title: str, tags: str, desc: str, custom_cover: str = None, user_id: int = 0) -> tuple[bool, str]:
    import httpx
    try:
        cookies = json.loads(Path(account["path"]).read_text())
        if isinstance(cookies, list): cookies = {c["name"]: c["value"] for c in cookies}
    except Exception as e: return False, f"Error cookie: {e}"

    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
    filename = Path(video_path).name
    filesize = Path(video_path).stat().st_size

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Mobile Safari/537.36",
        "Origin": "https://studio.bilibili.tv",
        "Referer": "https://studio.bilibili.tv/",
        "Cookie": cookie_header,
    }

    CHUNK_SIZE = 10 * 1024 * 1024

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        try:
            r = await client.get("https://api.bilibili.tv/preupload", params={"name": filename, "size": filesize, "r": "upos", "profile": "iup/bup", "ssl": "0", "version": "2.10.0", "build": "2100000", "biz": "UGC"}, headers=base_headers)
            pre = r.json()
            if pre.get("OK") != 1: return False, f"Preupload error: {pre}"
        except Exception as e: return False, f"Preupload timeout/error: {e}"

        upload_url = pre["endpoint"] + pre["upos_uri"].replace("upos://", "/")
        if upload_url.startswith("//"): upload_url = "https:" + upload_url
        upos_headers = {**base_headers, "X-Upos-Auth": pre["auth"], "Content-Type": "application/octet-stream"}

        try:
            r = await client.post(upload_url, params={"uploads": "", "output": "json"}, headers={**upos_headers, "Content-Type": "application/json"}, content=b"")
            upload_id = r.json().get("upload_id") or r.json().get("uploadId")
        except Exception as e: return False, f"Init upload timeout: {e}"

        total_chunks = (filesize + CHUNK_SIZE - 1) // CHUNK_SIZE
        parts = []
        with open(video_path, "rb") as f:
            for chunk_idx in range(total_chunks):
                if _CANCEL_BILI.get(user_id): return False, "Upload dibatalkan."
                
                start, end = chunk_idx * CHUNK_SIZE, min((chunk_idx + 1) * CHUNK_SIZE, filesize)
                f.seek(start)
                chunk_data = f.read(end - start)
                for attempt in range(3):
                    try:
                        r = await client.put(upload_url, params={"partNumber": chunk_idx+1, "uploadId": upload_id, "chunk": chunk_idx, "chunks": total_chunks, "size": end-start, "start": start, "end": end, "total": filesize}, content=chunk_data, headers=upos_headers, timeout=120)
                        if r.status_code in (200, 204): break
                    except Exception:
                        if attempt == 2: return False, f"Gagal upload chunk {chunk_idx+1} (Timeout)"
                        await asyncio.sleep(3)
                parts.append({"partNumber": chunk_idx + 1, "eTag": "etag"})

        try:
            r = await client.post(upload_url, params={"output": "json", "name": filename, "profile": "iup/bup", "uploadId": upload_id, "biz_id": str(pre.get("biz_id", "")), "biz": "UGC"}, json={"parts": parts}, headers={**upos_headers, "Content-Type": "application/json; charset=UTF-8"}, timeout=60)
            complete_data = r.json()
        except Exception as e: return False, f"Complete upload error: {e}"

        video_key = complete_data.get("key", "").strip("/")
        filename_only = video_key.replace(".mp4", "")
        submit_params = {"lang_id": "3", "platform": "web", "lang": "en_US", "s_locale": "en_US", "timezone": "GMT+07:00", "csrf": cookies.get("bili_jct", "") or cookies.get("joy_jct", "")}
        final_cover = custom_cover if custom_cover else "https://p.bstarstatic.com/ugc/a81bfcb06c220955768404166a1f856b.jpg"
        submit_data = {"title": title[:80], "cover": final_cover, "desc": desc, "no_reprint": True, "filename": filename_only, "playlist_id": "", "visibility": 0, "subtitle_id": None, "subtitle_lang_id": None, "from_spmid": "333.1011", "copyright": 1, "tag": tags or "anime"}

        try:
            r = await client.post("https://api.bilibili.tv/intl/videoup/web2/add", params=submit_params, json=submit_data, headers={**base_headers, "Content-Type": "application/json"}, timeout=60)
            try: res = r.json()
            except Exception: return False, f"API Error: {r.status_code}"
            if res.get("code") == 0: return True, "Upload & Submit Berhasil! ✅"
            return False, f"Submit gagal: {res}"
        except Exception as e:
            return False, f"Submit Exception: {e}"

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
    if len(args) < 3: return await message.reply("Cara pakai:\n/biliset tags anime,gaming\n/biliset mode all\n/biliset prefix Judul")
    key, val = args[1].lower(), args[2].strip()
    
    if key == "tags":
        settings["tags"] = [t.strip().lstrip("#") for t in val.split(",") if t.strip()]
        await message.reply(f"✅ Tags diset: {' '.join(f'#{t}' for t in settings['tags'])}")
    elif key == "mode":
        if val not in ("all", "round_robin"): return await message.reply("❌ Mode harus 'all' atau 'round_robin'")
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
    await message.reply("🛑 Permintaan pembatalan Bilibili dikirim. Menunggu proses saat ini dihentikan...")

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
    
    target_accounts = accounts if settings.get("account_mode") == "all" else [accounts[int(time.time()) % len(accounts)]]

    if bili_upload_lock.locked(): status_msg = await message.reply(f"⏳ <b>Menunggu antrean upload Bilibili...</b>\n📝 {title}")
    else: status_msg = await message.reply(f"🔄 Memulai proses Bilibili...\n📝 {title}")

    async with bili_upload_lock:
        await status_msg.edit(f"⬇️ <b>Mendownload video...</b>\n📝 Judul: {title}\n🖼 Cover: {'Custom URL' if custom_cover else 'Default'}\n<i>Gunakan /cancelbili untuk batal</i>")
        video_path, err = await asyncio.to_thread(_download_from_url, url, user_id)
        if not video_path: return await status_msg.edit(f"❌ <b>Download gagal/Batal:</b> {err}")

        results = []
        try:
            for i, acc in enumerate(target_accounts):
                if _CANCEL_BILI.get(user_id):
                    results.append("🛑 Upload dibatalkan secara manual.")
                    break

                await status_msg.edit(f"🚀 <b>Mengupload ke BiliTV ({i+1}/{len(target_accounts)})...</b>\n👤 Akun: <b>{acc['name']}</b>\n⏳ Harap bersabar...\n<i>Gunakan /cancelbili untuk batal</i>")
                ok, detail = await _do_upload_playwright(video_path, acc, title, tags_str, desc, custom_cover, user_id)
                results.append(f"{'✅' if ok else '❌'} <b>{acc['name']}</b>: {detail}")
                if i < len(target_accounts) - 1: await asyncio.sleep(3)
        finally:
            if video_path and os.path.exists(video_path): os.unlink(video_path)

        await status_msg.edit(f"📊 <b>Hasil Upload Bilibili</b>\n📝 {title}\n\n" + "\n".join(results))

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
