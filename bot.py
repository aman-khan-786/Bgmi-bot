import os
import re
import time
import urllib.request
import threading
import asyncio
from collections import deque

# =====================================================
# 0. ASYNCIO FIX FOR PYTHON 3.14+ (CRITICAL FOR RENDER)
# =====================================================
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

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
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# =====================================================
# 2. AUTONOMOUS SELF-PING ENGINE (KEEPS SERVER AWAKE)
# =====================================================
def auto_wake_engine():
    url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")
    while True:
        time.sleep(240)  # Pings every 4 minutes
        try:
            urllib.request.urlopen(url)
            print(f"[+] Self-Ping Fire! Engine is awake.")
        except Exception as e:
            print(f"[-] Self-Ping Missed: {e}")

threading.Thread(target=auto_wake_engine, daemon=True).start()

# =====================================================
# 3. CORE TELEGRAM MTPROTO ENGINE
# =====================================================
try:
    API_ID = int(os.environ.get("API_ID"))
    API_HASH = os.environ.get("API_HASH")
    SESSION_STRING = os.environ.get("SESSION_STRING")
    # MY_CHANNEL_ID ab ek primary destination hai, multiple ke liye list bhi use kar sakte hain
    MY_CHANNEL_ID = int(os.environ.get("MY_CHANNEL_ID"))
    SECRET_KEY = os.environ.get("SECRET_KEY", "ALPHA_BOT")
except TypeError as e:
    print(f"[-] CRITICAL BOOT ERROR: Environment Variables Missing or Invalid! Ensure MY_CHANNEL_ID has -100. Error: {e}")
    exit(1)

# Multiple Target Channels support ke liye set ki jagah dynamic storage
active_channels = set()

# Optimized deque to prevent memory overflow on Render Free Tier
processed_msg_ids = deque(maxlen=2000)
KEY_PATTERN = re.compile(r"\b[a-zA-Z0-9]{20,32}\b")

app = Client("shadow_bot", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)

# Command se dynamic channel add karne ka feature (Tera original logic)
@app.on_message(filters.me & filters.text & filters.regex(rf"^{SECRET_KEY}\s+ADD\s+(-\d+)"))
async def add_new_channel(client, message):
    try:
        new_channel_id = int(message.matches[0].group(1))
        active_channels.add(new_channel_id)
        await message.reply_text(
            f"✅ **Target Locked!**\nMonitoring Channel ID: `{new_channel_id}`\nAutonomous Engine Active."
        )
        print(f"[+] Successfully added target channel: {new_channel_id}")
    except Exception as e:
        print(f"[-] Error adding channel: {e}")
        await message.reply_text(f"❌ Error locking target: {str(e)}")

# MULTI-CHANNEL & MULTI-DESTINATION INTERCEPTOR ENGINE
@app.on_message(filters.channel | filters.group | filters.private)
async def intercept_and_forward(client, message):
    try:
        chat_id = message.chat.id
        
        # Fast exit if channel is not in active target list
        if chat_id not in active_channels:
            return

        print(f"[DEBUG] Incoming payload from Target ID: {chat_id}")

        # Prevent duplicate processing
        if message.id in processed_msg_ids:
            return
        processed_msg_ids.append(message.id)

        # Destined channels list (Agar ek se zyada jagah bhejna ho to is list me IDs daal sakta hai)
        destinations = [MY_CHANNEL_ID] # Tu chahe toh yahan aur bhi IDs add kar sakta hai: [MY_CHANNEL_ID, -100999999999]

        for dest_id in destinations:
            # Action A: APK Extraction (Hardcore File Verification)
            if message.document:
                file_name = (message.document.file_name or "").lower()
                mime_type = message.document.mime_type or ""
                if file_name.endswith(".apk") or "android.package-archive" in mime_type:
                    print(f"[+] APK detected ({file_name}). Mirroring to Destination: {dest_id}...")
                    await message.copy(dest_id)
                    print(f"[+] APK Mirrored Successfully to {dest_id}!")
                    continue

            # Action B: Key & Text Extraction (Regex Parsing)
            text_content = message.text or message.caption or ""
            if text_content:
                key_match = KEY_PATTERN.search(text_content)
                if key_match:
                    extracted_key = key_match.group(0)
                    print(f"[+] Key pattern matched: {extracted_key} | Forwarding to {dest_id}...")
                    await client.send_message(
                        dest_id,
                        f"🔥 **New Key Detected!**\n\n`{extracted_key}`\n\n_System Auto-grab_"
                    )
                    print(f"[+] Key Forwarded Successfully to {dest_id}!")

    except Exception as e:
        print(f"[-] CRITICAL ERROR in intercept_and_forward: {e}")

if __name__ == "__main__":
    print("Final C2 Server Initialized... Booting MTProto Engine!")
    app.run()
