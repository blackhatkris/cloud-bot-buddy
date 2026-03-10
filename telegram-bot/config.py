import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "200"))
MAX_UPLOAD_SIZE_MB = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "./downloads")
MEGA_EMAIL = os.getenv("MEGA_EMAIL", "")
MEGA_PASSWORD = os.getenv("MEGA_PASSWORD", "")
