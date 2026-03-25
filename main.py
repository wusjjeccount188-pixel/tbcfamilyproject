import os
import secrets
import traceback
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from pyrogram import Client, filters, errors, raw
from pyrogram.types import Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

# -------------------------
# ENV
# -------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# -------------------------
# SESSION STORAGE
# -------------------------
SESSION_DIR = os.getenv("SESSION_DIR", "/data/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

def make_session_path(name: str) -> str:
    return os.path.join(SESSION_DIR, name)

# -------------------------
# BOT CLIENT (Manager Bot)
# -------------------------
app_bot = Client(
    make_session_path("manager_bot"),
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user_sessions = {}

# -------------------------
# KEYBOARD HELPERS
# -------------------------
def get_main_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Create Section", "⚙️ Section Settings"]
    ], resize_keyboard=True)

def get_cancel_keyboard():
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)

async def get_stars_balance(client: Client):
    try:
        # Fetching star balance via raw API
        res = await client.invoke(raw.functions.payments.GetStarsStatus(peer=await client.resolve_peer("me")))
        return getattr(res, "balance", 0)
    except:
        return "N/A"

# -------------------------
# FASTAPI SETUP
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await app_bot.start()
    print("✅ Telegram Manager Bot Started!")
    yield
    await app_bot.stop()

app = FastAPI(lifespan=lifespan)

# -------------------------
# API ROUTES (Original Gift Logic)
# -------------------------
@app.get("/")
async def home():
    import pyrogram
    return {
        "status": "online",
        "pyrogram_version": pyrogram.__version__,
        "session_dir": SESSION_DIR
    }

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
        return JSONResponse(status_code=404, content={"error": f"Session '{session}' not found!"})

    client = Client(session_path, api_id=API_ID, api_hash=API_HASH)

    try:
        await client.start()
        peer = await client.resolve_peer(clean_target)

        # Gift Picking Logic
        gifts_obj = await client.invoke(raw.functions.payments.GetStarGifts(hash=0))
        gifts = getattr(gifts_obj, "gifts", [])
        if not gifts: raise RuntimeError("No gifts found.")

        valid_gift_id = gifts[0].id
        if gift_id:
            for g in gifts:
                if str(getattr(g, "id", "")) == str(gift_id):
                    valid_gift_id = g.id

        invoice = raw.types.InputInvoiceStarGift(
            peer=peer, gift_id=valid_gift_id, hide_name=hide_name,
            include_upgrade=include_upgrade,
            message=raw.types.TextWithEntities(text=message, entities=[])
        )

        form = await client.invoke(raw.functions.payments.GetPaymentForm(invoice=invoice))
        form_id = getattr(form, "form_id", None) or getattr(form, "id", None)
        
        result = await client.invoke(raw.functions.payments.SendStarsForm(form_id=form_id, invoice=invoice))

        return {"status": "success", "session": session, "gift_id": valid_gift_id}

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if client.is_connected: await client.stop()

# -------------------------
# BOT HANDLERS (New Keyboard UI)
# -------------------------

@app_bot.on_message(filters.command("start") & filters.private)
async def start_cmd(c, m: Message):
    await m.reply("🤖 **Welcome!** Choose an action below:", reply_markup=get_main_keyboard())

