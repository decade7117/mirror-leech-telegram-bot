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
  pip install playwright
  playwright install chromium
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

async def _do_upload_playwright(
    video_path: str,
    account: dict,
    title: str,
    tags: str,
    desc: str,
) -> tuple[bool, str]:
    """
    Upload video ke studio.bilibili.tv menggunakan Playwright.
    Inject cookies dari file JSON → buka studio → upload via file input.
    """
    try:
        from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    except ImportError:
        return False, "Playwright belum terinstall. Jalankan: pip install playwright && playwright install chromium"

    cookie_path = account["path"]
    try:
        raw_cookies = json.loads(Path(cookie_path).read_text())
    except Exception as e:
        return False, f"Gagal baca cookies: {e}"

    # Konversi cookies ke format Playwright
    # Support format dict (biliup) dan array (browser export)
    pw_cookies = []
    if isinstance(raw_cookies, dict):
        for name, value in raw_cookies.items():
            pw_cookies.append({
                "name": name,
                "value": str(value),
                "domain": ".bilibili.tv",
                "path": "/",
            })
    elif isinstance(raw_cookies, list):
        for c in raw_cookies:
            if "name" in c and "value" in c:
                # Paksa domain ke .bilibili.tv
                pw_cookies.append({
                    "name": c["name"],
                    "value": str(c["value"]),
                    "domain": ".bilibili.tv",
                    "path": c.get("path", "/"),
                })

    tags_list = [t.strip() for t in tags.split(",") if t.strip()]

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )
            await context.add_cookies(pw_cookies)
            page = await context.new_page()

            LOGGER.info("[bili.tv] Membuka studio.bilibili.tv...")
            await page.goto("https://studio.bilibili.tv/upload", wait_until="networkidle", timeout=60000)

            # Cek apakah sudah login
            current_url = page.url
            LOGGER.info(f"[bili.tv] URL setelah goto: {current_url}")
            if "login" in current_url or "passport" in current_url:
                await browser.close()
                return False, "Cookies tidak valid / sudah expired. Login ulang dengan /bililogin"

            # Tunggu file input muncul
            LOGGER.info("[bili.tv] Menunggu tombol upload...")
            try:
                await page.wait_for_selector("input[type='file']", timeout=30000)
            except PWTimeout:
                # Coba screenshot untuk debug
                await page.screenshot(path="/tmp/bili_debug.png")
                await browser.close()
                return False, "Timeout menunggu halaman upload. Cek /tmp/bili_debug.png"

            # Upload file
            LOGGER.info(f"[bili.tv] Upload file: {video_path}")
            file_input = page.locator("input[type='file']").first
            await file_input.set_input_files(video_path)

            # Tunggu progress upload selesai — tunggu elemen judul muncul
            LOGGER.info("[bili.tv] Menunggu file selesai diupload ke server...")
            try:
                # Tunggu max 2 jam untuk file besar
                await page.wait_for_selector(
                    "input[placeholder*='标题'], input[placeholder*='title'], input[placeholder*='Title']",
                    timeout=7200000,
                )
            except PWTimeout:
                await browser.close()
                return False, "Timeout upload file (>2 jam)"

            # Isi judul
            LOGGER.info(f"[bili.tv] Mengisi judul: {title}")
            title_input = page.locator(
                "input[placeholder*='标题'], input[placeholder*='title'], input[placeholder*='Title']"
            ).first
            await title_input.click()
            await title_input.fill("")
            await title_input.type(title, delay=50)

            # Isi deskripsi jika ada
            if desc:
                LOGGER.info(f"[bili.tv] Mengisi deskripsi...")
                try:
                    desc_input = page.locator(
                        "textarea[placeholder*='简介'], textarea[placeholder*='desc'], textarea[placeholder*='Description']"
                    ).first
                    await desc_input.click()
                    await desc_input.fill(desc)
                except Exception:
                    LOGGER.warning("[bili.tv] Gagal isi deskripsi, lanjut...")

            # Isi tags satu per satu
            if tags_list:
                LOGGER.info(f"[bili.tv] Mengisi tags: {tags_list}")
                try:
                    tag_input = page.locator(
                        "input[placeholder*='标签'], input[placeholder*='tag'], input[placeholder*='Tag']"
                    ).first
                    for tag in tags_list[:10]:  # max 10 tags
                        await tag_input.click()
                        await tag_input.type(tag, delay=50)
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(0.5)
                except Exception:
                    LOGGER.warning("[bili.tv] Gagal isi tags, lanjut...")

            # Klik tombol Submit/Publish
            LOGGER.info("[bili.tv] Klik tombol submit...")
            submit_selectors = [
                "button:has-text('发布')",
                "button:has-text('提交')",
                "button:has-text('Submit')",
                "button:has-text('Publish')",
                "button[type='submit']",
                ".submit-btn",
                ".publish-btn",
            ]
            submitted = False
            for sel in submit_selectors:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible():
                        await btn.click()
                        submitted = True
                        LOGGER.info(f"[bili.tv] Klik submit dengan selector: {sel}")
                        break
                except Exception:
                    continue

            if not submitted:
                await page.screenshot(path="/tmp/bili_submit_debug.png")
                await browser.close()
                return False, "Tombol submit tidak ditemukan. Screenshot disimpan di /tmp/bili_submit_debug.png"

            # Tunggu konfirmasi sukses
            LOGGER.info("[bili.tv] Menunggu konfirmasi sukses...")
            try:
                await page.wait_for_selector(
                    "text=成功, text=success, text=Success, text=审核, .success, .upload-success",
                    timeout=30000,
                )
                await browser.close()
                return True, "Upload berhasil! Video sedang dalam review ✅"
            except PWTimeout:
                # Cek URL berubah sebagai tanda sukses
                await asyncio.sleep(3)
                new_url = page.url
                await browser.close()
                if new_url != "https://studio.bilibili.tv/upload":
                    return True, f"Upload kemungkinan berhasil (redirect ke {new_url}) ✅"
                return False, "Tidak ada konfirmasi sukses dari halaman. Cek studio.bilibili.tv secara manual."

    except Exception as e:
        LOGGER.error(f"[bili.tv] Error Playwright: {e}")
        return False, f"Error: {str(e)[:300]}"


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

    await status_msg.edit(
        f"🚀 <b>Mengupload ke Bilibili...</b>\n\n"
        f"📹 File: <code>{Path(video_path).name}</code> ({file_size_mb} MB)\n"
        f"📝 Judul: {title}\n"
        f"📄 Deskripsi: {desc[:80] or '-'}\n"
        f"🏷 Tags: {tags_display}\n"
        f"👥 Akun: {', '.join(a['name'] for a in target_accounts)}\n\n"
        "⏳ Sedang mengupload, mohon tunggu..."
    )

    results = []
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

    # Hapus file temp setelah semua akun selesai
    try:
        os.unlink(video_path)
    except Exception:
        pass

    await status_msg.edit(
        f"📊 <b>Hasil Upload Bilibili</b>\n\n"
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
