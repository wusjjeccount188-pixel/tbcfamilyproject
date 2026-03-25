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
from pyrogram.types import Message, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery

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
    except: return 0

async def pick_gift_id(app: Client, requested: int | None) -> int:
    gifts_obj = await app.invoke(raw.functions.payments.GetStarGifts(hash=0))
    gift_list = getattr(gifts_obj, "gifts", [])
    if not gift_list: raise RuntimeError("No gifts available.")
    if requested:
        for g in gift_list:
            if getattr(g, "id", None) == requested: return requested
    return gift_list[0].id

# -------------------------
# PAGINATION LOGIC
# -------------------------
def get_gift_page_text(gifts, page=0, page_size=5):
    start = page * page_size
    end = start + page_size
    current_gifts = gifts[start:end]
    
    out = f"🎁 **Telegram Gift Catalog (Page {page + 1})**\n\n"
    for g in current_gifts:
        emoji = "🎁"
        if hasattr(g, "sticker") and hasattr(g.sticker, "emoji"):
            emoji = g.sticker.emoji
        price = getattr(g, "stars", "N/A")
        out += f"{emoji} **ID:** `{g.id}`\n💰 **Price:** {price} Stars\n\n"
    
    if not current_gifts: return "No more gifts found."
    return out

def get_gift_pagination_markup(page, total_gifts, page_size=5):
    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton("⬅️ Back", callback_data=f"giftpage_{page-1}"))
    if (page + 1) * page_size < total_gifts:
        buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"giftpage_{page+1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None

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
        
        # DM Check
        try:
            peer = await client.resolve_peer(clean_target)
            has_dm = False
            async for _ in client.get_chat_history(clean_target, limit=1):
                has_dm = True
                break
            if not has_dm:
                return JSONResponse(status_code=403, content={"status": "error", "message": "Target must DM first."})
        except:
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
        
        return JSONResponse(status_code=200, content={"status": "success", "message": "Gift sent!", "gift_id": str(valid_gift_id)})
    except errors.RPCError as e:
        return JSONResponse(status_code=400, content={"status": "error", "error_name": e.ID, "message": e.MESSAGE})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
    finally:
        if client.is_connected: await client.stop()

# -------------------------
# BOT HANDLERS
# -------------------------
@app_bot.on_message(filters.command("start") & filters.private)
async def start_cmd(c, m: Message):
    await m.reply("🤖 **Control Panel Connected.**", reply_markup=get_main_keyboard())

@app_bot.on_callback_query(filters.regex(r"^giftpage_(\d+)"))
async def handle_pagination(c, cb: CallbackQuery):
    page = int(cb.matches.group(1))
    user_id = str(cb.from_user.id)
    mapping = load_mapping()
    user_keys = mapping.get(user_id, [])
    valid_keys = [k for k in user_keys if os.path.exists(make_session_path(k) + ".session")]
    if not valid_keys: return await cb.answer("No active Key.", show_alert=True)
    
    client = Client(make_session_path(valid_keys[0]), api_id=API_ID, api_hash=API_HASH)
    try:
        await client.start()
        gifts_obj = await client.invoke(raw.functions.payments.GetStarGifts(hash=0))
        gift_list = getattr(gifts_obj, "gifts", [])
        await cb.message.edit_text(get_gift_page_text(gift_list, page), reply_markup=get_gift_pagination_markup(page, len(gift_list)))
        await client.stop()
    except Exception as e: await cb.answer(f"Error: {e}")

