import os
import asyncio
import mimetypes
import shutil
import logging
from mega import Mega
from pyrogram import Client
from pyrogram.types import Message
from config import MAX_FILE_SIZE_MB, DOWNLOAD_DIR
from handlers.proxy_rotator import ProxyRotator

logger = logging.getLogger(__name__)


class MegaHandler:
    def __init__(self, app: Client, user_states: dict):
        self.app = app
        self.user_states = user_states
        self.BATCH_SIZE_MB = 200
        self.QUOTA_LIMIT_MB = 1800
        self.total_downloaded_mb = 0
        self.proxy_rotator = ProxyRotator()
        self.current_mega = None

    async def _login_mega(self, use_proxy=False):
        """Login to Mega, optionally through a proxy"""
        if use_proxy:
            proxy = await self.proxy_rotator.get_working_proxy(max_attempts=15)
            if proxy:
                logger.info(f"Using proxy: {proxy}")
                # mega.py uses requests internally, set env proxy
                os.environ["HTTP_PROXY"] = proxy
                os.environ["HTTPS_PROXY"] = proxy
                os.environ["http_proxy"] = proxy
                os.environ["https_proxy"] = proxy
            else:
                logger.warning("No working proxy found, trying direct")
                self._clear_proxy()
        else:
            self._clear_proxy()
        
        mega = Mega()
        self.current_mega = mega.login()
        return self.current_mega

    def _clear_proxy(self):
        """Remove proxy env vars"""
        for key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
            os.environ.pop(key, None)

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
            "🔗 Send me the **Mega link** to download.\n\n"
            "📦 Batch mode: 200MB per batch\n"
            "🔄 Auto proxy rotation on quota hit\n"
            "✅ Already downloaded files will be uploaded first"
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
            await message.reply_text("❌ That doesn't look like a valid Mega link. Try again.")
            return
        
        self.user_states[user_id]["mega_link"] = mega_link
        self.user_states[user_id]["state"] = "mega_confirm"
        
        channel_title = self.user_states[user_id].get("channel_title", "Unknown")
        await message.reply_text(
            f"📋 **Confirm Upload:**\n\n"
            f"🔗 Link: `{mega_link[:50]}...`\n"
            f"📢 Channel: **{channel_title}**\n"
            f"📏 Max file size: {MAX_FILE_SIZE_MB}MB\n"
            f"📦 Batch: {self.BATCH_SIZE_MB}MB\n"
            f"🔄 Auto proxy rotation on quota\n\n"
            f"Send **yes** to start or **no** to cancel."
        )

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
        
        status_msg = await message.reply_text("⏳ Loading proxies & connecting to Mega...")
        
        try:
            # Pre-load proxies
            proxy_count = await self.proxy_rotator.fetch_proxies()
            await status_msg.edit_text(
                f"🔄 Loaded **{proxy_count}** proxies\n"
                f"📥 Connecting to Mega..."
            )
            
            await self._download_and_upload(message, status_msg, mega_link, channel_id)
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: `{str(e)}`")
        finally:
            self._clear_proxy()
            self.user_states[user_id]["state"] = "idle"

    async def _switch_to_proxy(self, status_msg: Message, retry_count: int = 0) -> bool:
        """Switch to a new proxy when quota hits"""
        max_retries = 20
        
        while retry_count < max_retries:
            retry_count += 1
            proxy = await self.proxy_rotator.get_working_proxy(max_attempts=5)
            
            if proxy is None:
                # Refresh proxy list
                await status_msg.edit_text("🔄 Refreshing proxy list...")
                count = await self.proxy_rotator.fetch_proxies()
                if count == 0:
                    await status_msg.edit_text(
                        "⚠️ No proxies available. Waiting 5 min and retrying..."
                    )
                    await asyncio.sleep(300)
                    await self.proxy_rotator.fetch_proxies()
                    continue
                proxy = await self.proxy_rotator.get_working_proxy(max_attempts=5)
                if proxy is None:
                    continue
            
            await status_msg.edit_text(
                f"🔄 Switching to proxy #{retry_count}...\n"
                f"🌐 {proxy[:30]}..."
            )
            
            try:
                os.environ["HTTP_PROXY"] = proxy
                os.environ["HTTPS_PROXY"] = proxy
                os.environ["http_proxy"] = proxy
                os.environ["https_proxy"] = proxy
                
                mega = Mega()
                self.current_mega = mega.login()
                self.total_downloaded_mb = 0
                
                await status_msg.edit_text(
                    f"✅ Connected via new proxy!\n"
                    f"🔄 Remaining proxies: {self.proxy_rotator.get_proxy_count()}"
                )
                return True
                
            except Exception as e:
                self.proxy_rotator.mark_failed(proxy)
                self._clear_proxy()
                continue
        
        await status_msg.edit_text("❌ All proxies exhausted. Try again later.")
        return False

    async def _download_and_upload(self, message: Message, status_msg: Message, mega_link: str, channel_id: int):
        """Download from Mega in batches with auto proxy rotation"""
        user_id = message.from_user.id
        download_path = os.path.join(DOWNLOAD_DIR, str(user_id))
        os.makedirs(download_path, exist_ok=True)
        
        try:
            # Initial login (direct, no proxy)
            m = await self._login_mega(use_proxy=False)
            
            await status_msg.edit_text("📥 Fetching file list from Mega...")
            
            # Get file list
            file_list = []
            if "/folder/" in mega_link or "#F!" in mega_link:
                try:
                    folder = m.find(mega_link)
                    files = m.get_files()
                    for file_id, file_info in files.items():
                        if file_info['t'] == 0:
                            file_list.append((file_id, file_info))
                except Exception:
                    import_result = m.import_public_url(mega_link)
                    files = m.get_files()
                    for file_id, file_info in files.items():
                        if file_info['t'] == 0:
                            file_list.append((file_id, file_info))
            else:
                file_info = m.get_public_url_info(mega_link)
                file_list = [(None, {"a": {"n": file_info["name"]}, "s": file_info["size"]})]
            
            total_files = len(file_list)
            uploaded = 0
            skipped = 0
            failed = 0
            batch_size_mb = 0
            batch_num = 1
            proxy_switches = 0
            
            await status_msg.edit_text(
                f"📁 Found **{total_files}** files\n"
                f"🚀 Starting Batch #{batch_num}"
            )
            
            for idx, (file_id, file_info) in enumerate(file_list, 1):
                try:
                    if isinstance(file_info, dict):
                        file_name = file_info.get('a', {}).get('n', f'file_{idx}')
                        file_size_bytes = file_info.get('s', 0)
                    else:
                        file_name = getattr(file_info, 'name', f'file_{idx}')
                        file_size_bytes = getattr(file_info, 'size', 0)
                    
                    file_size_mb = file_size_bytes / (1024 * 1024)
                    
                    # Skip files > MAX_FILE_SIZE_MB
                    if file_size_mb > MAX_FILE_SIZE_MB:
                        skipped += 1
                        continue
                    
                    # Check quota limit — switch proxy before hitting it
                    if self.total_downloaded_mb + file_size_mb > self.QUOTA_LIMIT_MB:
                        # Upload what we have first
                        batch_uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                        uploaded += batch_uploaded
                        
                        # Switch proxy
                        await status_msg.edit_text("🔄 Quota approaching, switching proxy...")
                        success = await self._switch_to_proxy(status_msg)
                        if not success:
                            break
                        
                        proxy_switches += 1
                        batch_num += 1
                        batch_size_mb = 0
                    
                    # Batch full — upload current batch first
                    if batch_size_mb + file_size_mb > self.BATCH_SIZE_MB and batch_size_mb > 0:
                        await status_msg.edit_text(f"📤 Uploading Batch #{batch_num} ({batch_size_mb:.0f}MB)...")
                        batch_uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                        uploaded += batch_uploaded
                        batch_num += 1
                        batch_size_mb = 0
                    
                    # Download file
                    await status_msg.edit_text(
                        f"📥 [{idx}/{total_files}] `{file_name}` ({file_size_mb:.1f}MB)\n"
                        f"📦 Batch #{batch_num} ({batch_size_mb:.0f}/{self.BATCH_SIZE_MB}MB)\n"
                        f"🔄 Proxy switches: {proxy_switches}"
                    )
                    
                    download_success = await self._try_download(
                        file_id, file_info, mega_link, download_path,
                        file_size_mb, status_msg
                    )
                    
                    if download_success:
                        batch_size_mb += file_size_mb
                        self.total_downloaded_mb += file_size_mb
                    else:
                        failed += 1
                    
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    failed += 1
                    logger.error(f"Error processing file {idx}: {e}")
                    await asyncio.sleep(1)
            
            # Upload remaining files
            if os.path.exists(download_path) and os.listdir(download_path):
                await status_msg.edit_text(f"📤 Uploading final batch...")
                batch_uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                uploaded += batch_uploaded
            
            await status_msg.edit_text(
                f"✅ **All Done!**\n\n"
                f"📤 Uploaded: {uploaded}\n"
                f"⏭️ Skipped (>{MAX_FILE_SIZE_MB}MB): {skipped}\n"
                f"❌ Failed: {failed}\n"
                f"📁 Total: {total_files}\n"
                f"📦 Batches: {batch_num}\n"
                f"🔄 Proxy switches: {proxy_switches}\n"
                f"🌐 Total downloaded: {self.total_downloaded_mb:.0f}MB"
            )
        
        finally:
            if os.path.exists(download_path):
                shutil.rmtree(download_path, ignore_errors=True)
            self._clear_proxy()

    async def _try_download(self, file_id, file_info, mega_link, download_path, file_size_mb, status_msg) -> bool:
        """Try downloading a file, auto-switch proxy on quota error"""
        max_proxy_retries = 5
        
        for attempt in range(max_proxy_retries):
            try:
                m = self.current_mega
                if file_id:
                    downloaded = m.download((file_id, file_info), download_path)
                else:
                    downloaded = m.download_url(mega_link, download_path)
                
                return downloaded is not None
                
            except Exception as e:
                error_msg = str(e).lower()
                
                if any(word in error_msg for word in ["quota", "bandwidth", "limit", "509"]):
                    # Upload what we have before switching
                    await status_msg.edit_text(
                        f"⚠️ Quota hit! Uploading downloaded files first..."
                    )
                    
                    # Switch to new proxy
                    success = await self._switch_to_proxy(status_msg)
                    if not success:
                        return False
                    continue
                else:
                    logger.error(f"Download error (non-quota): {e}")
                    return False
        
        return False

    async def _upload_pending_files(self, download_path: str, channel_id: int, status_msg: Message) -> int:
        """Upload all files in download folder to Telegram, then delete them"""
        uploaded = 0
        
        if not os.path.exists(download_path):
            return 0
        
        files = []
        for root, dirs, filenames in os.walk(download_path):
            for fname in filenames:
                files.append(os.path.join(root, fname))
        
        for file_path in files:
            try:
                file_name = os.path.basename(file_path)
                await self._upload_to_channel(channel_id, file_path, file_name)
                uploaded += 1
                
                if os.path.exists(file_path):
                    os.remove(file_path)
                
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Upload error for {file_path}: {e}")
                if os.path.exists(file_path):
                    os.remove(file_path)
                await asyncio.sleep(1)
        
        return uploaded

    async def _upload_to_channel(self, channel_id: int, file_path: str, file_name: str):
        """Upload file to Telegram channel as photo/video (not document)"""
        mime_type, _ = mimetypes.guess_type(file_path)
        
        if mime_type and mime_type.startswith("image/"):
            await self.app.send_photo(
                chat_id=channel_id,
                photo=file_path,
                caption=file_name
            )
        elif mime_type and mime_type.startswith("video/"):
            await self.app.send_video(
                chat_id=channel_id,
                video=file_path,
                caption=file_name,
                supports_streaming=True
            )
        else:
            await self.app.send_document(
                chat_id=channel_id,
                document=file_path,
                caption=file_name
            )
