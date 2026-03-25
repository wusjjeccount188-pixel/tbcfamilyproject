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
            if getattr(g, "id", None) == requested: return requested
    return gift_list[0].id

def get_gift_name(gift) -> str:
    """Extract gift name: try title, then sticker emoji, else 'Gift'."""
    # Try title or name attribute
    name = getattr(gift, "title", None) or getattr(gift, "name", None)
    if name:
        return name
    # Fallback to sticker emoji
    sticker = getattr(gift, "sticker", None)
    if sticker:
        for attr in getattr(sticker, "attributes", []):
            if isinstance(attr, raw.types.DocumentAttributeSticker):
                emoji = getattr(attr, "alt", None)
                if emoji:
                    return emoji
    return "Gift"

def get_gift_emoji(gift) -> str:
    """Return the emoji from the sticker, if any."""
    sticker = getattr(gift, "sticker", None)
    if sticker:
        for attr in getattr(sticker, "attributes", []):
            if isinstance(attr, raw.types.DocumentAttributeSticker):
                emoji = getattr(attr, "alt", None)
                if emoji:
                    return emoji
    return "🎁"

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

# -------------------------
# API: SEND GIFT (Hardened with All Errors)
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
        await asyncio.sleep(1.5)

        # 1. PEER & DM CHECK
        try:
            peer = await client.resolve_peer(clean_target)
            has_history = False
            async for _ in client.get_chat_history(clean_target, limit=1):
                has_history = True
                break
            if not has_history:
                return JSONResponse(status_code=403, content={"status": "error", "message": "Security: Target user must DM this account first."})
        except Exception:
            return JSONResponse(status_code=403, content={"status": "error", "message": "User not found or no DM history."})

        # 2. SELECT GIFT
        req_id = int(gift_id) if gift_id and gift_id.isnumeric() else None
        valid_gift_id = await pick_gift_id(client, req_id)

        # 3. CONSTRUCT INVOICE (Fixed Hide Name)
        invoice = raw.types.InputInvoiceStarGift(
            peer=peer,
            gift_id=valid_gift_id,
            message=raw.types.TextWithEntities(text=message, entities=[]),
            hide_name=True if hide_name else None,
            include_upgrade=True if include_upgrade else None
        )

        form = await client.invoke(raw.functions.payments.GetPaymentForm(invoice=invoice))
        form_id = getattr(form, "form_id", None) or getattr(form, "id", None)
        
        # 4. SEND GIFT
        await client.invoke(raw.functions.payments.SendStarsForm(form_id=form_id, invoice=invoice))

        return JSONResponse(status_code=200, content={
            "status": "success",
            "message": "Gift sent successfully!",
            "data": {"target": clean_target, "key": session, "gift_id": str(valid_gift_id), "hidden": hide_name}
        })

    except errors.BalanceTooLow:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Balance too low! This account needs more Stars."})
    except errors.FloodWait as e:
        return JSONResponse(status_code=429, content={"status": "error", "message": f"Rate limit: Please wait {e.value} seconds."})
    except errors.UserPrivacyRestricted:
        return JSONResponse(status_code=403, content={"status": "error", "message": "User privacy settings prevent receiving this gift."})
    except errors.AuthKeyUnregistered:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Session expired! Please delete and recreate this API Key."})
    except errors.RPCError as e:
        return JSONResponse(status_code=400, content={"status": "error", "error_code": e.ID, "message": f"Telegram Error: {e.MESSAGE}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Internal Error: {str(e)}"})
    finally:
        if client.is_connected: await client.stop()

# -------------------------
# BOT HANDLERS
# -------------------------
@app_bot.on_message(filters.command("start") & filters.private)
async def start_cmd(c, m: Message):
    await m.reply(
        "🤖 **Control Panel Connected.**",
        reply_markup=ReplyKeyboardMarkup(
            [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
            resize_keyboard=True
        )
    )

@app_bot.on_callback_query(filters.regex("^gift_page:"))
async def handle_gift_pagination(c: Client, query: CallbackQuery):
    user_id = str(query.from_user.id)
    action = query.data.split(":")[1] if ":" in query.data else None

    # Get the user's gift browser state
    state = user_states.get(user_id, {}).get("gift_browser")
    if not state:
        await query.answer("Session expired. Please request gift list again.", show_alert=True)
        await query.message.delete()
        return

    gifts = state["gifts"]
    current_page = state["page"]
    per_page = 16
    total_pages = (len(gifts) + per_page - 1) // per_page

    if action == "next":
        if current_page + 1 < total_pages:
            new_page = current_page + 1
        else:
            await query.answer("You're on the last page.")
            return
    elif action == "prev":
        if current_page - 1 >= 0:
            new_page = current_page - 1
        else:
            await query.answer("You're on the first page.")
            return
    elif action == "close":
        # Delete the message and clear state
        await query.message.delete()
        del user_states[user_id]["gift_browser"]
        await query.answer("Gift list closed.")
        return
    else:
        await query.answer("Invalid action.")
        return

    # Update page in state
    state["page"] = new_page
    # Generate new message text
    start_idx = new_page * per_page
    end_idx = start_idx + per_page
    page_gifts = gifts[start_idx:end_idx]

    # Format gift entries
    lines = []
    for gift in page_gifts:
        name = get_gift_name(gift)
        emoji = get_gift_emoji(gift)
        gift_id = getattr(gift, "id", "?")
        price = getattr(gift, "stars", 0)
        lines.append(f"{emoji} **{name}**\n🆔 `{gift_id}` – {price}⭐\n")

    # Build the message
    text = f"**🎁 Available Gifts** (Page {new_page+1}/{total_pages})\n\n" + "\n".join(lines)

    # Build inline keyboard
    buttons = []
    nav_buttons = []
    if new_page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data="gift_page:prev"))
    if new_page + 1 < total_pages:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data="gift_page:next"))
    if nav_buttons:
        buttons.append(nav_buttons)
    buttons.append([InlineKeyboardButton("❌ Close", callback_data="gift_page:close")])

    # Edit the original message
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    await query.answer()

