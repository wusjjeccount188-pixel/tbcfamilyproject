import os
import secrets
import json
import traceback
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from pyrogram import Client, filters, errors, raw
from pyrogram.types import Message, ReplyKeyboardMarkup

# -------------------------
# ENV & PATHS
# -------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

SESSION_DIR = os.getenv("SESSION_DIR", "/data/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

# User ownership mapping for privacy
MAPPING_FILE = os.path.join(SESSION_DIR, "user_mapping.json")

def load_mapping():
    if os.path.exists(MAPPING_FILE):
        try:
            with open(MAPPING_FILE, "r") as f: return json.load(f)
        except: return {}
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
        if hasattr(res, "balance"):
            balance_obj = res.balance
            return getattr(balance_obj, "amount", 0) if not isinstance(balance_obj, int) else balance_obj
        return 0
    except:
        return 0

async def pick_gift_id(app: Client, requested: int | None) -> int:
    gifts_obj = await app.invoke(raw.functions.payments.GetStarGifts(hash=0))
    gifts = getattr(gifts_obj, "gifts", [])
    if not gifts: raise RuntimeError("No gifts found.")
    if requested:
        for g in gifts:
            if getattr(g, "id", None) == requested: return requested
    return gifts[0].id

# -------------------------
# FASTAPI SETUP
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await app_bot.start()
    print("✅ Manager Bot & API Online!")
    yield
    await app_bot.stop()

app = FastAPI(lifespan=lifespan)

# -------------------------
# API ROUTE: SEND GIFT
# -------------------------
@app.api_route("/send-gift", methods=["GET", "POST"])
async def send_gift_api(
    target: str = Query(...),
    session: str = Query(...), # This is your 'Key' name
    message: str = Query("Enjoy!"),
    gift_id: str | None = Query(None),
    hide_name: bool = Query(False),
    include_upgrade: bool = Query(False),
):
    clean_target = target.replace("@", "").strip()
    session_path = make_session_path(session)

    if not os.path.exists(session_path + ".session"):
        return JSONResponse(status_code=404, content={"error": f"API Key '{session}' not found!"})

    client = Client(session_path, api_id=API_ID, api_hash=API_HASH)

    try:
        await client.start()
        peer = await client.resolve_peer(clean_target)
        
        req_id = int(gift_id) if gift_id and gift_id.isnumeric() else None
        valid_gift_id = await pick_gift_id(client, req_id)

        invoice = raw.types.InputInvoiceStarGift(
            peer=peer, gift_id=valid_gift_id, hide_name=hide_name,
            include_upgrade=include_upgrade,
            message=raw.types.TextWithEntities(text=message, entities=[])
        )

        form = await client.invoke(raw.functions.payments.GetPaymentForm(invoice=invoice))
        form_id = getattr(form, "form_id", None) or getattr(form, "id", None)
        
        result = await client.invoke(raw.functions.payments.SendStarsForm(form_id=form_id, invoice=invoice))

        return {"status": "success", "key_used": session, "gift_id": valid_gift_id, "result": str(result)}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if client.is_connected: await client.stop()

@app.get("/")
async def home():
    return {"status": "online", "message": "API Key Manager + Gift Service Active"}

# -------------------------
# BOT HANDLERS
# -------------------------

@app_bot.on_message(filters.command("start") & filters.private)
async def start_cmd(c, m: Message):
    await m.reply("🤖 **Control Panel Connected.** Choose an option:", reply_markup=get_main_keyboard())

@app_bot.on_message(filters.text & filters.private)
async def handle_bot_logic(c, m: Message):
    user_id = str(m.from_user.id)
    text = m.text
    mapping = load_mapping()

    # 1. CANCEL ACTION
    if text == "❌ Cancel":
        if user_id in user_states:
            if "client" in user_states[user_id]:
                try: await user_states[user_id]["client"].disconnect()
                except: pass
            del user_states[user_id]
        await m.reply("Action stopped.", reply_markup=get_main_keyboard())
        return

    # 2. START CREATE API KEY
    if text == "➕ Create API Key":
        user_states[user_id] = {"step": "phone"}
        await m.reply("📱 Send **Phone Number** (with country code):", reply_markup=get_cancel_keyboard())
        return

    # 3. SETTINGS (User's Private List)
    if text == "⚙️ API Key Settings":
        user_keys = mapping.get(user_id, [])
        valid_keys = [k for k in user_keys if os.path.exists(make_session_path(k) + ".session")]
        
        if not valid_keys:
            await m.reply("No API Keys found in your account.", reply_markup=get_main_keyboard())
            return

        btns = [[k] for k in valid_keys]
        btns.append(["❌ Cancel"])
        await m.reply("Select an API Key to manage:", reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))
        return

    # 4. VIEW DETAILS & AUTO-DELETE EXPIRED KEYS
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
            await msg.edit_text(info, reply_markup=ReplyKeyboardMarkup([[f"🗑 Delete {key_name}"], ["❌ Cancel"]], resize_keyboard=True))
            await msg.edit_text(info, reply_markup=del_kb)
        except (errors.AuthKeyUnregistered, errors.SessionExpired, errors.UserDeactivatedBan):
            # Auto-delete expired session
            if os.path.exists(path + ".session"): os.remove(path + ".session")
            if key_name in user_keys: user_keys.remove(key_name)
            mapping[user_id] = user_keys
            save_mapping(mapping)
            await msg.edit_text(f"❌ Key `{key_name}` has expired and was auto-deleted.")
        except Exception as e:
            await msg.edit_text(f"❌ Error: {e}")
        finally:
            if client.is_connected: await client.disconnect()
        return

    # 5. DELETE API KEY
    if text.startswith("🗑 Delete "):
        target = text.replace("🗑 Delete ", "").strip()
        user_keys = mapping.get(user_id, [])
        if target in user_keys:
            path = make_session_path(target) + ".session"
            if os.path.exists(path): os.remove(path)
            user_keys.remove(target)
            mapping[user_id] = user_keys
            save_mapping(mapping)
            await m.reply(f"✅ API Key `{target}` deleted.", reply_markup=get_main_keyboard())
        return

    # 6. REGISTRATION STEPS (Phone -> OTP -> 2FA)
    if user_id in user_states:
        state = user_states[user_id]

        if state["step"] == "phone":
            state["phone"] = text.replace(" ", "").strip()
            state["name"] = secrets.token_hex(4) # Random Name
            client = Client(make_session_path(state["name"]), API_ID, API_HASH)
            try:
                await client.connect()
                code = await client.send_code(state["phone"])
                state.update({"hash": code.phone_code_hash, "client": client, "step": "otp"})
                await m.reply("📩 **OTP Sent!** Enter code (e.g. 12345):")
            except Exception as e:
                await m.reply(f"❌ Error: {e}", reply_markup=get_main_keyboard())
                if client.is_connected: await client.disconnect()
                del user_states[user_id]
            return

        if state["step"] == "otp":
            # Auto format OTP: "12345" -> "1 2 3 4 5"
            formatted_otp = " ".join(list(text.replace(" ", "").strip()))
            try:
                await state["client"].sign_in(state["phone"], state["hash"], formatted_otp)
                
                # Save to user mapping
                if user_id not in mapping: mapping[user_id] = []
                mapping[user_id].append(state["name"])
                save_mapping(mapping)
                
                await m.reply(f"✅ API Key Created: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect()
                del user_states[user_id]
            except errors.SessionPasswordNeeded:
                state["step"] = "2fa"
                await m.reply("🔐 **2FA Password Required.** Send it:")
            except Exception as e:
                await m.reply(f"❌ Error: {e}", reply_markup=get_main_keyboard())
                if state["client"].is_connected: await state["client"].disconnect()
                del user_states[user_id]
            return

        if state["step"] == "2fa":
            try:
                await state["client"].check_password(text.strip())
                if user_id not in mapping: mapping[user_id] = []
                mapping[user_id].append(state["name"])
                save_mapping(mapping)
                await m.reply(f"✅ API Key Created: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect()
                del user_states[user_id]
            except Exception as e:
                await m.reply(f"❌ 2FA Error: {e}")

# -------------------------
# Keep Original /send-gift API Logic here if needed
# -------------------------
