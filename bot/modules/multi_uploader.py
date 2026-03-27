"""
============================================================
  bot/modules/bilibili_login.py
  Tambahkan ke folder: bot/modules/

  FITUR:
  - /bililogin      → simpan cookies bilibili.tv (upload file JSON)
  - /biliaccounts   → lihat semua akun yang sudah login
  - /bililogout     → logout / hapus akun
  - /biliupload     → upload video via direct URL ke bilibili.tv
  - /biliset        → atur default tags, judul, deskripsi
  - /bilicancel     → batalkan sesi login yang aktif

  FORMAT /biliupload:
  /biliupload <url> | <judul> | <deskripsi>
  /biliupload <url> | <judul>
  /biliupload <url>

  Contoh:
  /biliupload https://example.com/video.mp4 | Hidori Eps 12 | Episode 12 HD sub indo

  INSTALL DEPENDENCY:
  pip install httpx
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

# ─────────────────────────────────────────────
#  KONFIGURASI
# ─────────────────────────────────────────────
BILI_DIR = Path("/app/bili_accounts")        # Folder simpan semua cookies
BILI_DIR.mkdir(parents=True, exist_ok=True)

BILI_SETTINGS_FILE = BILI_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "tags": ["indonesia", "gaming", "anime"],
    "title_prefix": "",
    "desc": "",
    "tid": 171,          # 171=游戏 160=生活 17=单机游戏
    "copyright": 1,      # 1=original 2=repost
    "line": "bda2",      # bda2=百度加速2 | tx=腾讯 | bldsa | alia=阿里
    "limit": 3,
    "account_mode": "round_robin",  # atau "all"
}

# State sementara untuk proses login (per user)
_login_sessions: dict = {}  # user_id -> {"proc": ..., "tmp": ..., "msg": ...}


# ─────────────────────────────────────────────
#  HELPER: SETTINGS
# ─────────────────────────────────────────────

def load_settings() -> dict:
    if BILI_SETTINGS_FILE.exists():
        try:
            return json.loads(BILI_SETTINGS_FILE.read_text())
        except Exception:
            pass
    return DEFAULT_SETTINGS.copy()


def save_settings(s: dict):
    BILI_SETTINGS_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False))


# ─────────────────────────────────────────────
#  HELPER: AKUN
# ─────────────────────────────────────────────

def list_accounts() -> list[dict]:
    """Return list of {name, path, valid}"""
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
    existing = list_accounts()
    n = len(existing) + 1
    return f"akun{n}"


def get_cookie_path(name: str) -> Path:
    return BILI_DIR / f"cookies_{name}.json"



# ─────────────────────────────────────────────
#  PERINTAH: /bililogin — upload file cookies JSON
# ─────────────────────────────────────────────
#
#  Cara pakai:
#  1. Ketik /bililogin  → bot minta kirim file cookies
#  2. Ketik /bililogin nama_akun  → bot minta kirim file, disimpan dengan nama itu
#  3. Kirim file .json sebagai reply ke instruksi bot  → tersimpan otomatis
#
#  Format cookies yang didukung:
#  - Format biliup: {"SESSDATA": "...", "bili_jct": "...", ...}
#  - Format Netscape/array: [{"name": "SESSDATA", "value": "..."}, ...]

@new_task
async def bili_login_cmd(client, message: Message):
    user_id = message.from_user.id

    args = message.text.split(maxsplit=1)
    akun_name = args[1].strip() if len(args) > 1 else next_account_name()

    # Simpan state menunggu file dari user ini
    _login_sessions[user_id] = {
        "akun_name": akun_name,
        "waiting_file": True,
    }

    await message.reply(
        f"📂 <b>Upload Cookies Bilibili</b>\n\n"
        f"👤 Akun yang akan didaftarkan: <b>{akun_name}</b>\n\n"
        "Kirim file <code>cookies.json</code> kamu sekarang.\n\n"
        "<b>Cara dapatkan cookies:</b>\n"
        "• Gunakan ekstensi browser <b>EditThisCookie</b> atau <b>Cookie-Editor</b>\n"
        "• Login ke bilibili.com → Export cookies sebagai JSON\n"
        "• Kirim file .json tersebut ke sini\n\n"
        "<b>Format yang didukung:</b>\n"
        "• Format biliup: <code>{\"SESSDATA\": \"...\", \"bili_jct\": \"...\"}</code>\n"
        "• Format array: <code>[{\"name\": \"SESSDATA\", \"value\": \"...\"}]</code>\n\n"
        "⏱ Menunggu file selama 5 menit...\n"
        "Ketik /bilicancel untuk batalkan."
    )

    # Auto-expire session setelah 5 menit
    asyncio.get_event_loop().create_task(
        _expire_login_session(user_id, client, message.chat.id)
    )


async def _expire_login_session(user_id: int, client, chat_id: int):
    await asyncio.sleep(300)  # 5 menit
    session = _login_sessions.get(user_id)
    if session and session.get("waiting_file"):
        _login_sessions.pop(user_id, None)
        await client.send_message(
            chat_id,
            "⏱ Sesi login timeout. Ketik /bililogin untuk coba lagi."
        )


@new_task
async def bili_receive_cookie_file(client, message: Message):
    """Handler untuk menerima file cookies JSON yang dikirim user."""
    user_id = message.from_user.id
    session = _login_sessions.get(user_id)

    if not session or not session.get("waiting_file"):
        return  # Bukan dalam sesi login, abaikan

    doc = message.document
    if not doc:
        await message.reply("❌ Kirim sebagai file/dokumen, bukan foto atau teks.")
        return

    filename = doc.file_name or ""
    if not filename.endswith(".json"):
        await message.reply(
            "❌ File harus berformat <code>.json</code>\n"
            "Pastikan kamu export cookies sebagai JSON."
        )
        return

    akun_name = session["akun_name"]
    cookie_path = get_cookie_path(akun_name)

    status_msg = await message.reply(f"⏳ Memproses cookies untuk akun <b>{akun_name}</b>...")

    # Download file dari Telegram
    tmp_path = f"/tmp/cookies_upload_{user_id}.json"
    await client.download_media(message, file_name=tmp_path)

    # Validasi dan konversi format cookies
    try:
        raw = Path(tmp_path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        await status_msg.edit(f"❌ File JSON tidak valid:\n<code>{e}</code>")
        _login_sessions.pop(user_id, None)
        os.unlink(tmp_path)
        return

    # Konversi format array (Netscape/browser export) → dict
    if isinstance(data, list):
        converted = {}
        for item in data:
            if isinstance(item, dict) and "name" in item and "value" in item:
                converted[item["name"]] = item["value"]
        if not converted:
            await status_msg.edit(
                "❌ Format cookies array tidak dikenali.\n"
                "Pastikan setiap item punya field <code>name</code> dan <code>value</code>."
            )
            _login_sessions.pop(user_id, None)
            os.unlink(tmp_path)
            return
        data = converted

    # Validasi minimal ada SESSDATA
    required_keys = {"SESSDATA", "bili_jct"}
    found_keys = set(data.keys())
    missing = required_keys - found_keys
    if missing:
        await status_msg.edit(
            f"⚠️ Cookies kurang lengkap, key yang tidak ditemukan: <code>{', '.join(missing)}</code>\n\n"
            "Tetap disimpan, tapi mungkin tidak bisa upload.\n"
            "Pastikan kamu login dulu di bilibili.com sebelum export."
        )

    # Simpan cookies
    cookie_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.unlink(tmp_path)
    _login_sessions.pop(user_id, None)

    accounts = list_accounts()
    await status_msg.edit(
        f"✅ <b>Cookies berhasil disimpan!</b>\n\n"
        f"👤 Akun: <b>{akun_name}</b>\n"
        f"💾 Path: <code>{cookie_path}</code>\n"
        f"🔑 Keys: <code>{', '.join(sorted(data.keys()))}</code>\n"
        f"📊 Total akun terdaftar: <b>{len(accounts)}</b>\n\n"
        "Gunakan /biliaccounts untuk lihat semua akun.\n"
        "Gunakan /biliupload untuk mulai upload video."
    )


# ─────────────────────────────────────────────
#  PERINTAH: /biliaccounts
# ─────────────────────────────────────────────

@new_task
async def bili_accounts_cmd(client, message: Message):
    accounts = list_accounts()
    settings = load_settings()

    if not accounts:
        await message.reply(
            "📭 Belum ada akun Bilibili yang login.\n"
            "Gunakan /bililogin untuk menambah akun."
        )
        return

    lines = ["<b>📋 Daftar Akun Bilibili</b>\n"]
    for i, acc in enumerate(accounts, 1):
        status = "✅" if acc["valid"] else "⚠️"
        lines.append(f"{i}. {status} <b>{acc['name']}</b>")

    tags_str = ", ".join(f"#{t}" for t in settings.get("tags", []))
    lines.append(f"\n🏷 Tags: {tags_str or '-'}")
    lines.append(f"🔄 Mode: {settings.get('account_mode', 'round_robin')}")
    lines.append(f"📁 Kategori tid: {settings.get('tid', 171)}")
    lines.append("\nGunakan /bililogout [nama] untuk hapus akun.")

    buttons = [
        [InlineKeyboardButton("➕ Login Akun Baru", callback_data="bili_new_login")],
        [InlineKeyboardButton("⚙️ Pengaturan", callback_data="bili_settings")],
    ]
    await message.reply(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ─────────────────────────────────────────────
#  PERINTAH: /bililogout [nama]
# ─────────────────────────────────────────────

@new_task
async def bili_logout_cmd(client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        accounts = list_accounts()
        if not accounts:
            await message.reply("Belum ada akun yang login.")
            return
        names = "\n".join(f"  • {a['name']}" for a in accounts)
        await message.reply(
            f"Gunakan: <code>/bililogout [nama_akun]</code>\n\nAkun tersedia:\n{names}"
        )
        return

    name = args[1].strip()
    cookie_path = get_cookie_path(name)
    if not cookie_path.exists():
        await message.reply(f"❌ Akun <b>{name}</b> tidak ditemukan.")
        return

    cookie_path.unlink()
    await message.reply(f"✅ Akun <b>{name}</b> berhasil dihapus.")


# ─────────────────────────────────────────────
#  PERINTAH: /biliset — atur tags, tid, mode
# ─────────────────────────────────────────────

@new_task
async def bili_set_cmd(client, message: Message):
    """
    /biliset tags gaming,anime,indonesia
    /biliset tid 171
    /biliset mode all
    /biliset mode round_robin
    /biliset prefix [AUTO]
    /biliset desc Deskripsi default video
    """
    args = message.text.split(maxsplit=2)
    settings = load_settings()

    if len(args) < 3:
        tags_str = ",".join(settings.get("tags", []))
        await message.reply(
            "<b>⚙️ Pengaturan Bilibili Uploader</b>\n\n"
            f"🏷 Tags: <code>{tags_str}</code>\n"
            f"📁 tid: <code>{settings.get('tid', 171)}</code>\n"
            f"🔄 Mode: <code>{settings.get('account_mode', 'round_robin')}</code>\n"
            f"🔤 Prefix: <code>{settings.get('title_prefix', '')}</code>\n"
            f"📝 Desc: <code>{settings.get('desc', '')}</code>\n\n"
            "<b>Cara pakai:</b>\n"
            "/biliset tags gaming,anime,indonesia\n"
            "/biliset tid 171\n"
            "/biliset mode all\n"
            "/biliset mode round_robin\n"
            "/biliset prefix [AUTO]\n"
            "/biliset desc Deskripsi default\n\n"
            "<b>Kategori tid umum:</b>\n"
            "171=游戏  160=生活  17=单机游戏\n"
            "21=日常  138=搞笑  189=其他"
        )
        return

    key = args[1].lower()
    val = args[2].strip()

    if key == "tags":
        tags = [t.strip().lstrip("#") for t in val.split(",") if t.strip()]
        settings["tags"] = tags
        save_settings(settings)
        tags_display = " ".join(f"#{t}" for t in tags)
        await message.reply(f"✅ Tags diset: {tags_display}")

    elif key == "tid":
        try:
            settings["tid"] = int(val)
            save_settings(settings)
            await message.reply(f"✅ Kategori tid diset ke: {val}")
        except ValueError:
            await message.reply("❌ tid harus angka, contoh: /biliset tid 171")

    elif key == "mode":
        if val not in ("all", "round_robin"):
            await message.reply("❌ Mode harus: all atau round_robin")
            return
        settings["account_mode"] = val
        save_settings(settings)
        await message.reply(f"✅ Mode akun diset ke: {val}")

    elif key == "prefix":
        settings["title_prefix"] = val
        save_settings(settings)
        await message.reply(f"✅ Prefix judul diset ke: {val}")

    elif key == "desc":
        settings["desc"] = val
        save_settings(settings)
        await message.reply(f"✅ Deskripsi default diset ke: {val}")

    else:
        await message.reply(f"❌ Key tidak dikenal: {key}")


# ─────────────────────────────────────────────
#  HELPER: DOWNLOAD VIDEO DARI URL
# ─────────────────────────────────────────────

def _download_from_url(url: str) -> tuple[str | None, str | None]:
    """
    Download video dari direct URL ke /tmp.
    Return (path, error_message).
    """
    try:
        # Ambil nama file dari URL
        filename = url.rstrip("/").split("/")[-1].split("?")[0]
        if not filename or "." not in filename:
            filename = f"video_{int(time.time())}.mp4"

        tmp_path = f"/tmp/{filename}"

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BiliBot/1.0)"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)  # 1 MB per chunk
                    if not chunk:
                        break
                    f.write(chunk)

        return tmp_path, None

    except Exception as e:
        return None, str(e)


# ─────────────────────────────────────────────
#  HELPER: EKSEKUSI UPLOAD
# ─────────────────────────────────────────────

def _parse_cookies(cookie_path: str) -> dict:
    """Baca cookies dari file JSON, return dict {name: value}."""
    raw = json.loads(Path(cookie_path).read_text())
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
    return {}


def _cookies_to_header(cookies: dict) -> str:
    """Convert dict cookies ke string header Cookie."""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def _do_upload_playwright(
    video_path: str,
    account: dict,
    title: str,
    tags: str,
    desc: str,
) -> tuple[bool, str]:
    """
    Upload video ke bilibili.tv via pure API (tanpa browser).

    Flow:
      1. GET  api.bilibili.tv/preupload  → dapat upload_url, upload_id, upos_auth
      2. POST {upload_url}?uploads       → inisiasi multipart upload
      3. PUT  chunks                     → upload file per bagian
      4. POST {upload_url}?output=json   → complete multipart upload
      5. POST api.bilibili.tv/x/vu/web/add/v3  → submit metadata
    """
    import httpx

    # ── Load cookies ──────────────────────────────────────────
    try:
        cookies = _parse_cookies(account["path"])
    except Exception as e:
        return False, f"Gagal baca cookies: {e}"

    if not cookies.get("SESSDATA"):
        return False, "Cookies tidak valid — tidak ada SESSDATA. Login ulang dengan /bililogin"

    cookie_header = _cookies_to_header(cookies)
    filename = Path(video_path).name
    filesize = Path(video_path).stat().st_size

    base_headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
        "Origin": "https://studio.bilibili.tv",
        "Referer": "https://studio.bilibili.tv/",
        "Cookie": cookie_header,
    }

    CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB per chunk

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:

        # ── Step 1: Preupload ─────────────────────────────────
        LOGGER.info(f"[bili.tv] Step 1: preupload — {filename} ({filesize} bytes)")
        try:
            r = await client.get(
                "https://api.bilibili.tv/preupload",
                params={
                    "name": filename,
                    "size": filesize,
                    "r": "upos",
                    "profile": "iup/bup",
                    "ssl": "0",
                    "version": "2.10.0",
                    "build": "2100000",
                    "biz": "UGC",
                },
                headers=base_headers,
            )
            pre = r.json()
            LOGGER.info(f"[bili.tv] preupload response: {pre}")
        except Exception as e:
            return False, f"Preupload gagal: {e}"

        if pre.get("OK") != 1:
            return False, f"Preupload error: {pre}"

        upload_url   = pre["url"]           # e.g. //upos-cs-upcdntxa.bilivideo.com/iupever/nXXX.mp4
        upos_auth    = pre["upos_uri"]      # X-Upos-Auth header value
        biz_id       = str(pre.get("biz_id", ""))

        # Normalisasi URL
        if upload_url.startswith("//"):
            upload_url = "https:" + upload_url

        upos_headers = {
            **base_headers,
            "X-Upos-Auth": upos_auth,
            "Content-Type": "application/octet-stream",
        }

        # ── Step 2: Inisiasi multipart upload ─────────────────
        LOGGER.info(f"[bili.tv] Step 2: init multipart upload")
        try:
            r = await client.post(
                upload_url,
                params={"uploads": "", "output": "json"},
                headers={**upos_headers, "Content-Type": "application/json"},
                content=b"",
            )
            init_data = r.json()
            LOGGER.info(f"[bili.tv] init response: {init_data}")
        except Exception as e:
            return False, f"Init multipart gagal: {e}"

        upload_id = init_data.get("upload_id") or init_data.get("uploadId")
        if not upload_id:
            return False, f"Tidak dapat upload_id: {init_data}"

        # ── Step 3: Upload chunks ─────────────────────────────
        total_chunks = (filesize + CHUNK_SIZE - 1) // CHUNK_SIZE
        LOGGER.info(f"[bili.tv] Step 3: upload {total_chunks} chunks")

        parts = []
        with open(video_path, "rb") as f:
            for chunk_idx in range(total_chunks):
                start = chunk_idx * CHUNK_SIZE
                end   = min(start + CHUNK_SIZE, filesize)
                size  = end - start

                f.seek(start)
                chunk_data = f.read(size)

                params = {
                    "partNumber": chunk_idx + 1,
                    "uploadId":   upload_id,
                    "chunk":      chunk_idx,
                    "chunks":     total_chunks,
                    "size":       size,
                    "start":      start,
                    "end":        end,
                    "total":      filesize,
                }

                for attempt in range(3):
                    try:
                        r = await client.put(
                            upload_url,
                            params=params,
                            content=chunk_data,
                            headers=upos_headers,
                            timeout=600,
                        )
                        if r.status_code in (200, 204):
                            LOGGER.info(f"[bili.tv] chunk {chunk_idx+1}/{total_chunks} OK")
                            break
                        LOGGER.warning(f"[bili.tv] chunk {chunk_idx+1} attempt {attempt+1} status {r.status_code}")
                    except Exception as e:
                        LOGGER.warning(f"[bili.tv] chunk {chunk_idx+1} attempt {attempt+1} error: {e}")
                        if attempt == 2:
                            return False, f"Gagal upload chunk {chunk_idx+1}: {e}"
                        await asyncio.sleep(3)

                parts.append({"partNumber": chunk_idx + 1, "eTag": "etag"})

        # ── Step 4: Notify uploading status ──────────────────
        LOGGER.info(f"[bili.tv] Step 4: notify uploading")
        try:
            await client.post(
                "https://api.bilibili.tv/intl/videoup/web2/uploading",
                json={"upload_id": upload_id, "filename": filename},
                headers={**base_headers, "Content-Type": "application/json;charset=UTF-8"},
                timeout=30,
            )
        except Exception as e:
            LOGGER.warning(f"[bili.tv] uploading notify gagal (non-fatal): {e}")

        # ── Step 5: Complete multipart upload ─────────────────
        LOGGER.info(f"[bili.tv] Step 5: complete upload")
        try:
            r = await client.post(
                upload_url,
                params={
                    "output":   "json",
                    "name":     filename,
                    "profile":  "iup/bup",
                    "uploadId": upload_id,
                    "biz_id":   biz_id,
                    "biz":      "UGC",
                },
                json={"parts": parts},
                headers={**upos_headers, "Content-Type": "application/json; charset=UTF-8"},
                timeout=60,
            )
            complete_data = r.json()
            LOGGER.info(f"[bili.tv] complete response: {complete_data}")
        except Exception as e:
            return False, f"Complete upload gagal: {e}"

        if complete_data.get("OK") != 1:
            return False, f"Complete upload error: {complete_data}"

        # ── Step 6: Submit metadata ───────────────────────────
        LOGGER.info(f"[bili.tv] Step 6: submit metadata — judul: {title}")
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]

        # filename key dari complete response, fallback ke nama file
        video_key = complete_data.get("key", "").lstrip("/") or filename

        submit_data = {
            "copyright": 1,
            "videos": [{
                "filename": video_key,
                "title":    title,
                "desc":     desc,
            }],
            "source":     "",
            "tid":        0,
            "cover":      "",
            "title":      title,
            "tag":        ",".join(tags_list),
            "desc":       desc,
            "dynamic":    "",
            "no_reprint": 0,
            "open_elec":  0,
        }

        try:
            r = await client.post(
                "https://api.bilibili.tv/intl/videoup/web2/add",
                params={"csrf": cookies.get("bili_jct", "")},
                json=submit_data,
                headers={
                    **base_headers,
                    "Content-Type": "application/json;charset=UTF-8",
                },
                timeout=60,
            )
            submit_resp = r.json()
            LOGGER.info(f"[bili.tv] submit response: {submit_resp}")
        except Exception as e:
            return False, f"Submit metadata gagal: {e}"

        code = submit_resp.get("code", -1)
        if code == 0:
            data = submit_resp.get("data", {})
            aid  = data.get("aid", "")
            return True, f"Upload berhasil! {'aid=' + str(aid) if aid else ''} Video sedang dalam review ✅"

        return False, f"Submit error code={code}: {submit_resp.get('message', str(submit_resp))}"


# ─────────────────────────────────────────────
#  PERINTAH: /biliupload — upload via direct URL
# ─────────────────────────────────────────────

@new_task
async def bili_upload_cmd(client, message: Message):
    """
    Format perintah:
      /biliupload <url>
      /biliupload <url> | <judul>
      /biliupload <url> | <judul> | <deskripsi>

    Contoh:
      /biliupload https://example.com/video.mp4
      /biliupload https://example.com/video.mp4 | Hidori Stream Eps 12
      /biliupload https://example.com/video.mp4 | Hidori Stream Eps 12 | Episode 12 HD sub indo

    Catatan:
      - Judul & deskripsi opsional, jika tidak diisi pakai default dari /biliset
      - Separator antar bagian menggunakan | (pipe)
    """
    accounts = list_accounts()
    if not accounts:
        await message.reply(
            "❌ Belum ada akun Bilibili!\n"
            "Login dulu dengan /bililogin"
        )
        return

    settings = load_settings()
    args = message.text.split(maxsplit=1)

    if len(args) < 2:
        await message.reply(
            "❌ <b>URL tidak boleh kosong.</b>\n\n"
            "<b>Format:</b>\n"
            "<code>/biliupload &lt;url&gt;</code>\n"
            "<code>/biliupload &lt;url&gt; | &lt;judul&gt;</code>\n"
            "<code>/biliupload &lt;url&gt; | &lt;judul&gt; | &lt;deskripsi&gt;</code>\n\n"
            "<b>Contoh:</b>\n"
            "<code>/biliupload https://example.com/vid.mp4 | Hidori Eps 12 | Episode 12 sub indo HD</code>"
        )
        return

    # Parse URL | judul | deskripsi
    raw = args[1].strip()
    parts = [p.strip() for p in raw.split("|")]

    url = parts[0]
    custom_title = parts[1] if len(parts) > 1 and parts[1] else None
    custom_desc  = parts[2] if len(parts) > 2 and parts[2] else None

    # Validasi URL
    if not url.startswith("http://") and not url.startswith("https://"):
        await message.reply(
            "❌ URL tidak valid.\n"
            "Harus diawali dengan <code>http://</code> atau <code>https://</code>"
        )
        return

    # Ambil nama file dari URL sebagai judul fallback
    url_filename = url.rstrip("/").split("/")[-1].split("?")[0]
    url_stem = url_filename.rsplit(".", 1)[0] if "." in url_filename else url_filename

    title = custom_title or url_stem
    if settings.get("title_prefix"):
        title = f"{settings['title_prefix']} {title}"

    desc      = custom_desc or settings.get("desc", "")
    tags_str  = ",".join(settings.get("tags", []))
    mode      = settings.get("account_mode", "round_robin")
    valid_accs = [a for a in accounts if a["valid"]]

    if not valid_accs:
        await message.reply(
            "❌ Semua cookie akun tidak valid.\n"
            "Login ulang dengan /bililogin"
        )
        return

    # Pilih akun
    if mode == "all":
        target_accounts = valid_accs
    else:
        # Round robin sederhana berdasarkan waktu
        idx = int(time.time()) % len(valid_accs)
        target_accounts = [valid_accs[idx]]

    tags_display = " ".join(f"#{t}" for t in settings.get("tags", []))
    url_short    = url[:80] + ("..." if len(url) > 80 else "")

    status_msg = await message.reply(
        f"⬇️ <b>Mendownload video...</b>\n\n"
        f"🔗 URL: <code>{url_short}</code>\n"
        f"📝 Judul: {title}\n"
        f"📄 Deskripsi: {desc[:80] or '(default)'}\n"
        f"🏷 Tags: {tags_display}\n"
        f"👥 Akun: {', '.join(a['name'] for a in target_accounts)}\n"
        f"📁 Kategori: tid={settings.get('tid', 171)}"
    )

    # Download dulu ke /tmp
    video_path, err = await asyncio.to_thread(_download_from_url, url)
    if not video_path:
        await status_msg.edit(
            f"❌ <b>Gagal download video</b>\n\n"
            f"URL: <code>{url_short}</code>\n"
            f"Error: <code>{err}</code>"
        )
        return

    file_size_mb = round(Path(video_path).stat().st_size / 1024 / 1024, 1)

    # Pakai try/finally supaya file SELALU dihapus apapun yang terjadi
    results = []
    try:
        for acc in target_accounts:
            await status_msg.edit(
                f"🚀 <b>Mengupload ke bilibili.tv via browser...</b>\n\n"
                f"📹 File: <code>{Path(video_path).name}</code> ({file_size_mb} MB)\n"
                f"📝 Judul: {title}\n"
                f"👥 Akun: <b>{acc['name']}</b>\n\n"
                "⏳ Proses ini bisa memakan waktu sesuai ukuran file...\n"
                "Jangan kirim perintah lain dulu."
            )
            ok, detail = await _do_upload_playwright(
                video_path, acc, title, tags_str, desc
            )
            emoji = "✅" if ok else "❌"
            results.append(f"{emoji} <b>{acc['name']}</b>: {detail}")
    finally:
        # SELALU hapus file temp, sukses atau gagal atau crash sekalipun
        try:
            if video_path and os.path.exists(video_path):
                os.unlink(video_path)
                LOGGER.info(f"[bili] File temp dihapus: {video_path}")
        except Exception as e:
            LOGGER.warning(f"[bili] Gagal hapus file temp: {e}")

    await status_msg.edit(
        f"📊 <b>Hasil Upload Bilibili TV</b>\n\n"
        f"📝 Judul: {title}\n"
        f"📄 Desc: {desc[:80] or '-'}\n"
        f"🏷 Tags: {tags_display}\n\n"
        + "\n".join(results)
    )


# ─────────────────────────────────────────────
#  CALLBACK QUERY
# ─────────────────────────────────────────────

@new_task
async def bili_callback(client, query: CallbackQuery):
    data = query.data

    if data == "bili_new_login":
        await query.answer()
        await query.message.reply(
            "Gunakan perintah:\n"
            "<code>/bililogin [nama_akun_opsional]</code>\n\n"
            "Contoh: <code>/bililogin akun5</code>"
        )

    elif data == "bili_settings":
        await query.answer()
        settings = load_settings()
        tags_str = ",".join(settings.get("tags", []))
        await query.message.reply(
            f"<b>⚙️ Pengaturan saat ini:</b>\n\n"
            f"🏷 Tags: <code>{tags_str}</code>\n"
            f"📁 tid: <code>{settings.get('tid')}</code>\n"
            f"🔄 Mode: <code>{settings.get('account_mode')}</code>\n"
            f"🔤 Prefix: <code>{settings.get('title_prefix') or '-'}</code>\n"
            f"📝 Desc: <code>{settings.get('desc') or '-'}</code>\n\n"
            "Ubah dengan /biliset"
        )


# ─────────────────────────────────────────────
#  PERINTAH: /bilicancel — batalkan sesi login
# ─────────────────────────────────────────────

@new_task
async def bili_cancel_login_cmd(client, message: Message):
    user_id = message.from_user.id
    if user_id in _login_sessions:
        _login_sessions.pop(user_id)
        await message.reply("✅ Sesi login dibatalkan.")
    else:
        await message.reply("Tidak ada sesi login yang aktif.")


# ─────────────────────────────────────────────
#  REGISTRASI HANDLER — otomatis saat module di-import
# ─────────────────────────────────────────────

TgClient.bot.add_handler(
    MessageHandler(
        bili_login_cmd,
        filters=filters.command("bililogin") & CustomFilters.authorized,
    )
)
TgClient.bot.add_handler(
    MessageHandler(
        bili_cancel_login_cmd,
        filters=filters.command("bilicancel") & CustomFilters.authorized,
    )
)
TgClient.bot.add_handler(
    MessageHandler(
        bili_accounts_cmd,
        filters=filters.command("biliaccounts") & CustomFilters.authorized,
    )
)
TgClient.bot.add_handler(
    MessageHandler(
        bili_logout_cmd,
        filters=filters.command("bililogout") & CustomFilters.authorized,
    )
)
TgClient.bot.add_handler(
    MessageHandler(
        bili_set_cmd,
        filters=filters.command("biliset") & CustomFilters.authorized,
    )
)
TgClient.bot.add_handler(
    MessageHandler(
        bili_upload_cmd,
        filters=filters.command("biliupload") & CustomFilters.authorized,
    )
)
TgClient.bot.add_handler(
    CallbackQueryHandler(
        bili_callback,
        filters=filters.regex(r"^bili_"),
    )
)
# Handler penerima file cookies — tangkap dokumen .json dari user yang sedang dalam sesi login
TgClient.bot.add_handler(
    MessageHandler(
        bili_receive_cookie_file,
        filters=filters.document & CustomFilters.authorized,
    )
)

LOGGER.info("bilibili_login: ✅ semua handler berhasil didaftarkan")
