import os
import traceback
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from pyrogram import Client, filters, errors, raw
from pyrogram.types import Message

# -------------------------
# ENV
# -------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TELEGRAM_2FA_PASSWORD = os.getenv("TELEGRAM_2FA_PASSWORD", "")  # optional

# -------------------------
# SESSION STORAGE
# -------------------------
SESSION_DIR = os.getenv("SESSION_DIR", "/data/sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

def make_session_path(name: str) -> str:
    return os.path.join(SESSION_DIR, name)

# -------------------------
# BOT CLIENT (manager bot)
# -------------------------
app_bot = Client(
    make_session_path("manager_bot"),
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user_sessions = {}

async def start_with_optional_2fa(app: Client):
    try:
        await app.start()
    except errors.SessionPasswordNeeded:
        if TELEGRAM_2FA_PASSWORD:
            app.password = TELEGRAM_2FA_PASSWORD
            await app.start()
        else:
            raise

async def pick_gift_id(app: Client, requested: int | None) -> int:
    gifts_obj = await app.invoke(raw.functions.payments.GetStarGifts(hash=0))
    gifts = getattr(gifts_obj, "gifts", [])
    if not gifts:
        raise RuntimeError("No available gifts found in Telegram catalog.")

    if requested is not None:
        for g in gifts:
            if getattr(g, "id", None) == requested:
                return requested

    return gifts[0].id

@asynccontextmanager
async def lifespan(app: FastAPI):
    await app_bot.start()
    print("✅ Telegram Bot Started!")
    yield
    await app_bot.stop()

app = FastAPI(lifespan=lifespan)

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
        await start_with_optional_2fa(client)
        peer = await client.resolve_peer(clean_target)

        requested = None
        if gift_id:
            try:
                requested = int(gift_id)
            except ValueError:
                return JSONResponse(status_code=400, content={"error": "gift_id must be numeric if provided"})

        valid_gift_id = await pick_gift_id(client, requested)

        invoice = raw.types.InputInvoiceStarGift(
            peer=peer,
            gift_id=valid_gift_id,
            hide_name=hide_name,
            include_upgrade=include_upgrade,
            message=raw.types.TextWithEntities(text=message, entities=[])
        )

        form = await client.invoke(raw.functions.payments.GetPaymentForm(invoice=invoice))
        form_id = getattr(form, "form_id", None) or getattr(form, "id", None)
        if not form_id:
            raise RuntimeError("Could not retrieve form_id from Telegram.")

        result = await client.invoke(
            raw.functions.payments.SendStarsForm(form_id=form_id, invoice=invoice)
        )

        return {
            "status": "success",
            "target": clean_target,
            "session": session,
            "gift_id_used": valid_gift_id,
            "result": str(result)
        }

    except errors.SessionPasswordNeeded:
        return JSONResponse(status_code=401, content={"error": "2FA required. Set TELEGRAM_2FA_PASSWORD."})
    except errors.RPCError as e:
        return JSONResponse(status_code=400, content={"error": f"Telegram RPCError: {e}"})
    except Exception as e:
        print("❌ /send-gift error:", str(e))
        print(traceback.format_exc())
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        if client.is_connected:
            await client.stop()

# -------------------------
# BOT: session create
# -------------------------
@app_bot.on_message(filters.command("start") & filters.private)
async def start_cmd(c, m: Message):
    await m.reply("👋 Send a name to create a session (example: tgdev).")

@app_bot.on_message(filters.text & filters.private)
async def handle_steps(c, m: Message):
    user_id = m.from_user.id

    if user_id not in user_sessions:
        name = m.text.replace(" ", "_").strip()
        user_sessions[user_id] = {"name": name, "step": "phone"}
        await m.reply(f"📁 Name: `{name}`\nSend Phone Number (with country code):")
        return

    state = user_sessions[user_id]

    if state["step"] == "phone":
        state["phone"] = m.text.strip()
        path = make_session_path(state["name"])

        temp_client = Client(path, api_id=API_ID, api_hash=API_HASH)
        await temp_client.connect()

        code = await temp_client.send_code(state["phone"])
        state.update({"hash": code.phone_code_hash, "client": temp_client, "step": "otp"})
        await m.reply("📩 OTP Sent! Now send OTP:")
        return

    if state["step"] == "otp":
        try:
            await state["client"].sign_in(state["phone"], state["hash"], m.text.strip())
            await m.reply(f"✅ Session `{state['name']}` saved!")
            await state["client"].disconnect()
            del user_sessions[user_id]
        except errors.SessionPasswordNeeded:
            state["step"] = "2fa"
            await m.reply("🔐 Enter 2FA Password:")
        except Exception as e:
            await m.reply(f"❌ OTP failed: {e}")

    elif state["step"] == "2fa":
        try:
            await state["client"].check_password(m.text)
            await m.reply(f"✅ Session `{state['name']}` saved!")
            await state["client"].disconnect()
            del user_sessions[user_id]
        except Exception as e:
            await m.reply(f"❌ 2FA failed: {e}")