@app_bot.on_message(filters.text & filters.private)
async def handle_bot_logic(c, m: Message):
    user_id = m.from_user.id
    text = m.text

    # 1. CANCEL ACTION
    if text == "❌ Cancel":
        if user_id in user_sessions:
            if "client" in user_sessions[user_id]:
                try: await user_sessions[user_id]["client"].disconnect()
                except: pass
            del user_sessions[user_id]
        await m.reply("Operation cancelled.", reply_markup=get_main_keyboard())
        return

    # 2. START CREATE SESSION
    if text == "➕ Create Section":
        user_sessions[user_id] = {"step": "phone"}
        await m.reply("📱 Enter **Phone Number** (with country code):", reply_markup=get_cancel_keyboard())
        return

    # 3. SECTION SETTINGS (List Sessions)
    if text == "⚙️ Section Settings":
        files = [f.replace(".session", "") for f in os.listdir(SESSION_DIR) 
                 if f.endswith(".session") and f != "manager_bot"]
        if not files:
            await m.reply("No accounts found. Create one first!", reply_markup=get_main_keyboard())
            return
        
        # Build Keyboard for each session
        btn_list = [[f] for f in files]
        btn_list.append(["❌ Cancel"])
        await m.reply("Select an account to manage:", reply_markup=ReplyKeyboardMarkup(btn_list, resize_keyboard=True))
        return

    # 4. VIEW SESSION DETAILS
    all_sessions = [f.replace(".session", "") for f in os.listdir(SESSION_DIR) if f.endswith(".session")]
    if text in all_sessions:
        session_name = text
        path = make_session_path(session_name)
        client = Client(path, api_id=API_ID, api_hash=API_HASH)
        
        loading_msg = await m.reply(f"⌛ Fetching details for `{session_name}`...")
        try:
            await client.connect()
            me = await client.get_me()
            stars = await get_stars_balance(client)
            info_text = (
                f"📊 **Account Details**\n\n"
                f"👤 Name: {me.first_name}\n"
                f"🆔 ID: `{me.id}`\n"
                f"📞 Phone: `{me.phone_number}`\n"
                f"⭐️ Balance: **{stars} Stars**\n"
                f"📂 File: `{session_name}.session`"
            )
            del_markup = ReplyKeyboardMarkup([[f"🗑 Delete {session_name}"], ["❌ Cancel"]], resize_keyboard=True)
            await loading_msg.edit_text(info_text, reply_markup=del_markup)
        except Exception as e:
            await loading_msg.edit_text(f"❌ Could not load session: {e}")
        finally:
            await client.disconnect()
        return

    # 5. DELETE SESSION
    if text.startswith("🗑 Delete "):
        target_session = text.replace("🗑 Delete ", "").strip()
        file_path = make_session_path(target_session) + ".session"
        if os.path.exists(file_path):
            os.remove(file_path)
            await m.reply(f"✅ Account `{target_session}` has been deleted.", reply_markup=get_main_keyboard())
        return

    # 6. REGISTRATION STEPS (Phone -> OTP -> 2FA)
    if user_id in user_sessions:
        state = user_sessions[user_id]

        # Step: Receive Phone
        if state["step"] == "phone":
            state["phone"] = text.replace(" ", "").strip()
            # Random Name like a5d5e47u
            state["name"] = secrets.token_hex(4) 
            path = make_session_path(state["name"])

            client = Client(path, api_id=API_ID, api_hash=API_HASH)
            try:
                await client.connect()
                sent_code = await client.send_code(state["phone"])
                state.update({"hash": sent_code.phone_code_hash, "client": client, "step": "otp"})
                await m.reply("📩 **OTP Sent!** Enter code (e.g. 12345):")
            except Exception as e:
                await m.reply(f"❌ Error: {e}", reply_markup=get_main_keyboard())
                if client.is_connected: await client.disconnect()
                del user_sessions[user_id]
            return

        # Step: Receive OTP
        if state["step"] == "otp":
            # Auto format OTP: "12345" -> "1 2 3 4 5"
            raw_otp = text.replace(" ", "").strip()
            formatted_otp = " ".join(list(raw_otp))
            
            try:
                await state["client"].sign_in(state["phone"], state["hash"], formatted_otp)
                await m.reply(f"✅ Created! Session Name: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect()
                del user_sessions[user_id]
            except errors.SessionPasswordNeeded:
                state["step"] = "2fa"
                await m.reply("🔐 **2FA Password Required.** Please send it:")
            except Exception as e:
                await m.reply(f"❌ OTP Error: {e}", reply_markup=get_main_keyboard())
                await state["client"].disconnect()
                del user_sessions[user_id]
            return

        # Step: Receive 2FA
        if state["step"] == "2fa":
            try:
                await state["client"].check_password(text.strip())
                await m.reply(f"✅ Created with 2FA! Name: `{state['name']}`", reply_markup=get_main_keyboard())
                await state["client"].disconnect()
                del user_sessions[user_id]
            except Exception as e:
                await m.reply(f"❌ 2FA Error: {e}")
