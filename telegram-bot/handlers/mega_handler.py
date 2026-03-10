import os
import asyncio
import mimetypes
import shutil
import subprocess
import logging
from pyrogram import Client
from pyrogram.types import Message
from config import MAX_FILE_SIZE_MB, DOWNLOAD_DIR
from handlers.proxy_rotator import ProxyRotator

logger = logging.getLogger(__name__)


def install_megatools():
    """Install megatools if not present"""
    try:
        result = subprocess.run(["megadl", "--version"], capture_output=True, timeout=10)
        if result.returncode == 0:
            return True
    except FileNotFoundError:
        pass
    
    try:
        subprocess.run(["sudo", "apt", "install", "-y", "megatools"], capture_output=True, timeout=120)
        return True
    except Exception as e:
        logger.error(f"Failed to install megatools: {e}")
        return False


class MegaHandler:
    def __init__(self, app: Client, user_states: dict):
        self.app = app
        self.user_states = user_states
        self.BATCH_SIZE_MB = 200
        self.proxy_rotator = ProxyRotator()
        self.total_downloaded_mb = 0

    async def set_channel(self, message: Message):
        user_id = message.from_user.id
        self.user_states[user_id] = {"state": "setchannel_waiting_id"}
        await message.reply_text(
            "📢 Send me the **channel ID** or **@username** where I should upload files.\n\n"
            "Make sure I'm added as an admin with permission to post."
        )

    async def start_mega(self, message: Message):
        user_id = message.from_user.id
        state = self.user_states.get(user_id, {})
        
        if "channel_id" not in state:
            await message.reply_text("⚠️ First set a channel using /setchannel")
            return
        
        self.user_states[user_id]["state"] = "mega_waiting_link"
        await message.reply_text(
            "🔗 Send me the **Mega link** (file or folder).\n\n"
            "📦 200MB batches — download, upload, delete, repeat\n"
            "🔄 Auto proxy rotation on quota\n"
            "📁 Folder links fully supported!"
        )

    async def handle_input(self, message: Message):
        user_id = message.from_user.id
        state = self.user_states.get(user_id, {})
        current_state = state.get("state", "")

        if current_state == "setchannel_waiting_id":
            await self._handle_set_channel(message)
        elif current_state == "mega_waiting_link":
            await self._handle_mega_link(message)
        elif current_state == "mega_confirm":
            await self._handle_mega_confirm(message)

    async def _handle_set_channel(self, message: Message):
        user_id = message.from_user.id
        channel_id = message.text.strip()
        
        try:
            chat = await self.app.get_chat(channel_id)
            member = await self.app.get_chat_member(chat.id, "me")
            
            if member.privileges and member.privileges.can_post_messages:
                self.user_states[user_id] = {
                    "state": "idle",
                    "channel_id": chat.id,
                    "channel_title": chat.title
                }
                await message.reply_text(
                    f"✅ Channel set: **{chat.title}**\n\n"
                    f"You can now use /mega to start downloading."
                )
            else:
                await message.reply_text(
                    "❌ I don't have permission to post in this channel.\n"
                    "Make me an admin with 'Post Messages' permission and try again."
                )
        except Exception as e:
            await message.reply_text(
                f"❌ Error: Could not access channel.\n"
                f"Make sure the ID/username is correct and I'm added as admin.\n\n"
                f"Error: `{str(e)}`"
            )

    async def _handle_mega_link(self, message: Message):
        user_id = message.from_user.id
        mega_link = message.text.strip()
        
        if "mega.nz" not in mega_link and "mega.co.nz" not in mega_link:
            await message.reply_text("❌ Invalid Mega link. Try again.")
            return
        
        self.user_states[user_id]["mega_link"] = mega_link
        self.user_states[user_id]["state"] = "mega_confirm"
        
        channel_title = self.user_states[user_id].get("channel_title", "Unknown")
        is_folder = "/folder/" in mega_link or "#F!" in mega_link
        
        # Try to get file list for folders
        file_info_text = ""
        if is_folder:
            status = await message.reply_text("📁 Fetching folder contents...")
            file_list = await self._get_folder_file_list(mega_link)
            if file_list:
                total_size = sum(f["size"] for f in file_list)
                total_size_mb = total_size / (1024 * 1024)
                eligible = [f for f in file_list if f["size"] / (1024*1024) <= MAX_FILE_SIZE_MB]
                file_info_text = (
                    f"\n📁 Files: **{len(file_list)}** total\n"
                    f"✅ Eligible (≤{MAX_FILE_SIZE_MB}MB): **{len(eligible)}**\n"
                    f"📊 Total size: **{total_size_mb:.1f}MB**"
                )
                self.user_states[user_id]["file_list"] = file_list
            await status.delete()
        
        await message.reply_text(
            f"📋 **Confirm Upload:**\n\n"
            f"🔗 Link: `{mega_link[:60]}...`\n"
            f"📢 Channel: **{channel_title}**\n"
            f"📦 Type: {'📁 Folder' if is_folder else '📄 File'}\n"
            f"📏 Max file: {MAX_FILE_SIZE_MB}MB"
            f"{file_info_text}\n\n"
            f"Send **yes** to start or **no** to cancel."
        )

    async def _get_folder_file_list(self, mega_link: str) -> list:
        """Use megadl --path /dev/null --print-names or megals to list files"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "megals", "-l", "--human", "-n", mega_link,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            
            if proc.returncode != 0:
                logger.error(f"megals error: {stderr.decode()}")
                return []
            
            files = []
            for line in stdout.decode().strip().split("\n"):
                if not line.strip():
                    continue
                # megals -l output format: FLAGS SIZE DATE PATH
                parts = line.split()
                if len(parts) >= 4 and parts[0] != "d":  # Skip directories
                    try:
                        # Parse size (could be like "150M", "2.3G", "500K", or bytes)
                        size_str = parts[1]
                        size_bytes = self._parse_size(size_str)
                        file_name = parts[-1].split("/")[-1]
                        file_path = parts[-1]
                        files.append({
                            "name": file_name,
                            "size": size_bytes,
                            "path": file_path
                        })
                    except (ValueError, IndexError):
                        continue
            
            return files
        except asyncio.TimeoutError:
            logger.error("megals timed out")
            return []
        except FileNotFoundError:
            logger.error("megatools not installed")
            return []
        except Exception as e:
            logger.error(f"megals error: {e}")
            return []

    def _parse_size(self, size_str: str) -> int:
        """Parse human-readable size to bytes"""
        size_str = size_str.strip()
        multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        
        for suffix, mult in multipliers.items():
            if size_str.upper().endswith(suffix):
                return int(float(size_str[:-1]) * mult)
        
        try:
            return int(size_str)
        except ValueError:
            return 0

    async def _handle_mega_confirm(self, message: Message):
        user_id = message.from_user.id
        text = message.text.strip().lower()
        
        if text not in ["yes", "y", "ha", "haan"]:
            self.user_states[user_id]["state"] = "idle"
            await message.reply_text("❌ Cancelled.")
            return
        
        state = self.user_states[user_id]
        mega_link = state["mega_link"]
        channel_id = state["channel_id"]
        
        self.user_states[user_id]["state"] = "mega_downloading"
        self.total_downloaded_mb = 0
        
        status_msg = await message.reply_text("⏳ Loading proxies...")
        
        try:
            proxy_count = await self.proxy_rotator.fetch_proxies()
            await status_msg.edit_text(f"🔄 {proxy_count} proxies loaded. Starting download...")
            
            is_folder = "/folder/" in mega_link or "#F!" in mega_link
            
            if is_folder:
                await self._download_folder_megatools(message, status_msg, mega_link, channel_id)
            else:
                await self._download_single_megatools(message, status_msg, mega_link, channel_id)
                
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: `{str(e)}`")
        finally:
            self.user_states[user_id]["state"] = "idle"

    async def _download_single_megatools(self, message, status_msg, mega_link, channel_id):
        """Download single file using megadl"""
        user_id = message.from_user.id
        download_path = os.path.join(DOWNLOAD_DIR, str(user_id))
        os.makedirs(download_path, exist_ok=True)
        
        try:
            await status_msg.edit_text("📥 Downloading file...")
            
            success = await self._megadl(mega_link, download_path, status_msg)
            
            if success:
                uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                await status_msg.edit_text(f"✅ **Done!** Uploaded {uploaded} file(s).")
            else:
                await status_msg.edit_text("❌ Download failed.")
        finally:
            if os.path.exists(download_path):
                shutil.rmtree(download_path, ignore_errors=True)

    async def _download_folder_megatools(self, message, status_msg, mega_link, channel_id):
        """Download folder using megadl in batches"""
        user_id = message.from_user.id
        download_path = os.path.join(DOWNLOAD_DIR, str(user_id))
        os.makedirs(download_path, exist_ok=True)
        
        uploaded_total = 0
        skipped = 0
        failed = 0
        batch_num = 1
        proxy_switches = 0
        
        try:
            # Get file list
            file_list = self.user_states.get(user_id, {}).get("file_list", [])
            
            if not file_list:
                # Try direct download of entire folder
                await status_msg.edit_text("📥 Downloading entire folder...")
                success = await self._megadl(mega_link, download_path, status_msg)
                if success:
                    uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                    await status_msg.edit_text(f"✅ **Done!** Uploaded {uploaded} file(s).")
                else:
                    await status_msg.edit_text("❌ Folder download failed.")
                return
            
            total_files = len(file_list)
            batch_size_mb = 0
            
            await status_msg.edit_text(
                f"📁 Processing **{total_files}** files in batches of {self.BATCH_SIZE_MB}MB\n"
                f"🚀 Batch #{batch_num}"
            )
            
            for idx, file_info in enumerate(file_list, 1):
                file_name = file_info["name"]
                file_size_mb = file_info["size"] / (1024 * 1024)
                
                # Skip oversized files
                if file_size_mb > MAX_FILE_SIZE_MB:
                    skipped += 1
                    continue
                
                # Batch full — upload current batch
                if batch_size_mb + file_size_mb > self.BATCH_SIZE_MB and batch_size_mb > 0:
                    await status_msg.edit_text(f"📤 Uploading Batch #{batch_num}...")
                    batch_uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                    uploaded_total += batch_uploaded
                    batch_num += 1
                    batch_size_mb = 0
                
                await status_msg.edit_text(
                    f"📥 [{idx}/{total_files}] `{file_name}` ({file_size_mb:.1f}MB)\n"
                    f"📦 Batch #{batch_num} ({batch_size_mb:.0f}/{self.BATCH_SIZE_MB}MB)\n"
                    f"📤 Uploaded so far: {uploaded_total}"
                )
                
                # Download this file
                # Construct individual file URL if possible, otherwise use folder link
                success = await self._megadl(mega_link, download_path, status_msg)
                
                if success:
                    batch_size_mb += file_size_mb
                    self.total_downloaded_mb += file_size_mb
                else:
                    # Quota hit — upload what we have, switch proxy, retry
                    if batch_size_mb > 0:
                        await status_msg.edit_text("📤 Uploading downloaded files before proxy switch...")
                        batch_uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                        uploaded_total += batch_uploaded
                        batch_size_mb = 0
                    
                    switched = await self._switch_proxy(status_msg)
                    if switched:
                        proxy_switches += 1
                        # Retry
                        success = await self._megadl(mega_link, download_path, status_msg)
                        if success:
                            batch_size_mb += file_size_mb
                        else:
                            failed += 1
                    else:
                        failed += 1
                        break
                
                await asyncio.sleep(0.5)
            
            # Upload remaining
            if os.path.exists(download_path) and os.listdir(download_path):
                batch_uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                uploaded_total += batch_uploaded
            
            await status_msg.edit_text(
                f"✅ **All Done!**\n\n"
                f"📤 Uploaded: {uploaded_total}\n"
                f"⏭️ Skipped (>{MAX_FILE_SIZE_MB}MB): {skipped}\n"
                f"❌ Failed: {failed}\n"
                f"📁 Total: {total_files}\n"
                f"📦 Batches: {batch_num}\n"
                f"🔄 Proxy switches: {proxy_switches}"
            )
        
        finally:
            if os.path.exists(download_path):
                shutil.rmtree(download_path, ignore_errors=True)

    async def _megadl(self, url: str, dest: str, status_msg: Message, proxy: str = None) -> bool:
        """Download from Mega using megadl CLI tool"""
        cmd = ["megadl", "--no-ask-password", "--path", dest, url]
        
        env = os.environ.copy()
        if proxy:
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env
            )
            
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)  # 10 min timeout
            
            if proc.returncode == 0:
                return True
            
            error = stderr.decode().lower()
            logger.error(f"megadl error: {stderr.decode()}")
            
            if any(w in error for w in ["quota", "bandwidth", "limit", "over quota"]):
                # Quota hit — try with proxy
                if not proxy:
                    await status_msg.edit_text("⚠️ Quota hit! Switching to proxy...")
                    working_proxy = await self.proxy_rotator.get_working_proxy(max_attempts=10)
                    if working_proxy:
                        return await self._megadl(url, dest, status_msg, proxy=working_proxy)
                else:
                    # Current proxy also hit quota, get new one
                    self.proxy_rotator.mark_failed(proxy)
                    new_proxy = await self.proxy_rotator.get_working_proxy(max_attempts=10)
                    if new_proxy:
                        return await self._megadl(url, dest, status_msg, proxy=new_proxy)
            
            return False
            
        except asyncio.TimeoutError:
            logger.error("megadl timed out after 10 min")
            return False
        except Exception as e:
            logger.error(f"megadl exception: {e}")
            return False

    async def _switch_proxy(self, status_msg: Message) -> bool:
        """Switch to a working proxy"""
        for retry in range(15):
            proxy = await self.proxy_rotator.get_working_proxy(max_attempts=5)
            if proxy:
                await status_msg.edit_text(f"🔄 Switched to proxy #{retry+1}")
                return True
            
            await status_msg.edit_text("🔄 Refreshing proxy list...")
            count = await self.proxy_rotator.fetch_proxies()
            if count == 0:
                await status_msg.edit_text("⏳ No proxies, waiting 2 min...")
                await asyncio.sleep(120)
        
        return False

    async def _upload_pending_files(self, download_path: str, channel_id: int, status_msg: Message) -> int:
        """Upload all files in folder to Telegram, then delete"""
        uploaded = 0
        if not os.path.exists(download_path):
            return 0
        
        files = []
        for root, dirs, filenames in os.walk(download_path):
            for fname in filenames:
                fpath = os.path.join(root, fname)
                fsize = os.path.getsize(fpath) / (1024 * 1024)
                if fsize <= MAX_FILE_SIZE_MB:
                    files.append(fpath)
                else:
                    os.remove(fpath)  # Remove oversized files
        
        for file_path in files:
            try:
                file_name = os.path.basename(file_path)
                file_size_mb = os.path.getsize(file_path) / (1024*1024)
                
                await status_msg.edit_text(
                    f"📤 Uploading `{file_name}` ({file_size_mb:.1f}MB)..."
                )
                
                await self._upload_to_channel(channel_id, file_path, file_name)
                uploaded += 1
                
                if os.path.exists(file_path):
                    os.remove(file_path)
                
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Upload error: {e}")
                if os.path.exists(file_path):
                    os.remove(file_path)
        
        return uploaded

    async def _upload_to_channel(self, channel_id: int, file_path: str, file_name: str):
        """Upload as photo/video (not document)"""
        mime_type, _ = mimetypes.guess_type(file_path)
        
        if mime_type and mime_type.startswith("image/"):
            await self.app.send_photo(
                chat_id=channel_id, photo=file_path, caption=file_name
            )
        elif mime_type and mime_type.startswith("video/"):
            await self.app.send_video(
                chat_id=channel_id, video=file_path,
                caption=file_name, supports_streaming=True
            )
        else:
            await self.app.send_document(
                chat_id=channel_id, document=file_path, caption=file_name
            )
