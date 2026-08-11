from aiofiles.os import path as aiopath, listdir, remove
from asyncio import sleep, gather
from html import escape
from requests import utils as rutils

from ... import (
    intervals,
    task_dict,
    task_dict_lock,
    LOGGER,
    non_queued_up,
    non_queued_dl,
    queued_up,
    queued_dl,
    queue_dict_lock,
    same_directory_lock,
    DOWNLOAD_DIR,
)
from ...core.config_manager import Config
from ...core.torrent_manager import TorrentManager
from ..common import TaskConfig
from ..ext_utils.bot_utils import sync_to_async
from ..ext_utils.db_handler import database
from ..ext_utils.files_utils import (
    get_path_size,
    clean_download,
    clean_target,
    join_files,
    create_recursive_symlink,
    remove_excluded_files,
    remove_non_included_files,
    move_and_merge,
)
from ..ext_utils.links_utils import is_gdrive_id
from ..ext_utils.status_utils import get_readable_file_size
from ..ext_utils.task_manager import start_from_queued, check_running_tasks
from ..mirror_leech_utils.gdrive_utils.upload import GoogleDriveUpload
from ..mirror_leech_utils.rclone_utils.transfer import RcloneTransferHelper
from ..mirror_leech_utils.status_utils.gdrive_status import GoogleDriveStatus
from ..mirror_leech_utils.status_utils.queue_status import QueueStatus
from ..mirror_leech_utils.status_utils.rclone_status import RcloneStatus
from ..mirror_leech_utils.status_utils.telegram_status import TelegramStatus
from ..mirror_leech_utils.telegram_uploader import TelegramUploader
from ..telegram_helper.button_build import ButtonMaker
from ..telegram_helper.message_utils import (
    send_message,
    delete_status,
    update_status_message,
)


