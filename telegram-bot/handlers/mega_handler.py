import os
import asyncio
import mimetypes
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
            "I'll download all files (≤200MB each) and upload them to your channel."
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
            # Check if bot can send messages to this channel
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
            f"📏 Max file size: {MAX_FILE_SIZE_MB}MB\n\n"
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
        
        status_msg = await message.reply_text("⏳ Starting download from Mega...")
        
        try:
            await self._download_and_upload(message, status_msg, mega_link, channel_id)
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: `{str(e)}`")
        finally:
            self.user_states[user_id]["state"] = "idle"

    async def _download_and_upload(self, message: Message, status_msg: Message, mega_link: str, channel_id: int):
        """Download from Mega and upload to Telegram channel"""
        user_id = message.from_user.id
        download_path = os.path.join(DOWNLOAD_DIR, str(user_id))
        os.makedirs(download_path, exist_ok=True)
        
        try:
            # Login anonymously to Mega
            m = self.mega.login()
            
            await status_msg.edit_text("📥 Fetching file list from Mega...")
            
            # Check if it's a folder or file link
            if "/folder/" in mega_link or "#F!" in mega_link:
                # It's a folder
                files = m.get_files_in_node(m.import_public_url(mega_link))
                file_list = []
                for file_id, file_info in files.items():
                    if file_info['t'] == 0:  # Regular file
                        file_list.append((file_id, file_info))
            else:
                # Single file
                file_info = m.get_public_url_info(mega_link)
                file_list = [(None, file_info)]
            
            total = len(file_list)
            uploaded = 0
            skipped = 0
            
            await status_msg.edit_text(f"📁 Found **{total}** files. Starting download...")
            
            for idx, (file_id, file_info) in enumerate(file_list, 1):
                try:
                    file_name = file_info.get('a', {}).get('n', f'file_{idx}') if isinstance(file_info, dict) else getattr(file_info, 'name', f'file_{idx}')
                    file_size_mb = (file_info.get('s', 0) if isinstance(file_info, dict) else getattr(file_info, 'size', 0)) / (1024 * 1024)
                    
                    # Skip files > MAX_FILE_SIZE_MB
                    if file_size_mb > MAX_FILE_SIZE_MB:
                        skipped += 1
                        await status_msg.edit_text(
                            f"⏭️ Skipping `{file_name}` ({file_size_mb:.1f}MB > {MAX_FILE_SIZE_MB}MB)\n"
                            f"Progress: {idx}/{total}"
                        )
                        continue
                    
                    await status_msg.edit_text(
                        f"📥 Downloading `{file_name}` ({file_size_mb:.1f}MB)\n"
                        f"Progress: {idx}/{total}"
                    )
                    
                    # Download file
                    if file_id:
                        downloaded_file = m.download((file_id, file_info), download_path)
                    else:
                        downloaded_file = m.download_url(mega_link, download_path)
                    
                    if downloaded_file is None:
                        skipped += 1
                        continue
                    
                    file_path = str(downloaded_file)
                    
                    await status_msg.edit_text(
                        f"📤 Uploading `{file_name}` to channel...\n"
                        f"Progress: {idx}/{total}"
                    )
                    
                    # Upload to Telegram channel
                    await self._upload_to_channel(channel_id, file_path, file_name)
                    uploaded += 1
                    
                    # Delete local file after upload
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    
                    # Small delay to avoid flood
                    await asyncio.sleep(2)
                    
                except Exception as e:
                    skipped += 1
                    await status_msg.edit_text(f"⚠️ Error with file {idx}: `{str(e)[:100]}`")
                    await asyncio.sleep(1)
            
            await status_msg.edit_text(
                f"✅ **Upload Complete!**\n\n"
                f"📤 Uploaded: {uploaded}\n"
                f"⏭️ Skipped: {skipped}\n"
                f"📁 Total: {total}"
            )
        
        finally:
            # Clean up download directory
            if os.path.exists(download_path):
                import shutil
                shutil.rmtree(download_path, ignore_errors=True)

    async def _upload_to_channel(self, channel_id: int, file_path: str, file_name: str):
        """Upload file to Telegram channel as photo/video (not document)"""
        mime_type, _ = mimetypes.guess_type(file_path)
        file_size = os.path.getsize(file_path)
        
        # Telegram bot API limit is ~50MB for uploads
        # But with pyrogram (MTProto), we can upload up to 2GB
        max_size = MAX_UPLOAD_SIZE_MB * 1024 * 1024
        
        if mime_type and mime_type.startswith("image/"):
            # Upload as photo
            await self.app.send_photo(
                chat_id=channel_id,
                photo=file_path,
                caption=file_name
            )
        elif mime_type and mime_type.startswith("video/"):
            # Upload as video (not document)
            await self.app.send_video(
                chat_id=channel_id,
                video=file_path,
                caption=file_name,
                supports_streaming=True
            )
        else:
            # Other files as document
            await self.app.send_document(
                chat_id=channel_id,
                document=file_path,
                caption=file_name
            )