@app_bot.on_message(filters.text & filters.private)
async def handle_bot_logic(c, m: Message):
    user_id = str(m.from_user.id)
    text = m.text
    mapping = load_mapping()

    if text == "🎁 Gift List":
        user_keys = mapping.get(user_id, [])
        valid_keys = [k for k in user_keys if os.path.exists(make_session_path(k) + ".session")]
        if not valid_keys: return await m.reply("❌ Create an API Key first.")
        status = await m.reply("⌛ Loading Catalog...")
        client = Client(make_session_path(valid_keys[0]), api_id=API_ID, api_hash=API_HASH)
        try:
            await client.start()
            gifts_obj = await client.invoke(raw.functions.payments.GetStarGifts(hash=0))
            gift_list = getattr(gifts_obj, "gifts", [])
            await status.edit_text(get_gift_page_text(gift_list, 0), reply_markup=get_gift_pagination_markup(0, len(gift_list)))
            await client.stop()
        except Exception as e: await status.edit_text(f"Error: {e}")
        return

    if text == "➕ Create API Key":
        user_states[user_id] = {"step": "phone"}
        return await m.reply("📱 Send **Phone Number**:", reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True))

    if text == "⚙️ API Key Settings":
        user_keys = mapping.get(user_id, [])
        valid_keys = [k for k in user_keys if os.path.exists(make_session_path(k) + ".session")]
        if not valid_keys: return await m.reply("No Keys found.")
        btns = [[k] for k in valid_keys] + [["❌ Cancel"]]
        return await m.reply("Select Key:", reply_markup=ReplyKeyboardMarkup(btns, resize_keyboard=True))

    if text == "❌ Cancel":
        if user_id in user_states: del user_states[user_id]
        return await m.reply("Stopped.", reply_markup=get_main_keyboard())

    # Settings Detail & Delete
    user_keys = mapping.get(user_id, [])
    if text in user_keys:
        client = Client(make_session_path(text), API_ID, API_HASH)
        status = await m.reply(f"⌛ Connecting to `{text}`...")
        try:
            await client.connect()
            me = await client.get_me()
            stars = await get_stars_balance(client)
            await status.delete()
            await m.reply(f"📊 **API Key Details**\n\n👤 Name: {me.first_name}\n⭐️ Balance: **{stars} Stars**\n📂 Key: `{text}`", 
                          reply_markup=ReplyKeyboardMarkup([[f"🗑 Delete {text}"], ["❌ Cancel"]], resize_keyboard=True))
        finally: await client.disconnect()
        return

    if text.startswith("🗑 Delete "):
        key = text.replace("🗑 Delete ", "").strip()
        if key in mapping.get(user_id, []):
            os.remove(make_session_path(key) + ".session")
            mapping[user_id].remove(key)
            save_mapping(mapping)
            return await m.reply(f"✅ Deleted `{key}`", reply_markup=get_main_keyboard())

    # Auth Flow
    if user_id in user_states:
        state = user_states[user_id]
        if state["step"] == "phone":
            state.update({"phone": text.replace(" ",""), "name": secrets.token_hex(4)})
            client = Client(make_session_path(state["name"]), API_ID, API_HASH)
            try:
                await client.connect()
                code = await client.send_code(state["phone"])
                state.update({"hash": code.phone_code_hash, "client": client, "step": "otp"})
                await m.reply("📩 **OTP Sent!** Enter code:")
            except Exception as e: await m.reply(f"Error: {e}"); del user_states[user_id]
        elif state["step"] == "otp":
            formatted_otp = " ".join(list(text.replace(" ","").strip()))
            try:
                await state["client"].sign_in(state["phone"], state["hash"], formatted_otp)
                if user_id not in mapping: mapping[user_id] = []
                mapping[user_id].append(state["name"])
                save_mapping(mapping)
                await m.reply(f"✅ Created: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect(); del user_states[user_id]
            except errors.SessionPasswordNeeded:
                state["step"] = "2fa"; await m.reply("🔐 Send 2FA Password:")
            except Exception as e: await m.reply(f"Error: {e}"); del user_states[user_id]
        elif state["step"] == "2fa":
            try:
                await state["client"].check_password(text.strip())
                if user_id not in mapping: mapping[user_id] = []
                mapping[user_id].append(state["name"])
                save_mapping(mapping)
                await m.reply(f"✅ Created: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect(); del user_states[user_id]
            except Exception as e: await m.reply(f"2FA Error: {e}")
