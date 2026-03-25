import os
import secrets
import string
import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from pyrogram import Client, filters, errors, raw
from pyrogram.types import (
    Message,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)

# -------------------------
# CONFIG & ENV
# -------------------------
load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SESSION_DIR = os.getenv("SESSION_DIR", "./sessions")
os.makedirs(SESSION_DIR, exist_ok=True)

# Temporary storage for login states
user_sessions = {}

# Prevent concurrent usage of the same session file
session_locks: dict[str, asyncio.Lock] = {}


def get_session_lock(session_name: str) -> asyncio.Lock:
    if session_name not in session_locks:
        session_locks[session_name] = asyncio.Lock()
    return session_locks[session_name]


# -------------------------
# UTILS
# -------------------------
def generate_secure_suffix(length=6):
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def get_all_sessions():
    return [
        f.replace(".session", "")
        for f in os.listdir(SESSION_DIR)
        if f.endswith(".session") and "manager_bot" not in f
    ]


# -------------------------
# BOT KEYBOARDS
# -------------------------
MAIN_MENU = ReplyKeyboardMarkup(
    [
        ["👤 Create via Account", "🤖 Create via Bot Token"],
        ["📊 Get Session Details"]
    ],
    resize_keyboard=True
)

# -------------------------
# PYROGRAM BOT INSTANCE
# -------------------------
app_bot = Client(
    "manager_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=SESSION_DIR
)

# -------------------------
# FASTAPI LIFESPAN
# -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    await app_bot.start()
    print("🚀 Bot and API are online!")
    yield
    await app_bot.stop()

app = FastAPI(lifespan=lifespan)

# -------------------------
# BOT HANDLERS
# -------------------------
@app_bot.on_message(filters.command("start") & filters.private)
async def start_handler(c, m: Message):
    await m.reply(
        "💎 **Advanced Session Manager**\n\n"
        "Choose an option below to create or manage your Telegram sessions securely.",
        reply_markup=MAIN_MENU
    )


@app_bot.on_message(filters.text & filters.private)
async def menu_logic(c, m: Message):
    user_id = m.from_user.id
    text = m.text

    if text == "👤 Create via Account":
        user_sessions[user_id] = {"step": "naming", "type": "user"}
        await m.reply("🏷️ Enter a **Nickname** for this account:")
        return

    if text == "🤖 Create via Bot Token":
        user_sessions[user_id] = {"step": "naming", "type": "bot"}
        await m.reply("🏷️ Enter a **Nickname** for this bot:")
        return

    if text == "📊 Get Session Details":
        sessions = get_all_sessions()
        if not sessions:
            await m.reply("📭 No sessions found in storage.")
            return

        await m.reply(f"📂 **Found {len(sessions)} sessions:**")
        for s in sessions:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 View Details & Stars", callback_data=f"info_{s}")],
                [InlineKeyboardButton("🗑️ Delete Session", callback_data=f"del_{s}")]
            ])
            await m.reply(f"📦 ID: `{s}`", reply_markup=kb)
        return

    if user_id not in user_sessions:
        return

    state = user_sessions[user_id]

    if state["step"] == "naming":
        safe_name = "".join(x for x in text if x.isalnum())[:10]
        random_id = generate_secure_suffix()
        final_filename = f"{safe_name}_{random_id}"
        state["filename"] = final_filename

        if state["type"] == "user":
            state["step"] = "phone"
            await m.reply(
                f"📄 File: `{final_filename}`\n"
                f"📞 Send **Phone Number** (with country code):"
            )
        else:
            state["step"] = "bot_token"
            await m.reply(
                f"📄 File: `{final_filename}`\n"
                f"🤖 Send **Bot Token**:"
            )
        return

    if state["step"] == "bot_token":
        client = Client(
            state["filename"],
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSION_DIR,
            no_updates=True
        )
        try:
            await client.connect()
            await client.sign_in_bot(text)
            await m.reply(f"✅ Bot session `{state['filename']}` saved!")
        except Exception as e:
            await m.reply(f"❌ Failed: {e}")
        finally:
            if client.is_connected:
                await client.disconnect()
            user_sessions.pop(user_id, None)
        return

    if state["step"] == "phone":
        client = Client(
            state["filename"],
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSION_DIR,
            no_updates=True
        )
        try:
            await client.connect()
            code = await client.send_code(text)
            state.update({
                "client": client,
                "phone": text,
                "hash": code.phone_code_hash,
                "step": "otp"
            })
            await m.reply("📩 OTP Sent! Enter code:")
        except Exception as e:
            await m.reply(f"❌ Error: {e}")
            if client.is_connected:
                await client.disconnect()
            user_sessions.pop(user_id, None)
        return

    if state["step"] == "otp":
        try:
            await state["client"].sign_in(state["phone"], state["hash"], text)
            await m.reply(f"✅ Session `{state['filename']}` active!")
            if state["client"].is_connected:
                await state["client"].disconnect()
            user_sessions.pop(user_id, None)
        except errors.SessionPasswordNeeded:
            state["step"] = "2fa"
            await m.reply("🔐 2FA Password required:")
        except Exception as e:
            await m.reply(f"❌ Failed: {e}")
        return

    if state["step"] == "2fa":
        try:
            await state["client"].check_password(text)
            await m.reply(f"✅ Session `{state['filename']}` (2FA) active!")
            if state["client"].is_connected:
                await state["client"].disconnect()
            user_sessions.pop(user_id, None)
        except Exception as e:
            await m.reply(f"❌ 2FA Error: {e}")


