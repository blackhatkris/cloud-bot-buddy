import asyncio
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.errors import FloodWait, ChatForwardsRestricted


class ForwardHandler:
    def __init__(self, app: Client, user_states: dict):
        self.app = app
        self.user_states = user_states

    async def start_forward(self, message: Message):
        user_id = message.from_user.id
        self.user_states[user_id] = {
            **self.user_states.get(user_id, {}),
            "state": "forward_source_channel"
        }
        await message.reply_text(
            "📤 **Forward Setup**\n\n"
            "Step 1: Send me the **source channel** ID or @username\n"
            "(The channel to copy FROM)"
        )

    async def handle_input(self, message: Message):
        user_id = message.from_user.id
        state = self.user_states.get(user_id, {})
        current_state = state.get("state", "")

        if current_state == "forward_source_channel":
            await self._set_source(message)
        elif current_state == "forward_target_channel":
            await self._set_target(message)
        elif current_state == "forward_start_link":
            await self._set_start_link(message)
        elif current_state == "forward_end_link":
            await self._set_end_link(message)
        elif current_state == "forward_caption":
            await self._set_caption(message)
        elif current_state == "forward_confirm":
            await self._handle_confirm(message)

    async def _set_source(self, message: Message):
        user_id = message.from_user.id
        channel = message.text.strip()
        
        try:
            chat = await self.app.get_chat(channel)
            self.user_states[user_id]["source_channel"] = chat.id
            self.user_states[user_id]["source_title"] = chat.title
            self.user_states[user_id]["state"] = "forward_target_channel"
            
            await message.reply_text(
                f"✅ Source: **{chat.title}**\n\n"
                f"Step 2: Now send me the **target channel** ID or @username\n"
                f"(The channel to copy TO)"
            )
        except Exception as e:
            await message.reply_text(f"❌ Could not access channel: `{str(e)}`\nTry again.")

    async def _set_target(self, message: Message):
        user_id = message.from_user.id
        channel = message.text.strip()
        
        try:
            chat = await self.app.get_chat(channel)
            member = await self.app.get_chat_member(chat.id, "me")
            
            if not (member.privileges and member.privileges.can_post_messages):
                await message.reply_text("❌ I can't post in this channel. Make me admin first.")
                return
            
            self.user_states[user_id]["target_channel"] = chat.id
            self.user_states[user_id]["target_title"] = chat.title
            self.user_states[user_id]["state"] = "forward_start_link"
            
            await message.reply_text(
                f"✅ Target: **{chat.title}**\n\n"
                f"Step 3: Send me the **link of the FIRST post** to start copying from.\n"
                f"Example: `https://t.me/channelname/123`"
            )
        except Exception as e:
            await message.reply_text(f"❌ Could not access channel: `{str(e)}`\nTry again.")

    async def _set_start_link(self, message: Message):
        user_id = message.from_user.id
        link = message.text.strip()
        
        msg_id = self._extract_msg_id(link)
        if msg_id is None:
            await message.reply_text("❌ Invalid link. Send a valid Telegram post link.")
            return
        
        self.user_states[user_id]["start_msg_id"] = msg_id
        self.user_states[user_id]["state"] = "forward_end_link"
        
        await message.reply_text(
            f"✅ Start post: #{msg_id}\n\n"
            f"Step 4: Send me the **link of the LAST post** to stop at."
        )

    async def _set_end_link(self, message: Message):
        user_id = message.from_user.id
        link = message.text.strip()
        
        msg_id = self._extract_msg_id(link)
        if msg_id is None:
            await message.reply_text("❌ Invalid link. Send a valid Telegram post link.")
            return
        
        start_id = self.user_states[user_id]["start_msg_id"]
        if msg_id < start_id:
            await message.reply_text("❌ End post must be after start post.")
            return
        
        self.user_states[user_id]["end_msg_id"] = msg_id
        self.user_states[user_id]["state"] = "forward_caption"
        
        total = msg_id - start_id + 1
        await message.reply_text(
            f"✅ End post: #{msg_id} (approx {total} posts)\n\n"
            f"Step 5: Send me a **custom caption** to use.\n"
            f"Send **skip** to keep original captions.\n\n"
            f"You can use these variables:\n"
            f"`{{original}}` - Original caption\n"
            f"`{{filename}}` - File name"
        )

    async def _set_caption(self, message: Message):
        user_id = message.from_user.id
        text = message.text.strip()
        
        if text.lower() == "skip":
            self.user_states[user_id]["custom_caption"] = None
        else:
            self.user_states[user_id]["custom_caption"] = text
        
        self.user_states[user_id]["state"] = "forward_confirm"
        
        state = self.user_states[user_id]
        caption_display = text if text.lower() != "skip" else "Original captions"
        
        await message.reply_text(
            f"📋 **Confirm Forward:**\n\n"
            f"📥 Source: **{state['source_title']}**\n"
            f"📤 Target: **{state['target_title']}**\n"
            f"🔢 Posts: #{state['start_msg_id']} → #{state['end_msg_id']}\n"
            f"📝 Caption: {caption_display}\n"
            f"🏷️ Forward tag: **Removed**\n\n"
            f"Send **yes** to start or **no** to cancel."
        )

    async def _handle_confirm(self, message: Message):
        user_id = message.from_user.id
        text = message.text.strip().lower()
        
        if text not in ["yes", "y", "ha", "haan"]:
            self.user_states[user_id]["state"] = "idle"
            await message.reply_text("❌ Cancelled.")
            return
        
        state = self.user_states[user_id]
        self.user_states[user_id]["state"] = "forward_running"
        
        status_msg = await message.reply_text("⏳ Starting forward process...")
        
        try:
            await self._do_forward(
                message, status_msg,
                state["source_channel"],
                state["target_channel"],
                state["start_msg_id"],
                state["end_msg_id"],
                state.get("custom_caption")
            )
        except Exception as e:
            await status_msg.edit_text(f"❌ Error: `{str(e)}`")
        finally:
            self.user_states[user_id]["state"] = "idle"

    async def _do_forward(self, message: Message, status_msg: Message,
                          source_id: int, target_id: int,
                          start_id: int, end_id: int,
                          custom_caption: str | None):
        """Copy messages without forward tag using file_id"""
        
        forwarded = 0
        skipped = 0
        total = end_id - start_id + 1
        
        for msg_id in range(start_id, end_id + 1):
            try:
                # Get the original message
                try:
                    msg = await self.app.get_messages(source_id, msg_id)
                except Exception:
                    skipped += 1
                    continue
                
                if msg.empty or msg.service:
                    skipped += 1
                    continue
                
                # Build caption
                caption = self._build_caption(msg, custom_caption)
                
                # Copy without forward tag using copy_message or sending via file_id
                if msg.photo:
                    await self.app.send_photo(
                        chat_id=target_id,
                        photo=msg.photo.file_id,
                        caption=caption
                    )
                elif msg.video:
                    await self.app.send_video(
                        chat_id=target_id,
                        video=msg.video.file_id,
                        caption=caption,
                        supports_streaming=True
                    )
                elif msg.document:
                    await self.app.send_document(
                        chat_id=target_id,
                        document=msg.document.file_id,
                        caption=caption
                    )
                elif msg.audio:
                    await self.app.send_audio(
                        chat_id=target_id,
                        audio=msg.audio.file_id,
                        caption=caption
                    )
                elif msg.animation:
                    await self.app.send_animation(
                        chat_id=target_id,
                        animation=msg.animation.file_id,
                        caption=caption
                    )
                elif msg.sticker:
                    await self.app.send_sticker(
                        chat_id=target_id,
                        sticker=msg.sticker.file_id
                    )
                elif msg.voice:
                    await self.app.send_voice(
                        chat_id=target_id,
                        voice=msg.voice.file_id,
                        caption=caption
                    )
                elif msg.video_note:
                    await self.app.send_video_note(
                        chat_id=target_id,
                        video_note=msg.video_note.file_id
                    )
                elif msg.text:
                    text_to_send = caption if custom_caption else msg.text
                    await self.app.send_message(
                        chat_id=target_id,
                        text=text_to_send
                    )
                else:
                    skipped += 1
                    continue
                
                forwarded += 1
                
                # Update progress every 10 messages
                if forwarded % 10 == 0:
                    await status_msg.edit_text(
                        f"⏳ Forwarding... {forwarded}/{total}\n"
                        f"✅ Sent: {forwarded} | ⏭️ Skipped: {skipped}"
                    )
                
                # Delay to avoid flood
                await asyncio.sleep(1.5)
                
            except FloodWait as e:
                await status_msg.edit_text(f"⏳ Rate limited. Waiting {e.value}s...")
                await asyncio.sleep(e.value + 1)
            except Exception as e:
                skipped += 1
                await asyncio.sleep(1)
        
        await status_msg.edit_text(
            f"✅ **Forward Complete!**\n\n"
            f"📤 Forwarded: {forwarded}\n"
            f"⏭️ Skipped: {skipped}\n"
            f"📁 Total: {total}"
        )

    def _build_caption(self, msg: Message, custom_caption: str | None) -> str:
        """Build caption from template or original"""
        if custom_caption is None:
            return msg.caption or ""
        
        original = msg.caption or ""
        filename = ""
        
        if msg.document:
            filename = msg.document.file_name or ""
        elif msg.video:
            filename = msg.video.file_name or ""
        elif msg.audio:
            filename = msg.audio.file_name or ""
        
        return custom_caption.replace("{original}", original).replace("{filename}", filename)

    def _extract_msg_id(self, link: str) -> int | None:
        """Extract message ID from Telegram post link"""
        try:
            # https://t.me/channelname/123
            parts = link.rstrip("/").split("/")
            return int(parts[-1])
        except (ValueError, IndexError):
            return None
