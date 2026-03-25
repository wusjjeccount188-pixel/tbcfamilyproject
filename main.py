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
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Create API Key", "⚙️ API Key Settings"],
        ["🎁 Gift List"]
    ], resize_keyboard=True)

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
        raise RuntimeError("No gifts available.")
    if requested:
        for g in gift_list:
            if getattr(g, "id", None) == requested: return requested
    return gift_list[0].id

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
        return JSONResponse(status_code=404, content={"status": "error", "message": "API Key not found!"})

    client = Client(session_path, api_id=API_ID, api_hash=API_HASH)
    try:
        await client.start()
        await asyncio.sleep(1.5)
        try:
            peer = await client.resolve_peer(clean_target)
            has_history = False
            async for _ in client.get_chat_history(clean_target, limit=1):
                has_history = True
                break
            if not has_history:
                return JSONResponse(status_code=403, content={"status": "error", "message": "User must DM account first."})
        except Exception:
            return JSONResponse(status_code=403, content={"status": "error", "message": "User not found or no DM history."})

        req_id = int(gift_id) if gift_id and gift_id.isnumeric() else None
        valid_gift_id = await pick_gift_id(client, req_id)
        invoice = raw.types.InputInvoiceStarGift(
            peer=peer, gift_id=valid_gift_id,
            message=raw.types.TextWithEntities(text=message, entities=[]),
            hide_name=True if hide_name else None,
            include_upgrade=True if include_upgrade else None
        )
        form = await client.invoke(raw.functions.payments.GetPaymentForm(invoice=invoice))
        form_id = getattr(form, "form_id", None) or getattr(form, "id", None)
        await client.invoke(raw.functions.payments.SendStarsForm(form_id=form_id, invoice=invoice))
        return JSONResponse(status_code=200, content={"status": "success", "message": "Gift sent!"})
    except errors.RPCError as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": f"Telegram Error: {e.MESSAGE}"})
    finally:
        if client.is_connected: await client.stop()

# -------------------------
# BOT HANDLERS
# -------------------------
@app_bot.on_message(filters.command("start") & filters.private)
async def start_cmd(c, m: Message):
    await m.reply("🤖 **Control Panel Connected.**", reply_markup=get_main_keyboard())

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
        await m.reply("Cancelled.", reply_markup=get_main_keyboard())
        return

    # --- NEW: GIFT LIST HANDLER ---
    if text == "🎁 Gift List":
        user_keys = mapping.get(user_id, [])
        valid_keys = [k for k in user_keys if os.path.exists(make_session_path(k) + ".session")]
        if not valid_keys:
            await m.reply("❌ Create an API Key first to fetch the gift list.")
            return
        
        status = await m.reply("⌛ Fetching available gifts...")
        try:
            client = Client(make_session_path(valid_keys[0]), api_id=API_ID, api_hash=API_HASH)
            await client.start()
            gifts_obj = await client.invoke(raw.functions.payments.GetStarGifts(hash=0))
            gift_list = getattr(gifts_obj, "gifts", [])
            
            if not gift_list:
                await status.edit_text("No gifts available currently.")
            else:
                out = "🎁 **Available Telegram Gifts:**\n\n"
                for g in gift_list:
                    emoji = getattr(g, "sticker", None)
                    emoji_text = emoji.emoji if emoji and hasattr(emoji, "emoji") else "🎁"
                    # Fixed price extraction
                    price = getattr(g, "stars", 0)
                    out += f"{emoji_text} **ID:** `{g.id}`\n💰 **Price:** {price} Stars\n\n"
                await status.edit_text(out)
            await client.stop()
        except Exception as e:
            await status.edit_text(f"❌ Error fetching gifts: {e}")
        return

    if text == "➕ Create API Key":
        user_states[user_id] = {"step": "phone"}
        await m.reply("📱 Send **Phone Number**:", reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))
        return

    if text == "⚙️ API Key Settings":
        user_keys = mapping.get(user_id, [])
        valid_keys = [k for k in user_keys if os.path.exists(make_session_path(k) + ".session")]
        if not valid_keys:
            await m.reply("No Keys found.")
            return
        btns = [[k] for k in valid_keys] + [["❌ Cancel"]]
        await m.reply("Select Key:", reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))
        return

    # Details & Delete Logic
    user_keys = mapping.get(user_id, [])
    if text in user_keys:
        key_name = text
        client = Client(make_session_path(key_name), API_ID, API_HASH)
        status = await m.reply(f"⌛ Connecting to `{key_name}`...")
        try:
            await client.connect()
            me = await client.get_me()
            stars = await get_stars_balance(client)
            info = f"📊 **API Key Details**\n\n👤 Name: {me.first_name}\n⭐️ Balance: **{stars} Stars**\n📂 Key: `{key_name}`"
            await status.delete()
            await m.reply(info, reply_markup=ReplyKeyboardMarkup([[f"🗑 Delete {key_name}"], ["❌ Cancel"]], resize_keyboard=True))
        finally:
            if client.is_connected: await client.disconnect()
        return

    if text.startswith("🗑 Delete "):
        target = text.replace("🗑 Delete ", "").strip()
        if target in mapping.get(user_id, []):
            os.remove(make_session_path(target) + ".session")
            mapping[user_id].remove(target)
            save_mapping(mapping)
            await m.reply(f"✅ Key `{target}` deleted.", reply_markup=get_main_keyboard())
        return

    # Auth Flow
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
                await m.reply("📩 **OTP Sent!** Enter code:")
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
                await m.reply(f"✅ Created: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect()
                del user_states[user_id]
            except errors.SessionPasswordNeeded:
                state["step"] = "2fa"
                await m.reply("🔐 **2FA Password Required.** Send Password:")
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
                await m.reply(f"✅ Created: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect()
                del user_states[user_id]
            except Exception as e:
                await m.reply(f"❌ 2FA Error: {e}")