@app_bot.on_message(filters.text & filters.private)
async def handle_bot_logic(c, m: Message):
    user_id = str(m.from_user.id)
    text = m.text
    mapping = load_mapping()

    if text == "❌ Cancel":
        # Clear any ongoing state
        if user_id in user_states:
            # Disconnect any active client (if creating key)
            if "client" in user_states[user_id]:
                try: await user_states[user_id]["client"].disconnect()
                except: pass
            # Also clear gift browser if present
            if "gift_browser" in user_states[user_id]:
                del user_states[user_id]["gift_browser"]
            del user_states[user_id]
        await m.reply(
            "Cancelled.",
            reply_markup=ReplyKeyboardMarkup(
                [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
                resize_keyboard=True
            )
        )
        return

    if text == "🎁 Gift List":
        # Fetch fresh gift list
        try:
            gifts_obj = await app_bot.invoke(raw.functions.payments.GetStarGifts(hash=0))
            gifts = getattr(gifts_obj, "gifts", [])
            if not gifts:
                await m.reply("No gifts available at the moment.")
                return

            # Store gifts and initial page in user state
            if user_id not in user_states:
                user_states[user_id] = {}
            user_states[user_id]["gift_browser"] = {
                "gifts": gifts,
                "page": 0
            }

            # Build first page (page 0)
            per_page = 16
            start_idx = 0
            end_idx = min(per_page, len(gifts))
            page_gifts = gifts[start_idx:end_idx]

            lines = []
            for gift in page_gifts:
                name = get_gift_name(gift)
                emoji = get_gift_emoji(gift)
                gift_id = getattr(gift, "id", "?")
                price = getattr(gift, "stars", 0)
                lines.append(f"{emoji} **{name}**\n🆔 `{gift_id}` – {price}⭐\n")

            total_pages = (len(gifts) + per_page - 1) // per_page
            text = f"**🎁 Available Gifts** (Page 1/{total_pages})\n\n" + "\n".join(lines)

            # Build inline keyboard
            buttons = []
            nav_buttons = []
            if total_pages > 1:
                nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data="gift_page:next"))
            if nav_buttons:
                buttons.append(nav_buttons)
            buttons.append([InlineKeyboardButton("❌ Close", callback_data="gift_page:close")])

            # Send message and store message_id for future edits
            sent = await m.reply(text, reply_markup=InlineKeyboardMarkup(buttons))
            user_states[user_id]["gift_browser"]["message_id"] = sent.id
            user_states[user_id]["gift_browser"]["chat_id"] = sent.chat.id

        except Exception as e:
            await m.reply(f"❌ Failed to fetch gift list: {e}")
        return

    if text == "➕ Create API Key":
        user_states[user_id] = {"step": "phone"}
        await m.reply(
            "📱 Send **Phone Number** (with +):",
            reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)
        )
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
            await m.reply(
                info,
                reply_markup=ReplyKeyboardMarkup([[f"🗑 Delete {key_name}"], ["❌ Cancel"]], resize_keyboard=True)
            )
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
            await m.reply(
                f"✅ Key `{target}` deleted.",
                reply_markup=ReplyKeyboardMarkup(
                    [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
                    resize_keyboard=True
                )
            )
        return

    if user_id in user_states:
        state = user_states[user_id]
        # Only handle if this is a key creation flow (not gift browsing)
        if "step" in state:
            if state["step"] == "phone":
                state["phone"] = text.replace(" ", "")
                state["name"] = secrets.token_hex(4)
                client = Client(make_session_path(state["name"]), API_ID, API_HASH)
                try:
                    await client.connect()
                    code = await client.send_code(state["phone"])
                    state.update({"hash": code.phone_code_hash, "client": client, "step": "otp"})
                    await m.reply(
                        "📩 **OTP Sent!** Enter code:",
                        reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)
                    )
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
                    await m.reply(
                        f"✅ Created: `{state['name']}`",
                        reply_markup=ReplyKeyboardMarkup(
                            [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
                            resize_keyboard=True
                        )
                    )
                    await state["client"].disconnect()
                    del user_states[user_id]
                except errors.SessionPasswordNeeded:
                    state["step"] = "2fa"
                    await m.reply(
                        "🔐 **2FA Required.** Send Password:",
                        reply_markup=ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)
                    )
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
                    await m.reply(
                        f"✅ Created: `{state['name']}`",
                        reply_markup=ReplyKeyboardMarkup(
                            [["➕ Create API Key", "⚙️ API Key Settings"], ["🎁 Gift List"]],
                            resize_keyboard=True
                        )
                    )
                    await state["client"].disconnect()
                    del user_states[user_id]
                except Exception as e:
                    await m.reply(f"❌ 2FA Error: {e}")

@app.get("/")
async def health(): return {"status": "ok"}