class TaskListener(TaskConfig):
    def __init__(self):
        super().__init__()

    async def clean(self):
        try:
            if st := intervals["status"]:
                for intvl in list(st.values()):
                    intvl.cancel()
            intervals["status"].clear()
            await gather(TorrentManager.aria2.purgeDownloadResult(), delete_status())
        except:
            pass

    def clear(self):
        self.subname = ""
        self.subsize = 0
        self.files_to_proceed = []
        self.proceed_count = 0
        self.progress = True

    async def remove_from_same_dir(self):
        async with task_dict_lock:
            if (
                self.folder_name
                and self.same_dir
                and self.mid in self.same_dir[self.folder_name]["tasks"]
            ):
                self.same_dir[self.folder_name]["tasks"].remove(self.mid)
                self.same_dir[self.folder_name]["total"] -= 1

    async def on_download_start(self):
        if (
            self.is_super_chat
            and Config.INCOMPLETE_TASK_NOTIFIER
            and Config.DATABASE_URL
        ):
            await database.add_incomplete_task(
                self.message.chat.id, self.message.link, self.tag
            )

    async def on_download_complete(self):
        await sleep(2)
        if self.is_cancelled:
            return
        multi_links = False
        if (
            self.folder_name
            and self.same_dir
            and self.mid in self.same_dir[self.folder_name]["tasks"]
        ):
            async with same_directory_lock:
                while True:
                    async with task_dict_lock:
                        if self.mid not in self.same_dir[self.folder_name]["tasks"]:
                            return
                        if (
                            self.same_dir[self.folder_name]["total"] <= 1
                            or len(self.same_dir[self.folder_name]["tasks"]) > 1
                        ):
                            if self.same_dir[self.folder_name]["total"] > 1:
                                self.same_dir[self.folder_name]["tasks"].remove(
                                    self.mid
                                )
                                self.same_dir[self.folder_name]["total"] -= 1
                                spath = f"{self.dir}{self.folder_name}"
                                des_id = list(self.same_dir[self.folder_name]["tasks"])[
                                    0
                                ]
                                des_path = f"{DOWNLOAD_DIR}{des_id}{self.folder_name}"
                                LOGGER.info(f"Moving files from {self.mid} to {des_id}")
                                await move_and_merge(spath, des_path, self.mid)
                                multi_links = True
                            break
                    await sleep(1)
        async with task_dict_lock:
            if self.is_cancelled:
                return
            if self.mid not in task_dict:
                return
            download = task_dict[self.mid]
            self.name = download.name()
            gid = download.gid()
        LOGGER.info(f"Download completed: {self.name}")

        if not (self.is_torrent or self.is_qbit):
            self.seed = False

        if multi_links:
            self.seed = False
            await self.on_upload_error(
                f"{self.name} Downloaded!\n\nWaiting for other tasks to finish..."
            )
            return
        elif self.same_dir:
            self.seed = False

        if self.folder_name:
            self.name = self.folder_name.strip("/").split("/", 1)[0]

        if not await aiopath.exists(f"{self.dir}/{self.name}"):
            try:
                files = await listdir(self.dir)
                self.name = files[-1]
                if self.name == "yt-dlp-thumb":
                    self.name = files[0]
            except Exception as e:
                await self.on_upload_error(str(e))
                return

        dl_path = f"{self.dir}/{self.name}"
        self.size = await get_path_size(dl_path)
        self.is_file = await aiopath.isfile(dl_path)

        if self.seed:
            up_dir = self.up_dir = f"{self.dir}10000"
            up_path = f"{self.up_dir}/{self.name}"
            await create_recursive_symlink(self.dir, self.up_dir)
            LOGGER.info(f"Shortcut created: {dl_path} -> {up_path}")
        else:
            up_dir = self.dir
            up_path = dl_path

        if not self.included_extensions:
            await remove_excluded_files(
                self.up_dir or self.dir, self.excluded_extensions
            )
        else:
            await remove_non_included_files(
                self.up_dir or self.dir, self.included_extensions
            )

        if not await aiopath.exists(up_path):
            e = "No files to upload. In case you have filled EXCLUDED/INCLUDED EXTENSIONS, then check if all files have those extensions or not."
            await self.on_upload_error(str(e))
            return

        if not Config.QUEUE_ALL:
            async with queue_dict_lock:
                if self.mid in non_queued_dl:
                    non_queued_dl.remove(self.mid)
            await start_from_queued()

        if self.join and not self.is_file:
            await join_files(up_path)

        if self.extract and not self.is_nzb:
            up_path = await self.proceed_extract(up_path, gid)
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]
            self.size = await get_path_size(up_dir)
            self.clear()
            if not self.included_extensions:
                await remove_excluded_files(up_dir, self.excluded_extensions)
            else:
                await remove_non_included_files(up_dir, self.included_extensions)

        if self.ffmpeg_cmds:
            up_path = await self.proceed_ffmpeg(
                up_path,
                gid,
            )
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]
            self.size = await get_path_size(up_dir)
            self.clear()

        if self.name_sub:
            LOGGER.info(f"Start Name Substitution {up_path}")
            up_path = await self.substitute(up_path)
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]

        if self.screen_shots:
            up_path = await self.generate_screenshots(up_path)
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]
            self.size = await get_path_size(up_dir)

        if self.convert_audio or self.convert_video:
            up_path = await self.convert_media(
                up_path,
                gid,
            )
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]
            self.size = await get_path_size(up_dir)
            self.clear()

        if self.sample_video:
            up_path = await self.generate_sample_video(up_path, gid)
            if self.is_cancelled:
                return
            self.is_file = await aiopath.isfile(up_path)
            self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]
            self.size = await get_path_size(up_dir)
            self.clear()

        if self.compress:
            up_path = await self.proceed_compress(
                up_path,
                gid,
            )
            self.is_file = await aiopath.isfile(up_path)
            if self.is_cancelled:
                return
            self.clear()

        self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]
        self.size = await get_path_size(up_dir)

        if self.is_leech and not self.compress:
            await self.proceed_split(up_path, gid)
            if self.is_cancelled:
                return
            self.clear()

        self.subproc = None

        add_to_queue, event = await check_running_tasks(self, "up")
        await start_from_queued()
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Upload: {self.name}")
            async with task_dict_lock:
                task_dict[self.mid] = QueueStatus(self, gid, "Up")
            await event.wait()
            if self.is_cancelled:
                return
            LOGGER.info(f"Start from Queued/Upload: {self.name}")

        self.size = await get_path_size(up_dir)

        if self.is_leech:
            LOGGER.info(f"Leech Name: {self.name}")
            tg = TelegramUploader(self, up_dir)
            async with task_dict_lock:
                task_dict[self.mid] = TelegramStatus(self, tg, gid, "up")
            await gather(
                update_status_message(self.message.chat.id),
                tg.upload(),
            )
            del tg
        # ==========================================
        # SUNTIKAN MESIN CUSTOM UPLOAD DENGAN LIVE PROGRESS BAR
        # ==========================================
        elif getattr(self, 'up_dest', '') in ["gofile", "buzzheavier"]:
            LOGGER.info(f"Custom Upload Name: {self.name} ke {self.up_dest.upper()}")
            import requests
            import os
            import time
            import asyncio
            import urllib.parse
            
            target_path = os.path.join(self.dir, self.name)
            
            if os.path.isfile(target_path):
                ui_msg = await send_message(self.message, f"⬆️ Memulai Upload ke **{self.up_dest.upper()}**...\n📁 `{self.name}`\nMenyiapkan koneksi...")
                
                # Variabel berbagi (Shared State) antara Thread HTTP dan Async Task
                shared_prog = {"uploaded": 0, "total": os.path.getsize(target_path), "done": False, "link": None, "error": None}
                
                def sync_upload():
                    try:
                        filename = os.path.basename(target_path)
                        
                        if self.up_dest == "gofile":
                            server = requests.get("https://api.gofile.io/servers").json()['data']['servers'][0]['name']
                            url = f"https://{server}.gofile.io/contents/uploadfile"
                            boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
                            
                            # Generator pemecah file (Chunker) + Perekam Progress
                            def file_gen():
                                head = (f"--{boundary}\r\n"
                                        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                                        f"Content-Type: application/octet-stream\r\n\r\n").encode('utf-8')
                                yield head
                                shared_prog['uploaded'] += len(head)
                                
                                with open(target_path, 'rb') as f:
                                    while chunk := f.read(262144): # Potong per 256KB
                                        yield chunk
                                        shared_prog['uploaded'] += len(chunk)
                                        
                                tail = f"\r\n--{boundary}--\r\n".encode('utf-8')
                                yield tail
                                shared_prog['uploaded'] += len(tail)
                                
                            headers = {'Content-Type': f'multipart/form-data; boundary={boundary}'}
                            res = requests.post(url, data=file_gen(), headers=headers).json()
                            
                            if res.get('status') == 'ok':
                                shared_prog['link'] = res['data']['downloadPage']
                            else:
                                shared_prog['error'] = f"Error Server GoFile: {str(res)}"
                        
                        elif self.up_dest == "buzzheavier":
                            fname = urllib.parse.quote(filename, safe="")
                            url = f"https://w.buzzheavier.com/{fname}"
                            
                            def file_gen_bh():
                                with open(target_path, 'rb') as f:
                                    while chunk := f.read(262144):
                                        yield chunk
                                        shared_prog['uploaded'] += len(chunk)
                                        
                            r = requests.put(url, data=file_gen_bh())
                            res = r.json()
                            url_res = res.get("data", {}).get("url")
                            if not url_res and res.get("data", {}).get("id"):
                                url_res = f"https://buzzheavier.com/{res['data']['id']}"
                                
                            if url_res:
                                shared_prog['link'] = url_res
                            else:
                                shared_prog['error'] = f"Error Server Buzzheavier: {str(res)}"
                                
                    except Exception as e:
                        shared_prog['error'] = str(e)
                    finally:
                        shared_prog['done'] = True

                # Tugas Latar Belakang untuk Update Pesan di Telegram
                async def progress_updater():
                    last_text = ""
                    start_time = time.time()
                    while not shared_prog['done']:
                        await asyncio.sleep(3) # Update setiap 3 detik
                        if shared_prog['done']: break
                        
                        uploaded = shared_prog['uploaded']
                        total = shared_prog['total']
                        percent = (uploaded / total) * 100 if total > 0 else 0
                        elapsed = time.time() - start_time
                        speed = uploaded / elapsed if elapsed > 0 else 0
                        
                        # Bikin visual kotak loading [██████▒▒▒]
                        bar_length = 15
                        filled = int(bar_length * percent / 100)
                        bar = "█" * filled + "▒" * (bar_length - filled)
                        
                        text = (f"⬆️ **Mengupload ke {self.up_dest.upper()}**\n"
                                f"📁 `{self.name}`\n"
                                f"[{bar}] {percent:.1f}%\n"
                                f"**Processed:** {get_readable_file_size(uploaded)} / {get_readable_file_size(total)}\n"
                                f"**Speed:** {get_readable_file_size(speed)}/s")
                                
                        if text != last_text:
                            try:
                                await ui_msg.edit_text(text)
                                last_text = text
                            except Exception:
                                pass # Abaikan error kalau kena flood limit

                # Jalankan Upload dan Progress secara bersamaan
                updater_task = asyncio.create_task(progress_updater())
                await asyncio.to_thread(sync_upload)
                await updater_task
                
                # Kirim Hasil Akhir
                if shared_prog['link']:
                    pesan = f"✅ **Berhasil Upload ke {self.up_dest.upper()}**\n\n📁 `{self.name}`\n🔗 **Link:** {shared_prog['link']}"
                else:
                    pesan = f"❌ **Gagal Upload ke {self.up_dest.upper()}**\n\n📁 `{self.name}`\nError: {shared_prog['error']}"
                    
                try:
                    await ui_msg.edit_text(pesan)
                except:
                    await send_message(self.message, pesan)
            else:
                await send_message(self.message, f"❌ **Gagal:** `{self.name}` adalah Folder. Script Custom ini hanya untuk Single File.")
                
            self.clean()
            return
        # ==========================================
        elif is_gdrive_id(self.up_dest) or self.up_dest == "gd":
            LOGGER.info(f"Gdrive Upload Name: {self.name}")
            drive = GoogleDriveUpload(self, up_path)
            async with task_dict_lock:
                task_dict[self.mid] = GoogleDriveStatus(self, drive, gid, "up")
            await gather(
                update_status_message(self.message.chat.id),
                sync_to_async(drive.upload),
            )
            del drive
        else:
            LOGGER.info(f"Rclone Upload Name: {self.name}")
            RCTransfer = RcloneTransferHelper(self)
            async with task_dict_lock:
                task_dict[self.mid] = RcloneStatus(self, RCTransfer, gid, "up")
            await gather(
                update_status_message(self.message.chat.id),
                RCTransfer.upload(up_path),
            )
            del RCTransfer

        if self.seed:
            await clean_target(self.up_dir)
            async with queue_dict_lock:
                if self.mid in non_queued_up:
                    non_queued_up.remove(self.mid)
            await start_from_queued()
            return
            
        await clean_download(self.dir)
        async with task_dict_lock:
            if self.mid in task_dict:
                del task_dict[self.mid]
            count = len(task_dict)
        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        async with queue_dict_lock:
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)

        await start_from_queued()

    async def on_upload_complete(
        self, link, files, folders, mime_type, rclone_path="", dir_id=""
    ):
        if (
            self.is_super_chat
            and Config.INCOMPLETE_TASK_NOTIFIER
            and Config.DATABASE_URL
        ):
            await database.rm_complete_task(self.message.link)
        msg = f"<b>Name: </b><code>{escape(self.name)}</code>\n\n<b>Size: </b>{get_readable_file_size(self.size)}"
        LOGGER.info(f"Task Done: {self.name}")
        if self.is_leech:
            msg += f"\n<b>Total Files: </b>{folders}"
            if mime_type != 0:
                msg += f"\n<b>Corrupted Files: </b>{mime_type}"
            msg += f"\n<b>cc: </b>{self.tag}\n\n"
            if not files:
                await send_message(self.message, msg)
            else:
                fmsg = ""
                for index, (link, name) in enumerate(files.items(), start=1):
                    fmsg += f"{index}. <a href='{link}'>{name}</a>\n"
                    if len(fmsg.encode() + msg.encode()) > 4000:
                        await send_message(self.message, msg + fmsg)
                        await sleep(1)
                        fmsg = ""
                if fmsg != "":
                    await send_message(self.message, msg + fmsg)
        else:
            msg += f"\n\n<b>Type: </b>{mime_type}"
            if mime_type == "Folder":
                msg += f"\n<b>SubFolders: </b>{folders}"
                msg += f"\n<b>Files: </b>{files}"
            if (
                link
                or rclone_path
                and Config.RCLONE_SERVE_URL
                and not self.private_link
            ):
                buttons = ButtonMaker()
                if link:
                    buttons.url_button("☁️ Cloud Link", link)
                else:
                    msg += f"\n\nPath: <code>{rclone_path}</code>"
                if rclone_path and Config.RCLONE_SERVE_URL and not self.private_link:
                    remote, rpath = rclone_path.split(":", 1)
                    url_path = rutils.quote(f"{rpath}")
                    share_url = f"{Config.RCLONE_SERVE_URL}/{remote}/{url_path}"
                    if mime_type == "Folder":
                        share_url += "/"
                    buttons.url_button("🔗 Rclone Link", share_url)
                if not rclone_path and dir_id:
                    INDEX_URL = ""
                    if self.private_link:
                        INDEX_URL = self.user_dict.get("INDEX_URL", "") or ""
                    elif Config.INDEX_URL:
                        INDEX_URL = Config.INDEX_URL
                    if INDEX_URL:
                        share_url = f"{INDEX_URL}findpath?id={dir_id}"
                        buttons.url_button("⚡ Index Link", share_url)
                        if mime_type.startswith(("image", "video", "audio")):
                            share_urls = f"{INDEX_URL}findpath?id={dir_id}&view=true"
                            buttons.url_button("🌐 View Link", share_urls)
                button = buttons.build_menu(2)
            else:
                msg += f"\n\nPath: <code>{rclone_path}</code>"
                button = None
            msg += f"\n\n<b>cc: </b>{self.tag}"
            await send_message(self.message, msg, button)

    async def on_download_error(self, error, button=None):
        async with task_dict_lock:
            if self.mid in task_dict:
                del task_dict[self.mid]
            count = len(task_dict)
        await self.remove_from_same_dir()
        msg = f"{self.tag} Download: {escape(str(error))}"
        await send_message(self.message, msg, button)
        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        if (
            self.is_super_chat
            and Config.INCOMPLETE_TASK_NOTIFIER
            and Config.DATABASE_URL
        ):
            await database.rm_complete_task(self.message.link)

        async with queue_dict_lock:
            if self.mid in queued_dl:
                queued_dl[self.mid].set()
                del queued_dl[self.mid]
            if self.mid in queued_up:
                queued_up[self.mid].set()
                del queued_up[self.mid]
            if self.mid in non_queued_dl:
                non_queued_dl.remove(self.mid)
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)

        await start_from_queued()
        await sleep(3)
        await clean_download(self.dir)
        if self.up_dir:
            await clean_download(self.up_dir)
        if self.thumb and await aiopath.exists(self.thumb):
            await remove(self.thumb)

    async def on_upload_error(self, error):
        async with task_dict_lock:
            if self.mid in task_dict:
                del task_dict[self.mid]
            count = len(task_dict)
        await send_message(self.message, f"{self.tag} {escape(str(error))}")
        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        if (
            self.is_super_chat
            and Config.INCOMPLETE_TASK_NOTIFIER
            and Config.DATABASE_URL
        ):
            await database.rm_complete_task(self.message.link)

        async with queue_dict_lock:
            if self.mid in queued_dl:
                queued_dl[self.mid].set()
                del queued_dl[self.mid]
            if self.mid in queued_up:
                queued_up[self.mid].set()
                del queued_up[self.mid]
            if self.mid in non_queued_dl:
                non_queued_dl.remove(self.mid)
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)

        await start_from_queued()
        await sleep(3)
        await clean_download(self.dir)
        if self.up_dir:
            await clean_download(self.up_dir)
        if self.thumb and await aiopath.exists(self.thumb):
            await remove(self.thumb)
