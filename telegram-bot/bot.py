import os
import asyncio
import shutil
from pyrogram import Client, filters
from pyrogram.types import Message
from config import API_ID, API_HASH, BOT_TOKEN, DOWNLOAD_DIR

# Import handlers
from handlers.mega_handler import MegaHandler
from handlers.forward_handler import ForwardHandler

# Create downloads directory
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

app = Client(
    "mega_forward_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# Store user states
user_states = {}
mega_handler = MegaHandler(app, user_states)
forward_handler = ForwardHandler(app, user_states)


@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message):
    await message.reply_text(
        "🤖 **Mega Downloader & Channel Forwarder Bot**\n\n"
        "📥 **Mega Commands:**\n"
        "  /setchannel - Set target channel for uploads\n"
        "  /mega - Download from Mega and upload to channel\n\n"
        "📤 **Forward Commands:**\n"
        "  /forward - Forward media between channels\n\n"
        "❓ /help - Show this message"
    )


@app.on_message(filters.command("help") & filters.private)
async def help_command(client: Client, message: Message):
    await message.reply_text(
        "📖 **How to use:**\n\n"
        "**Mega Downloader:**\n"
        "1. Add me to your channel as admin\n"
        "2. Use /setchannel to set target channel\n"
        "3. Use /mega to start downloading from Mega\n"
        "4. Files > 200MB will be skipped\n"
        "5. Media uploads as photos/videos (not documents)\n\n"
        "**Channel Forwarder:**\n"
        "1. Add me to source & target channels as admin\n"
        "2. Use /forward to start\n"
        "3. Set source channel, target channel\n"
        "4. Set start & end post links\n"
        "5. Set custom caption (optional)\n"
        "6. Media copied without 'Forwarded from' tag"
    )


# --- SET CHANNEL ---
@app.on_message(filters.command("setchannel") & filters.private)
async def set_channel(client: Client, message: Message):
    await mega_handler.set_channel(message)


# --- MEGA DOWNLOAD ---
@app.on_message(filters.command("mega") & filters.private)
async def mega_command(client: Client, message: Message):
    await mega_handler.start_mega(message)


# --- FORWARD ---
@app.on_message(filters.command("forward") & filters.private)
async def forward_command(client: Client, message: Message):
    await forward_handler.start_forward(message)


# --- HANDLE TEXT MESSAGES (state-based) ---
@app.on_message(filters.text & filters.private & ~filters.command(["start", "help", "setchannel", "mega", "forward"]))
async def handle_text(client: Client, message: Message):
    user_id = message.from_user.id
    state = user_states.get(user_id, {})
    current_state = state.get("state", "")

    # Mega handler states
    if current_state.startswith("mega_") or current_state.startswith("setchannel_"):
        await mega_handler.handle_input(message)
    # Forward handler states
    elif current_state.startswith("forward_"):
        await forward_handler.handle_input(message)
    else:
        pass  # Ignore random messages when no state is active


print("🤖 Bot is starting...")
app.run()