# -------------------------
# CALLBACKS
# -------------------------
@app_bot.on_callback_query()
async def handle_callbacks(c, q: CallbackQuery):
    data = q.data

    if data.startswith("info_"):
        session_id = data.replace("info_", "")
        await q.answer("Fetching data...")

        client = Client(
            session_id,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSION_DIR,
            no_updates=True
        )

        try:
            await client.connect()

            if not await client.get_me():
                await q.edit_message_text("❌ Session is not authorized.")
                return

            me = await client.get_me()
            stars = await client.invoke(
                raw.functions.payments.GetStarsStatus(
                    peer=await client.resolve_peer("me")
                )
            )
            balance = getattr(stars, "balance", 0)

            status_text = (
                f"📝 **Details for:** `{session_id}`\n\n"
                f"👤 **User:** {me.first_name}\n"
                f"🆔 **ID:** `{me.id}`\n"
                f"⭐ **Stars Available:** `{balance}`\n"
                f"🤖 **Is Bot:** {me.is_bot}"
            )
            await q.edit_message_text(status_text, reply_markup=q.message.reply_markup)

        except Exception as e:
            await q.edit_message_text(f"❌ Session Error: {e}\nSession might be expired.")
        finally:
            if client.is_connected:
                await client.disconnect()

    elif data.startswith("del_"):
        session_id = data.replace("del_", "")
        path = os.path.join(SESSION_DIR, f"{session_id}.session")
        if os.path.exists(path):
            os.remove(path)
            await q.edit_message_text(f"🗑️ Session `{session_id}` deleted permanently.")
        else:
            await q.answer("File not found.")


# -------------------------
# API ENDPOINT (GIFT SENDER)
# -------------------------
@app.get("/send-gift")
async def send_gift_api(
    target: str = Query(..., description="Username or peer"),
    session: str = Query(..., description="Session name without .session"),
    gift_id: int = Query(..., description="Telegram star gift id"),
    message: str = Query("Gift!")
):
    session_path = os.path.join(SESSION_DIR, f"{session}.session")
    if not os.path.exists(session_path):
        return JSONResponse(
            status_code=404,
            content={"error": "Session file missing."}
        )

    lock = get_session_lock(session)

    async with lock:
        client = Client(
            session,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=SESSION_DIR,
            no_updates=True
        )

        try:
            # connect() instead of start() -> no update handler issues
            await client.connect()

            me = await client.get_me()
            if not me:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Session is not authorized."}
                )

            clean_target = target.lstrip("@")
            peer = await client.resolve_peer(clean_target)

            invoice = raw.types.InputInvoiceStarGift(
                peer=peer,
                gift_id=gift_id,
                message=raw.types.TextWithEntities(
                    text=message,
                    entities=[]
                )
            )

            form = await client.invoke(
                raw.functions.payments.GetPaymentForm(invoice=invoice)
            )

            form_id = getattr(form, "form_id", None) or getattr(form, "id", None)
            if not form_id:
                return JSONResponse(
                    status_code=400,
                    content={"error": "Could not fetch payment form id."}
                )

            result = await client.invoke(
                raw.functions.payments.SendStarsForm(
                    form_id=form_id,
                    invoice=invoice
                )
            )

            return {
                "status": "success",
                "session": session,
                "target": clean_target,
                "gift_id": gift_id,
                "result": str(result)
            }

        except errors.UsernameNotOccupied:
            return JSONResponse(
                status_code=404,
                content={"error": f"Target username not found: {target}"}
            )
        except errors.PeerIdInvalid:
            return JSONResponse(
                status_code=400,
                content={"error": f"Invalid target peer: {target}"}
            )
        except errors.FloodWait as e:
            return JSONResponse(
                status_code=429,
                content={"error": f"Flood wait: retry after {e.value} seconds"}
            )
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"error": str(e)}
            )
        finally:
            if client.is_connected:
                await client.disconnect()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
