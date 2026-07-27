import os
import re
from collections import deque
from pyrogram import Client, filters

# Fetching details from Render Environment Variables
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
MY_CHANNEL_ID = int(os.environ.get("MY_CHANNEL_ID"))
SECRET_KEY = os.environ.get("SECRET_KEY", "ALPHA_BOT")

# Smart Variables (In-Memory)
active_channels = set()
processed_msg_ids = deque(maxlen=1000)
KEY_PATTERN = re.compile(r"\b[a-zA-Z0-9]{20,32}\b")

# Initialize the Userbot with Session String
app = Client("shadow_bot", session_string=SESSION_STRING, api_id=API_ID, api_hash=API_HASH)

@app.on_message(filters.me & filters.text & filters.regex(rf"^{SECRET_KEY}\s+ADD\s+(-\d+)"))
async def add_new_channel(client, message):
    try:
        new_channel_id = int(message.matches[0].group(1))
        active_channels.add(new_channel_id)
        await message.reply_text(f"✅ **Target Locked!**\nMonitoring Channel ID: `{new_channel_id}`")
    except Exception as e:
        await message.reply_text(f"❌ Error: {str(e)}")

@app.on_message(filters.channel | filters.group)
async def intercept_and_forward(client, message):
    chat_id = message.chat.id
    if chat_id not in active_channels:
        return

    if message.id in processed_msg_ids:
        return
    processed_msg_ids.append(message.id)

    # Extract APKs
    if message.document:
        file_name = (message.document.file_name or "").lower()
        if file_name.endswith(".apk") or message.document.mime_type == "application/vnd.android.package-archive":
            await message.copy(MY_CHANNEL_ID)
            return

    # Extract Keys
    text_content = message.text or message.caption or ""
    key_match = KEY_PATTERN.search(text_content)
    if key_match:
        extracted_key = key_match.group(0)
        await client.send_message(
            MY_CHANNEL_ID,
            f"🔥 **New Key Detected!**\n\n`{extracted_key}`\n\n_Auto-grabbed_"
        )

if __name__ == "__main__":
    print("Cloud Userbot Engine Started...")
    app.run()
