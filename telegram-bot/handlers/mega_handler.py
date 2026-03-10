import os
import asyncio
import mimetypes
import subprocess
import shutil
from pathlib import Path
from mega import Mega
from pyrogram import Client
from pyrogram.types import Message
from config import MAX_FILE_SIZE_MB, MAX_UPLOAD_SIZE_MB, DOWNLOAD_DIR


class MegaHandler:
    def __init__(self, app: Client, user_states: dict):
        self.app = app
        self.user_states = user_states
        self.mega = Mega()
        self.BATCH_SIZE_MB = 200  # Process in 200MB batches
        self.QUOTA_LIMIT_MB = 1800  # Reset before hitting 2GB (safety margin)
        self.total_downloaded_mb = 0  # Track total downloaded in session

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
            "I'll download in batches (200MB each), upload what's done,\n"
            "and auto-reset IP if quota hits."
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
            f"📦 Batch size: {self.BATCH_SIZE_MB}MB\n"
            f"🔄 Auto IP reset at: ~{self.QUOTA_LIMIT_MB}MB\n\n"
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
        
        status_msg = await message.reply_text("⏳ Starting download from Mega...")
        
        try:
            await self._download_and_upload(message, status_msg, mega_link, channel_id)
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: `{str(e)}`")
        finally:
            self.user_states[user_id]["state"] = "idle"

    async def _reset_ip(self, status_msg: Message):
        """Reset IP to bypass Mega transfer quota"""
        await status_msg.edit_text(
            "🔄 **Quota limit approaching!**\n"
            "Resetting IP address..."
        )
        
        # Method 1: Try restarting network interface
        try:
            subprocess.run(["sudo", "ip", "link", "set", "ens5", "down"], timeout=10)
            await asyncio.sleep(2)
            subprocess.run(["sudo", "ip", "link", "set", "ens5", "up"], timeout=10)
            await asyncio.sleep(5)
            await status_msg.edit_text("✅ Network interface restarted. Continuing...")
            self.total_downloaded_mb = 0
            return True
        except Exception:
            pass

        # Method 2: Try using a different DNS (sometimes helps)
        try:
            subprocess.run(
                ["sudo", "bash", "-c", "echo 'nameserver 8.8.4.4' > /etc/resolv.conf"],
                timeout=10
            )
            await asyncio.sleep(2)
            self.total_downloaded_mb = 0
            return True
        except Exception:
            pass

        # Method 3: Wait for quota reset (6 hours typically)
        await status_msg.edit_text(
            "⚠️ Could not reset IP automatically.\n"
            "Options:\n"
            "1. Wait ~6 hours for quota to reset\n"
            "2. Manually assign a new Elastic IP in AWS Console\n"
            "3. Stop & start the EC2 instance (gets new public IP)"
        )
        return False

    async def _download_and_upload(self, message: Message, status_msg: Message, mega_link: str, channel_id: int):
        """Download from Mega in batches and upload to Telegram channel"""
        user_id = message.from_user.id
        download_path = os.path.join(DOWNLOAD_DIR, str(user_id))
        os.makedirs(download_path, exist_ok=True)
        
        try:
            # Login anonymously to Mega
            m = self.mega.login()
            
            await status_msg.edit_text("📥 Fetching file list from Mega...")
            
            # Get file list
            file_list = []
            if "/folder/" in mega_link or "#F!" in mega_link:
                try:
                    folder = m.find(mega_link)
                    files = m.get_files()
                    for file_id, file_info in files.items():
                        if file_info['t'] == 0:  # Regular file
                            file_list.append((file_id, file_info))
                except Exception:
                    # Try alternative method
                    import_result = m.import_public_url(mega_link)
                    files = m.get_files()
                    for file_id, file_info in files.items():
                        if file_info['t'] == 0:
                            file_list.append((file_id, file_info))
            else:
                # Single file
                file_info = m.get_public_url_info(mega_link)
                file_list = [(None, {"a": {"n": file_info["name"]}, "s": file_info["size"]})]
            
            total_files = len(file_list)
            uploaded = 0
            skipped = 0
            failed = 0
            batch_size_mb = 0
            batch_num = 1
            
            await status_msg.edit_text(
                f"📁 Found **{total_files}** files.\n"
                f"📦 Processing in {self.BATCH_SIZE_MB}MB batches...\n"
                f"🚀 Starting Batch #{batch_num}"
            )
            
            for idx, (file_id, file_info) in enumerate(file_list, 1):
                try:
                    # Get file info
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
                        await status_msg.edit_text(
                            f"⏭️ Skipping `{file_name}` ({file_size_mb:.1f}MB > {MAX_FILE_SIZE_MB}MB)\n"
                            f"Progress: {idx}/{total_files} | Batch #{batch_num}"
                        )
                        continue
                    
                    # Check if we need IP reset before downloading
                    if self.total_downloaded_mb + file_size_mb > self.QUOTA_LIMIT_MB:
                        # First, upload anything already downloaded
                        await self._upload_pending_files(download_path, channel_id, status_msg)
                        
                        # Reset IP
                        success = await self._reset_ip(status_msg)
                        if not success:
                            await status_msg.edit_text(
                                f"⚠️ **Stopped at quota limit.**\n\n"
                                f"📤 Uploaded: {uploaded}\n"
                                f"⏭️ Skipped: {skipped}\n"
                                f"❌ Remaining: {total_files - idx + 1}\n\n"
                                f"Re-run /mega after IP reset to continue."
                            )
                            return
                        
                        # Re-login to Mega with new IP
                        m = self.mega.login()
                        batch_num += 1
                        batch_size_mb = 0
                    
                    # Check batch size - upload current batch if full
                    if batch_size_mb + file_size_mb > self.BATCH_SIZE_MB and batch_size_mb > 0:
                        await status_msg.edit_text(
                            f"📦 Batch #{batch_num} complete ({batch_size_mb:.0f}MB)\n"
                            f"📤 Uploading batch to channel..."
                        )
                        
                        # Upload all files in download folder
                        batch_uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                        uploaded += batch_uploaded
                        
                        batch_num += 1
                        batch_size_mb = 0
                        
                        await status_msg.edit_text(
                            f"🚀 Starting Batch #{batch_num}\n"
                            f"Total uploaded so far: {uploaded}/{total_files}"
                        )
                    
                    # Download file
                    await status_msg.edit_text(
                        f"📥 [{idx}/{total_files}] Downloading `{file_name}` ({file_size_mb:.1f}MB)\n"
                        f"📦 Batch #{batch_num} ({batch_size_mb:.0f}/{self.BATCH_SIZE_MB}MB)\n"
                        f"🌐 Total downloaded: {self.total_downloaded_mb:.0f}MB"
                    )
                    
                    try:
                        if file_id:
                            downloaded_file = m.download((file_id, file_info), download_path)
                        else:
                            downloaded_file = m.download_url(mega_link, download_path)
                        
                        if downloaded_file is None:
                            failed += 1
                            continue
                        
                        batch_size_mb += file_size_mb
                        self.total_downloaded_mb += file_size_mb
                        
                    except Exception as download_err:
                        error_msg = str(download_err).lower()
                        
                        # Check if it's a quota error
                        if "quota" in error_msg or "bandwidth" in error_msg or "limit" in error_msg:
                            await status_msg.edit_text(
                                f"⚠️ **Mega quota hit!**\n"
                                f"📤 Uploading what we have so far..."
                            )
                            
                            # Upload whatever is downloaded
                            batch_uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                            uploaded += batch_uploaded
                            
                            # Try IP reset
                            success = await self._reset_ip(status_msg)
                            if success:
                                m = self.mega.login()
                                batch_num += 1
                                batch_size_mb = 0
                                # Retry this file
                                try:
                                    if file_id:
                                        downloaded_file = m.download((file_id, file_info), download_path)
                                    else:
                                        downloaded_file = m.download_url(mega_link, download_path)
                                    batch_size_mb += file_size_mb
                                    self.total_downloaded_mb = file_size_mb  # Reset counter
                                except Exception:
                                    failed += 1
                                    continue
                            else:
                                await status_msg.edit_text(
                                    f"⛔ **Quota hit - could not reset IP.**\n\n"
                                    f"📤 Uploaded: {uploaded}\n"
                                    f"❌ Failed: {failed}\n"
                                    f"📁 Remaining: {total_files - idx}\n\n"
                                    f"Already downloaded files were uploaded ✅\n"
                                    f"Re-run /mega after getting new IP."
                                )
                                return
                        else:
                            failed += 1
                            continue
                    
                    # Small delay between downloads
                    await asyncio.sleep(0.5)
                    
                except Exception as e:
                    failed += 1
                    await asyncio.sleep(1)
            
            # Upload remaining files in last batch
            if os.listdir(download_path):
                await status_msg.edit_text(
                    f"📤 Uploading final batch #{batch_num}..."
                )
                batch_uploaded = await self._upload_pending_files(download_path, channel_id, status_msg)
                uploaded += batch_uploaded
            
            await status_msg.edit_text(
                f"✅ **All Done!**\n\n"
                f"📤 Uploaded: {uploaded}\n"
                f"⏭️ Skipped (>200MB): {skipped}\n"
                f"❌ Failed: {failed}\n"
                f"📁 Total files: {total_files}\n"
                f"📦 Batches used: {batch_num}\n"
                f"🌐 Total downloaded: {self.total_downloaded_mb:.0f}MB"
            )
        
        finally:
            # Clean up download directory
            if os.path.exists(download_path):
                shutil.rmtree(download_path, ignore_errors=True)

    async def _upload_pending_files(self, download_path: str, channel_id: int, status_msg: Message) -> int:
        """Upload all files currently in download folder to Telegram, then delete them"""
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
                
                await status_msg.edit_text(
                    f"📤 Uploading `{file_name}` to channel..."
                )
                
                await self._upload_to_channel(channel_id, file_path, file_name)
                uploaded += 1
                
                # Delete after upload
                if os.path.exists(file_path):
                    os.remove(file_path)
                
                # Delay to avoid Telegram flood
                await asyncio.sleep(2)
                
            except Exception as e:
                # Still try to delete the file
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
