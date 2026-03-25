import os
import secrets
import json
import traceback
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from pyrogram import Client, filters, errors, raw
from pyrogram.types import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

# -------------------------
# ENV & PATHS
# -------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

SESSION_DIR = os.getenv("SESSION_DIR", "/data/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

# User ownership mapping to keep sessions private
MAPPING_FILE = os.path.join(SESSION_DIR, "user_mapping.json")

def load_mapping():
    if os.path.exists(MAPPING_FILE):
        with open(MAPPING_FILE, "r") as f: return json.load(f)
    return {}

def save_mapping(mapping):
    with open(MAPPING_FILE, "w") as f: json.dump(mapping, f)

def make_session_path(name: str) -> str:
    return os.path.join(SESSION_DIR, name)

# -------------------------
# BOT CLIENT
# -------------------------
app_bot = Client(
    make_session_path("manager_bot"),
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user_states = {}

# -------------------------
# HELPERS
# -------------------------
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Create API Key", "⚙️ API Key Settings"]
    ], resize_keyboard=True)

def get_cancel_keyboard():
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)

async def get_stars_balance(client: Client):
    try:
        res = await client.invoke(raw.functions.payments.GetStarsStatus(peer=await client.resolve_peer("me")))
        # Only returning the integer amount
        return getattr(res, "balance", 0)
    except:
        return 0

# -------------------------
# FASTAPI
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await app_bot.start()
    print("✅ Manager Bot Started!")
    yield
    await app_bot.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def home():
    return {"status": "online"}

# -------------------------
# BOT LOGIC
# -------------------------

@app_bot.on_message(filters.command("start") & filters.private)
async def start_cmd(c, m: Message):
    await m.reply("🤖 **Control Panel Connected.**", reply_markup=get_main_keyboard())

@app_bot.on_message(filters.text & filters.private)
async def handle_logic(c, m: Message):
    user_id = str(m.from_user.id)
    text = m.text
    mapping = load_mapping()

    # 1. CANCEL
    if text == "❌ Cancel":
        if user_id in user_states:
            if "client" in user_states[user_id]:
                try: await user_states[user_id]["client"].disconnect()
                except: pass
            del user_states[user_id]
        await m.reply("Action stopped.", reply_markup=get_main_keyboard())
        return

    # 2. CREATE API KEY
    if text == "➕ Create API Key":
        user_states[user_id] = {"step": "phone"}
        await m.reply("📱 Send Phone Number:", reply_markup=get_cancel_keyboard())
        return

    # 3. SETTINGS (User's Private List)
    if text == "⚙️ API Key Settings":
        user_keys = mapping.get(user_id, [])
        valid_keys = []
        
        for key in user_keys:
            if os.path.exists(make_session_path(key) + ".session"):
                valid_keys.append(key)
        
        if not valid_keys:
            await m.reply("No API Keys found for you.")
            return

        btns = [[k] for k in valid_keys]
        btns.append(["❌ Cancel"])
        await m.reply("Select an API Key:", reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))
        return

    # 4. VIEW DETAILS & AUTO-DELETE EXPIRED
    user_keys = mapping.get(user_id, [])
    if text in user_keys:
        key_name = text
        path = make_session_path(key_name)
        client = Client(path, api_id=API_ID, api_hash=API_HASH)
        
        msg = await m.reply(f"⌛ Fetching `{key_name}` details...")
        try:
            await client.connect()
            me = await client.get_me()
            stars = await get_stars_balance(client)
            
            info = (
                f"📊 **API Key Details**\n\n"
                f"👤 Name: {me.first_name}\n"
                f"🆔 ID: `{me.id}`\n"
                f"⭐️ Balance: **{stars} Stars**\n"
                f"📂 Key: `{key_name}`"
            )
            del_kb = ReplyKeyboardMarkup([[f"🗑 Delete {key_name}"], ["❌ Cancel"]], resize_keyboard=True)
            await msg.edit_text(info, reply_markup=del_kb)
        except (errors.AuthKeyUnregistered, errors.SessionExpired, errors.UserDeactivatedBan):
            # Auto-delete from folder and mapping
            if os.path.exists(path + ".session"): os.remove(path + ".session")
            user_keys.remove(key_name)
            mapping[user_id] = user_keys
            save_mapping(mapping)
            await msg.edit_text(f"❌ Key `{key_name}` was expired and has been auto-deleted.")
        except Exception as e:
            await msg.edit_text(f"❌ Error: {e}")
        finally:
            if client.is_connected: await client.disconnect()
        return

    # 5. DELETE ACTION
    if text.startswith("🗑 Delete "):
        target = text.replace("🗑 Delete ", "").strip()
        if target in mapping.get(user_id, []):
            path = make_session_path(target) + ".session"
            if os.path.exists(path): os.remove(path)
            mapping[user_id].remove(target)
            save_mapping(mapping)
            await m.reply(f"✅ Key `{target}` deleted.", reply_markup=get_main_keyboard())
        return

    # 6. AUTH STEPS
    if user_id in user_states:
        state = user_states[user_id]

        if state["step"] == "phone":
            state["phone"] = text.replace(" ", "")
            state["name"] = secrets.token_hex(4)
            client = Client(make_session_path(state["name"]), API_ID, API_HASH)
            try:
                await client.connect()
                code = await client.send_code(state["phone"])
                state.update({"hash": code.phone_code_hash, "client": client, "step": "otp"})
                await m.reply("📩 Send OTP:")
            except Exception as e:
                await m.reply(f"❌ Error: {e}", reply_markup=get_main_keyboard())
                del user_states[user_id]
            return

        if state["step"] == "otp":
            formatted_otp = " ".join(list(text.replace(" ", "")))
            try:
                await state["client"].sign_in(state["phone"], state["hash"], formatted_otp)
                
                # Save ownership
                if user_id not in mapping: mapping[user_id] = []
                mapping[user_id].append(state["name"])
                save_mapping(mapping)
                
                await m.reply(f"✅ API Key Created: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect()
                del user_states[user_id]
            except errors.SessionPasswordNeeded:
                state["step"] = "2fa"
                await m.reply("🔐 Send 2FA Password:")
            except Exception as e:
                await m.reply(f"❌ Error: {e}", reply_markup=get_main_keyboard())
                del user_states[user_id]
            return

        if state["step"] == "2fa":
            try:
                await state["client"].check_password(text)
                if user_id not in mapping: mapping[user_id] = []
                mapping[user_id].append(state["name"])
                save_mapping(mapping)
                await m.reply(f"✅ API Key Created: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect()
                del user_states[user_id]
            except Exception as e:
                await m.reply(f"❌ 2FA Error: {e}")
