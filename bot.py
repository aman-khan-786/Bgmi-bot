import os
import re
import time
import urllib.request
import threading
from collections import deque
from pyrogram import Client, filters
from flask import Flask

# =====================================================
# 1. DUMMY WEB SERVER (RENDER FREE TIER BYPASS)
# =====================================================
web_app = Flask(__name__)

@web_app.route('/')
def health_check():
    return "C2 Engine is Online, Autonomous and Scraping!"

def run_web():
    # Render dynamic port assignment handle karega
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# =====================================================
# 2. AUTONOMOUS SELF-PING ENGINE (NO TERMUX)
# =====================================================
def auto_wake_engine():
    # Render automatically provides RENDER_EXTERNAL_URL, fallback tere link par set hai
    url = os.environ.get("RENDER_EXTERNAL_URL", "https://bgmi-bot-ebm7.onrender.com")
    while True:
        time.sleep(240)  # Har 4 minute me ping trigger hoga
        try:
            urllib.request.urlopen(url)
            print(f"[+] Self-Ping Fire! {url} is awake.")
        except Exception as e:
            print(f"[-] Self-Ping Missed: {e}")

threading.Thread(target=auto_wake_engine, daemon=True).start()

# =====================================================
# 3. CORE TELEGRAM MTPROTO ENGINE
# =====================================================
# Environment Variables se credentials fetch karna
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
MY_CHANNEL_ID = int(os.environ.get("MY_CHANNEL_ID"))
SECRET_KEY = os.environ.get("SECRET_KEY", "ALPHA_BOT")

# In-memory ultra-fast storage
active_channels = set()
processed_msg_ids = deque(maxlen=1000)

# Hardcore Regex for BGMI Keys (20-32 characters alphanumeric)
KEY_PATTERN = re.compile(r"\b[a-zA-Z0-9]{20,32}\b")

# App initialization (Pure Userbot Mode via Session String)
app = Client("shadow_bot", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)

# Module: Admin Control (Add Targets via Saved Messages)
@app.on_message(filters.me & filters.text & filters.regex(rf"^{SECRET_KEY}\s+ADD\s+(-\d+)"))
async def add_new_channel(client, message):
    try:
        new_channel_id = int(message.matches[0].group(1))
        active_channels.add(new_channel_id)
        await message.reply_text(
            f"✅ **Target Locked!**\nMonitoring Channel ID: `{new_channel_id}`\nAutonomous Engine Active."
        )
    except Exception as e:
        await message.reply_text(f"❌ Error locking target: {str(e)}")

# Module: Core Scraper (Extracts APKs and Keys)
@app.on_message(filters.channel | filters.group)
async def intercept_and_forward(client, message):
    chat_id = message.chat.id
    
    # Ignore irrelevant channels
    if chat_id not in active_channels:
        return

    # Anti-Duplicate Check
    if message.id in processed_msg_ids:
        return
    processed_msg_ids.append(message.id)

    # Action A: APK Extraction
    if message.document:
        file_name = (message.document.file_name or "").lower()
        if file_name.endswith(".apk") or message.document.mime_type == "application/vnd.android.package-archive":
            # .copy() ensures the 'forwarded from' tag is removed
            await message.copy(MY_CHANNEL_ID)
            return

    # Action B: Key & Text Extraction
    text_content = message.text or message.caption or ""
    key_match = KEY_PATTERN.search(text_content)
    
    if key_match:
        extracted_key = key_match.group(0)
        await client.send_message(
            MY_CHANNEL_ID,
            f"🔥 **New Key Detected!**\n\n`{extracted_key}`\n\n_System Auto-grab_"
        )

if __name__ == "__main__":
    print("Final C2 Server Initialized... Booting MTProto Engine!")
    app.run()
