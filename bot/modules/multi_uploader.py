import os
import requests
import urllib.parse
from pyrogram import filters, Client

# --- PENYIMPANAN API KEY SEMENTARA ---
USER_API_KEYS = {
    'gofile': '', 'pixeldrain': '', 'transferit': '', 
    'filemirage': '', 'buzzheavier': '', 'player4me': '', 'akirabox': ''
}

# ==========================================
# 1. FUNGSI UPLOAD API
# ==========================================

def upload_gofile(file_path, api_key):
    try:
        server = requests.get('https://api.gofile.io/servers').json()['data']['servers'][0]['name']
        with open(file_path, 'rb') as f:
            res = requests.post(f"https://{server}.gofile.io/contents/uploadfile", files={'file': f}, data={'token': api_key})
            return res.json()['data']['downloadPage'] if res.status_code == 200 else "❌ Gagal Gofile"
    except Exception as e: return f"Error: {e}"

def upload_pixeldrain(file_path, api_key):
    try:
        with open(file_path, 'rb') as f:
            res = requests.post("https://pixeldrain.com/api/file", files={'file': f}, auth=('', api_key))
            return f"https://pixeldrain.com/u/{res.json().get('id')}" if res.status_code in [200, 201] else "❌ Gagal Pixeldrain"
    except Exception as e: return f"Error: {e}"

def upload_buzzheavier(file_path, api_key=None):
    try:
        filename = urllib.parse.quote(os.path.basename(file_path), safe="")
        with open(file_path, 'rb') as f:
            res = requests.put(f"https://w.buzzheavier.com/{filename}", data=f)
            data = res.json().get('data', {})
            return data.get('url') or f"https://buzzheavier.com/f/{data.get('id')}" if res.status_code == 200 else "❌ Gagal Buzzheavier"
    except Exception as e: return f"Error: {e}"

def upload_generic_host(file_path, upload_url, api_key):
    try:
        with open(file_path, 'rb') as f:
            res = requests.post(upload_url, files={'file': f}, data={'api_key': api_key, 'key': api_key})
            if res.status_code == 200:
                return str(res.json()) 
            return f"❌ HTTP Error: {res.status_code}"
    except Exception as e: return f"Error: {e}"

# ==========================================
# 2. HANDLER COMMAND (SET KEY & MIRROR UPLOAD)
# ==========================================

host_list = ['gofile', 'pixeldrain', 'transferit', 'filemirage', 'buzzheavier', 'player4me', 'akirabox']

# COMMAND UNTUK SET API KEY (/setgofile, /setplayer4me, dll)
@Client.on_message(filters.command([f"set{host}" for host in host_list]))
async def set_api_key_cmd(client, message):
    host_name = message.command[0].replace('set', '')
    if len(message.command) > 1:
        USER_API_KEYS[host_name] = message.command[1]
        await message.reply(f"✅ API Key untuk **{host_name}** berhasil disimpan!")
    else:
        await message.reply(f"Gunakan format: `/set{host_name} [API_KEY]`")

# COMMAND UNTUK MIRROR (/gofile [LINK], /player4me [LINK], dll)
@Client.on_message(filters.command(host_list))
async def mirror_file_cmd(client, message):
    host_name = message.command[0]
    
    if len(message.command) < 2:
        return await message.reply(f"Kirim Link file yang ingin di-mirror: `/{host_name} https://link-video.com/file.mp4`")
    
    input_data = message.command[1]
    file_path = input_data
    msg = await message.reply(f"🔍 Memproses permintaan untuk {host_name}...")

    # CEK APAKAH USER MENGIRIM LINK ATAU PATH LOKAL
    if input_data.startswith("http://") or input_data.startswith("https://"):
        await msg.edit_text("⬇️ Sedang mengunduh file dari link ke server...")
        
        # Buat folder sementara jika belum ada
        if not os.path.exists("downloads"):
            os.makedirs("downloads")
            
        # Ambil nama file dari URL
        filename = input_data.split("/")[-1].split("?")[0]
        if not filename:
            filename = "downloaded_file.bin"
            
        file_path = os.path.join("downloads", filename)
        
        # Proses Download
        try:
            with requests.get(input_data, stream=True) as r:
                r.raise_for_status()
                with open(file_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
        except Exception as e:
            return await msg.edit_text(f"❌ Gagal mengunduh link: {e}")
            
        await msg.edit_text(f"⬆️ File berhasil diunduh. Sekarang mulai mengunggah ke {host_name}...")
    else:
        if not os.path.exists(file_path):
            return await msg.edit_text("❌ Link tidak valid atau file lokal tidak ditemukan.")
        await msg.edit_text(f"⬆️ Mengunggah file lokal ke {host_name}...")

    # PROSES UPLOAD API
    api_key = USER_API_KEYS.get(host_name)

    if host_name == 'gofile': link = upload_gofile(file_path, api_key)
    elif host_name == 'pixeldrain': link = upload_pixeldrain(file_path, api_key)
    elif host_name == 'buzzheavier': link = upload_buzzheavier(file_path, api_key)
    else:
        endpoints = {
            'player4me': "https://player4me.com/api/upload",
            'akirabox': "https://akirabox.com/api/upload",
            'filemirage': "https://filemirage.com/api/upload",
            'transferit': "https://transfer.it/api/upload"
        }
        link = upload_generic_host(file_path, endpoints.get(host_name), api_key)

    await msg.edit_text(f"**✅ Berhasil Mirror ke {host_name.capitalize()}**\nLink Hasil: `{link}`")
    
    # Bersihkan file hasil download agar server tidak penuh
    if input_data.startswith("http://") or input_data.startswith("https://"):
        if os.path.exists(file_path):
            os.remove(file_path)
