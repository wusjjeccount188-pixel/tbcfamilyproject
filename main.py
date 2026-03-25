import os
import secrets
import json
import traceback
import asyncio
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
async def get_stars_balance(client: Client):
    try:
        res = await client.invoke(raw.functions.payments.GetStarsStatus(peer=await client.resolve_peer("me")))
        if hasattr(res, "balance"):
            b = res.balance
            return getattr(b, "amount", b) if not isinstance(b, int) else b
        return 0
    except:
        return 0

async def pick_gift_id(app: Client, requested: int | None) -> int:
    gifts_obj = await app.invoke(raw.functions.payments.GetStarGifts(hash=0))
    gift_list = getattr(gifts_obj, "gifts", [])
    if not gift_list:
        raise RuntimeError("No gifts available in Telegram catalog.")

    if requested:
        for g in gift_list:
            if getattr(g, "id", None) == requested:
                return requested
    
    return gift_list[0].id # Pick first gift ID correctly

# -------------------------
# FASTAPI SETUP
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await app_bot.start()
    print("✅ System Online!")
    yield
    await app_bot.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def home():
    return {"status": "online", "message": "API Key & Gift Service Active"}

# -------------------------
# API: SEND GIFT (Hardened with JSON Errors)
# -------------------------
@app.api_route("/send-gift", methods=["GET", "POST"])
async def send_gift_api(
    target: str = Query(...),
    session: str = Query(...), 
    message: str = Query("Enjoy!"),
    gift_id: str | None = Query(None),
    hide_name: bool = Query(False),
    include_upgrade: bool = Query(False),
):
    clean_target = target.replace("@", "").strip()
    session_path = make_session_path(session)

    if not os.path.exists(session_path + ".session"):
        return JSONResponse(status_code=404, content={"status": "error", "message": f"API Key '{session}' not found!"})

    client = Client(session_path, api_id=API_ID, api_hash=API_HASH)

    try:
        await client.start()
        await asyncio.sleep(1.5) # Prevent ConnectionError

        # 1. Peer Resolution
        try:
            peer = await client.resolve_peer(clean_target)
        except Exception:
            return JSONResponse(status_code=403, content={
                "status": "error", 
                "message": f"Target user @{clean_target} must DM the account first or is invalid."
            })

        # 2. Gift Selection
        req_id = int(gift_id) if gift_id and gift_id.isnumeric() else None
        valid_gift_id = await pick_gift_id(client, req_id)

        # 3. Invoice & Payment Form
        invoice = raw.types.InputInvoiceStarGift(
            peer=peer, gift_id=valid_gift_id, hide_name=hide_name,
            include_upgrade=include_upgrade,
            message=raw.types.TextWithEntities(text=message, entities=[])
        )

        form = await client.invoke(raw.functions.payments.GetPaymentForm(invoice=invoice))
        form_id = getattr(form, "form_id", None) or getattr(form, "id", None)
        
        # 4. Final Send
        result = await client.invoke(raw.functions.payments.SendStarsForm(form_id=form_id, invoice=invoice))

        return JSONResponse(status_code=200, content={
            "status": "success",
            "message": "Gift sent successfully!",
            "data": {
                "target": clean_target,
                "api_key": session,
                "gift_id": str(valid_gift_id),
                "is_anonymous": hide_name
            }
        })

    except errors.RPCError as e:
        # Catch ALL Telegram Errors (Balance low, Flood, etc.)
        return JSONResponse(status_code=400, content={
            "status": "error",
            "error_code": e.CODE,
            "error_name": e.ID,
            "message": f"Telegram Error: {e.MESSAGE}"
        })
    except Exception as e:
        # Catch Unhandled Python errors
        print(f"CRITICAL ERROR: {traceback.format_exc()}")
        return JSONResponse(status_code=500, content={
            "status": "error",
            "message": f"Internal System Error: {str(e)}"
        })
    finally:
        if client.is_connected: await client.stop()

# -------------------------
# BOT HANDLERS
# -------------------------
@app_bot.on_message(filters.command("start") & filters.private)
async def start_cmd(c, m: Message):
    await m.reply("🤖 **Control Panel Connected.**", reply_markup=ReplyKeyboardMarkup([["➕ Create API Key", "⚙️ API Key Settings"]], resize_keyboard=True))

@app_bot.on_message(filters.text & filters.private)
async def handle_bot_logic(c, m: Message):
    user_id = str(m.from_user.id)
    text = m.text
    mapping = load_mapping()

    if text == "❌ Cancel":
        if user_id in user_states:
            if "client" in user_states[user_id]:
                try: await user_states[user_id]["client"].disconnect()
                except: pass
            del user_states[user_id]
        await m.reply("Action stopped.", reply_markup=ReplyKeyboardMarkup([["➕ Create API Key", "⚙️ API Key Settings"]], resize_keyboard=True))
        return

    if text == "➕ Create API Key":
        user_states[user_id] = {"step": "phone"}
        await m.reply("📱 Send **Phone Number** (with country code):", reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))
        return

    if text == "⚙️ API Key Settings":
        user_keys = mapping.get(user_id, [])
        valid_keys = [k for k in user_keys if os.path.exists(make_session_path(k) + ".session")]
        if not valid_keys:
            await m.reply("No Keys found.")
            return
        btns = [[k] for k in valid_keys]
        btns.append(["❌ Cancel"])
        await m.reply("Select Key:", reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))
        return

    user_keys = mapping.get(user_id, [])
    if text in user_keys:
        key_name = text
        path = make_session_path(key_name)
        client = Client(path, api_id=API_ID, api_hash=API_HASH)
        status = await m.reply(f"⌛ Connecting to `{key_name}`...")
        try:
            await client.connect()
            me = await client.get_me()
            stars = await get_stars_balance(client)
            info = f"📊 **API Key Details**\n\n👤 Name: {me.first_name}\n🆔 ID: `{me.id}`\n⭐️ Balance: **{stars} Stars**\n📂 Key: `{key_name}`"
            await status.delete()
            await m.reply(info, reply_markup=ReplyKeyboardMarkup([[f"🗑 Delete {key_name}"], ["❌ Cancel"]], resize_keyboard=True))
        except (errors.AuthKeyUnregistered, errors.SessionExpired):
            if os.path.exists(path + ".session"): os.remove(path + ".session")
            user_keys.remove(key_name)
            mapping[user_id] = user_keys
            save_mapping(mapping)
            await status.edit_text(f"❌ Key `{key_name}` expired and auto-deleted.")
        finally:
            if client.is_connected: await client.disconnect()
        return

    if text.startswith("🗑 Delete "):
        target = text.replace("🗑 Delete ", "").strip()
        if target in mapping.get(user_id, []):
            path = make_session_path(target) + ".session"
            if os.path.exists(path): os.remove(path)
            mapping[user_id].remove(target)
            save_mapping(mapping)
            await m.reply(f"✅ Key `{target}` deleted.", reply_markup=ReplyKeyboardMarkup([["➕ Create API Key", "⚙️ API Key Settings"]], resize_keyboard=True))
        return

    # Registration steps
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
                await m.reply("📩 **OTP Sent!** Enter code:", reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))
            except Exception as e:
                await m.reply(f"❌ Error: {e}")
                del user_states[user_id]
            return

        if state["step"] == "otp":
            formatted_otp = " ".join(list(text.replace(" ", "").strip()))
            try:
                await state["client"].sign_in(state["phone"], state["hash"], formatted_otp)
                if user_id not in mapping: mapping[user_id] = []
                mapping[user_id].append(state["name"])
                save_mapping(mapping)
                await m.reply(f"✅ Created: `{state['name']}`", reply_markup=ReplyKeyboardMarkup([["➕ Create API Key", "⚙️ API Key Settings"]], resize_keyboard=True))
                await state["client"].disconnect()
                del user_states[user_id]
            except errors.SessionPasswordNeeded:
                state["step"] = "2fa"
                await m.reply("🔐 **2FA Password Required.** Send Password:", reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))
            except Exception as e:
                await m.reply(f"❌ Error: {e}")
                del user_states[user_id]
            return

        if state["step"] == "2fa":
            try:
                await state["client"].check_password(text.strip())
                if user_id not in mapping: mapping[user_id] = []
                mapping[user_id].append(state["name"])
                save_mapping(mapping)
                await m.reply(f"✅ Created: `{state['name']}`", reply_markup=ReplyKeyboardMarkup([["➕ Create API Key", "⚙️ API Key Settings"]], resize_keyboard=True))
                await state["client"].disconnect()
                del user_states[user_id]
            except Exception as e:
                await m.reply(f"❌ 2FA Error: {e}")
