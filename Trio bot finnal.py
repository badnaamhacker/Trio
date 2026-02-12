import os
import logging
import datetime
from typing import Optional, List, Tuple

from dotenv import load_dotenv
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderUnavailable

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, DateTime,
    ForeignKey, UniqueConstraint, and_, or_
)
from sqlalchemy.orm import sessionmaker, declarative_base

from telegram import (
    Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    LabeledPrice,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, PreCheckoutQueryHandler,
    filters
)

# ----------------- LOGGING -----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ----------------- ENV -----------------
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("7998800242EO41qI4Q6zXiPzaal6CN7VO2Hc", "7998800242:AAFEdw_XQEO41Q6zXiPzaal6CN7VO2Hc").strip()
ADMIN_TELEGRAM_ID = int(os.getenv("5152271", "515671"))
BOT_USERNAME = os.getenv("@TrioConnectbot", "@TrioConnectbot").strip().lstrip("@")  # without @

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing in .env")
if not ADMIN_TELEGRAM_ID:
    logger.warning("ADMIN_TELEGRAM_ID missing. Admin panel will not work properly.")
if not BOT_USERNAME:
    logger.warning("BOT_USERNAME missing. Referral links may not work.")

# ----------------- DB -----------------
DATABASE_URL = "sqlite:///trio_connect.db"
engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# ----------------- MODELS -----------------
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)

    telegram_id = Column(Integer, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True, index=True)
    name = Column(String, nullable=True)

    age = Column(Integer, nullable=True)
    gender = Column(String, nullable=True)

    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    city = Column(String, nullable=True)
    country = Column(String, nullable=True)

    profile_picture_file_id = Column(String, nullable=True)
    is_registered = Column(Boolean, default=False)

    referred_by_id = Column(Integer, nullable=True)        # telegram_id of referrer
    referral_count = Column(Integer, default=0)            # successful referrals count
    referral_counted = Column(Boolean, default=False)      # for referred user: counted or not

    free_unlocks = Column(Integer, default=0)              # from referrals (1 per 3 successful)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

class BlockedProfile(Base):
    __tablename__ = "blocked_profiles"
    blocker_id = Column(Integer, primary_key=True)
    blocked_id = Column(Integer, primary_key=True)
    __table_args__ = (UniqueConstraint("blocker_id", "blocked_id", name="uq_block"),)

class MatchRequest(Base):
    __tablename__ = "match_requests"
    id = Column(Integer, primary_key=True)
    requester_id = Column(Integer, nullable=False, index=True)  # telegram_id
    target_id = Column(Integer, nullable=False, index=True)     # telegram_id
    purpose = Column(String, nullable=False)
    status = Column(String, default="Pending")                  # Pending/Accepted/Rejected
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (UniqueConstraint("requester_id", "target_id", name="uq_req"),)

class Match(Base):
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True)
    user1_id = Column(Integer, nullable=False, index=True)  # smaller telegram_id
    user2_id = Column(Integer, nullable=False, index=True)  # larger telegram_id
    purpose = Column(String, nullable=False)

    user1_unlocked = Column(Boolean, default=False)
    user2_unlocked = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (UniqueConstraint("user1_id", "user2_id", name="uq_match"),)

class Report(Base):
    __tablename__ = "reports"
    id = Column(Integer, primary_key=True)
    reporter_id = Column(Integer, nullable=False, index=True)
    reported_id = Column(Integer, nullable=False, index=True)
    reason = Column(String, nullable=False)
    status = Column(String, default="Pending")  # Pending/Reviewed
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

Base.metadata.create_all(engine)

# ----------------- GEO -----------------
geolocator = Nominatim(user_agent="trio-connect-bot")

# ----------------- STATES -----------------
(
    ST_CREATE_AGE, ST_CREATE_GENDER, ST_CREATE_LOCATION, ST_CREATE_PHOTO,
    ST_EDIT_MENU, ST_EDIT_AGE, ST_EDIT_GENDER, ST_EDIT_LOCATION, ST_EDIT_PHOTO,
    ST_FIND_FILTER, ST_FIND_BROWSE, ST_FIND_PURPOSE, ST_FIND_REPORT_REASON, ST_FIND_REPORT_TEXT,
    ST_DELETE_CONFIRM,
    ST_ADMIN_BC_AUDIENCE, ST_ADMIN_BC_SEND,
    ST_ADMIN_VIEW_USER, ST_ADMIN_DELETE_USER,
) = range(19)

# ----------------- HELPERS -----------------
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_TELEGRAM_ID

def db_session():
    return SessionLocal()

def upsert_user_from_telegram(update: Update) -> User:
    tg = update.effective_user
    session = db_session()
    try:
        user = session.query(User).filter_by(telegram_id=tg.id).first()
        full_name = (tg.full_name or tg.first_name or "User").strip()
        username = tg.username

        if not user:
            user = User(telegram_id=tg.id, name=full_name, username=username)
            session.add(user)
            session.commit()
        else:
            changed = False
            if user.name != full_name:
                user.name = full_name
                changed = True
            if user.username != username:
                user.username = username
                changed = True
            if changed:
                session.commit()

        session.refresh(user)
        return user
    finally:
        session.close()

def get_user(telegram_id: int) -> Optional[User]:
    session = db_session()
    try:
        return session.query(User).filter_by(telegram_id=telegram_id).first()
    finally:
        session.close()

def main_menu_kb() -> ReplyKeyboardMarkup:
    # As per your final menu: View, Edit, Find Match, Refer, Requests
    return ReplyKeyboardMarkup(
        [
            ["View Your Profile", "Edit Profile"],
            ["Find Match", "Requests"],
            ["Refer 3 users to unlock 1 username"],
        ],
        resize_keyboard=True
    )

async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "✅ Main Menu:"):
    await update.effective_message.reply_text(text, reply_markup=main_menu_kb())

def referral_link(user: User) -> str:
    if not BOT_USERNAME:
        return "BOT_USERNAME not set"
    return f"https://t.me/{BOT_USERNAME}?start=ref_{user.telegram_id}"

def profile_caption(u: User, show_username: bool) -> str:
    loc = "N/A"
    if u.city and u.country:
        loc = f"{u.city}, {u.country}"
    elif u.country:
        loc = u.country

    cap = (
        f"👤 Name: {u.name or 'N/A'}\n"
        f"🎂 Age: {u.age or 'N/A'}\n"
        f"🚻 Gender: {u.gender or 'N/A'}\n"
        f"📍 Location: {loc}\n"
    )
    if show_username and u.username:
        cap += f"🔗 Username: @{u.username}\n"
    return cap

async def ensure_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    tg = update.effective_user
    if tg.username:
        return True

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Open Telegram Settings", url="tg://settings")],
        [InlineKeyboardButton("✅ I have created username", callback_data="start:check_username")],
    ])
    await update.effective_message.reply_text(
        "⚠️ Username not found\n\n"
        "Your Telegram username is required to use this bot.\n"
        "Go to Settings and create a username and then click on the button below.",
        reply_markup=kb
    )
    return False

def canonical_pair(a: int, b: int) -> Tuple[int, int]:
    return (a, b) if a < b else (b, a)

def build_find_candidates(current_user: User, gender_filter: str) -> List[int]:
    """Returns list of telegram_ids for matching browse, filtered and excluding blocked/requested/matched."""
    session = db_session()
    try:
        # Exclusions
        blocked = session.query(BlockedProfile.blocked_id).filter_by(blocker_id=current_user.telegram_id).all()
        blocked_ids = {x[0] for x in blocked}

        # Already requested (sent by current user)
        sent_req = session.query(MatchRequest.target_id).filter(
            MatchRequest.requester_id == current_user.telegram_id,
            MatchRequest.status.in_(["Pending", "Accepted"])
        ).all()
        sent_req_ids = {x[0] for x in sent_req}

        # Received pending requests from others (so we don't show them again)
        recv_req = session.query(MatchRequest.requester_id).filter(
            MatchRequest.target_id == current_user.telegram_id,
            MatchRequest.status == "Pending"
        ).all()
        recv_req_ids = {x[0] for x in recv_req}

        # Already matched
        matches = session.query(Match).filter(
            or_(Match.user1_id == current_user.telegram_id, Match.user2_id == current_user.telegram_id)
        ).all()
        matched_ids = set()
        for m in matches:
            matched_ids.add(m.user2_id if m.user1_id == current_user.telegram_id else m.user1_id)

        excluded = blocked_ids | sent_req_ids | recv_req_ids | matched_ids | {current_user.telegram_id}

        q = session.query(User).filter(User.is_registered == True).filter(User.telegram_id.notin_(excluded))

        if gender_filter == "Male":
            q = q.filter(User.gender == "Male")
        elif gender_filter == "Female":
            q = q.filter(User.gender == "Female")
        elif gender_filter == "Other":
            q = q.filter(User.gender == "Other")
        else:
            # Any
            pass

        users = q.all()

        # Sort by "nearby" (simple: abs lat + abs lon difference if available)
        if current_user.latitude is not None and current_user.longitude is not None:
            def dist_key(x: User):
                if x.latitude is None or x.longitude is None:
                    return 10**9
                return abs(x.latitude - current_user.latitude) + abs(x.longitude - current_user.longitude)
            users.sort(key=dist_key)

        return [u.telegram_id for u in users]
    finally:
        session.close()

async def send_match_card(chat_id: int, context: ContextTypes.DEFAULT_TYPE, target: User):
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Like ❤️", callback_data=f"fm:like:{target.telegram_id}"),
            InlineKeyboardButton("Skip ➡️", callback_data="fm:skip"),
        ],
        [
            InlineKeyboardButton("Dislike 👎", callback_data=f"fm:dislike:{target.telegram_id}"),
            InlineKeyboardButton("Report 🚩", callback_data=f"fm:report:{target.telegram_id}"),
        ],
        [InlineKeyboardButton("Back", callback_data="back:main")],
    ])

    cap = profile_caption(target, show_username=False)  # other user's username hidden
    if target.profile_picture_file_id:
        await context.bot.send_photo(chat_id=chat_id, photo=target.profile_picture_file_id, caption=cap, reply_markup=kb)
    else:
        await context.bot.send_message(chat_id=chat_id, text=cap, reply_markup=kb)

async def show_next_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    session = db_session()
    try:
        current = session.query(User).filter_by(telegram_id=uid).first()
        if not current or not current.is_registered:
            await update.effective_message.reply_text("Create a profile first /start")
            return ConversationHandler.END

        candidates: List[int] = context.user_data.get("fm_candidates", [])
        pos: int = int(context.user_data.get("fm_pos", 0))

        if pos >= len(candidates):
            await update.effective_message.reply_text(
                "No more profiles found yet. Try later or change filters",
                reply_markup=main_menu_kb()
            )
            return ConversationHandler.END

        target_id = candidates[pos]
        context.user_data["fm_pos"] = pos + 1

        target = session.query(User).filter_by(telegram_id=target_id).first()
        if not target:
            return await show_next_match(update, context)

        await send_match_card(update.effective_chat.id, context, target)
        return ST_FIND_BROWSE
    finally:
        session.close()

# ----------------- START + MENUS -----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = upsert_user_from_telegram(update)

    # Save referral param (count will happen only when referred user completes profile)
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                ref_id = int(arg.split("_", 1)[1])
            except Exception:
                ref_id = 0

            if ref_id and ref_id != user.telegram_id:
                session = db_session()
                try:
                    u = session.query(User).filter_by(telegram_id=user.telegram_id).first()
                    if u and not u.referred_by_id:
                        # only set once
                        ref = session.query(User).filter_by(telegram_id=ref_id).first()
                        if ref:
                            u.referred_by_id = ref_id
                            session.commit()
                finally:
                    session.close()

    if user.is_registered:
        await send_main_menu(update, context, "✅ Welcome back! Main Menu:")
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Create profile", callback_data="start:create")],
        [InlineKeyboardButton("Help", callback_data="start:help")],
        [InlineKeyboardButton("Privacy and policy", callback_data="start:privacy")],
    ])
    await update.effective_message.reply_text(
        "Welcome to Trio Connect Bot.\n"
        "This bot matches and connects users.\n\n"
        "Select the option from below 😊:",
        reply_markup=kb
    )

async def cb_start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "start:help":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="start:back")]])
        await q.edit_message_text(
            "Help:\n"
            "1) Create Profile (age, gender, location, photo)\n"
            "2) Find Match (Male/Female/Any)\n"
            "3) Like -> choose purpose -> Request goes to other user\n"
            "4) Requests -> Accept/Reject\n"
            "5) Username unlock (7 Telegram Stars⭐) or Referral free unlock after match is accepted\n"
            "6) You can delete your profile by clicking /delete",
            reply_markup=kb
        )
        return

    if data == "start:privacy":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="start:back")]])
        await q.edit_message_text(
            "Privacy & Policy:\n"
            "- We store Telegram ID, username, age, gender, location, photo for matching.\n"
            "- Your username will not be visible to others until unlocked.\n"
            "- You can delete your data with /delete\n",
            reply_markup=kb
        )
        return

    if data == "start:back":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Create profile", callback_data="start:create")],
            [InlineKeyboardButton("Help", callback_data="start:help")],
            [InlineKeyboardButton("Privacy and policy", callback_data="start:privacy")],
        ])
        await q.edit_message_text("Welcome to Trio Connect Bot.\nSelect an option from below:", reply_markup=kb)
        return

# ----------------- CREATE PROFILE -----------------
async def cb_create_profile_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    upsert_user_from_telegram(update)

    if not await ensure_username(update, context):
        return ConversationHandler.END

    await q.message.reply_text("Please enter your age (18-99):", reply_markup=ReplyKeyboardRemove())
    return ST_CREATE_AGE

async def cb_check_username_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    upsert_user_from_telegram(update)

    if not await ensure_username(update, context):
        return ConversationHandler.END

    await q.message.reply_text("✅ Username found. Now enter your age (18-99):", reply_markup=ReplyKeyboardRemove())
    return ST_CREATE_AGE

async def st_create_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.effective_message.text.strip())
    except Exception:
        await update.effective_message.reply_text("Send in age number (18-99):")
        return ST_CREATE_AGE

    if age < 18 or age > 99:
        await update.effective_message.reply_text("Age must be between 18 and 99. Send again:")
        return ST_CREATE_AGE

    context.user_data["age"] = age

    kb = ReplyKeyboardMarkup([["Male", "Female", "Other"]], resize_keyboard=True, one_time_keyboard=True)
    await update.effective_message.reply_text("Select gender:", reply_markup=kb)
    return ST_CREATE_GENDER

async def st_create_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gender = (update.effective_message.text or "").strip()
    if gender not in ["Male", "Female", "Other"]:
        kb = ReplyKeyboardMarkup([["Male", "Female", "Other"]], resize_keyboard=True, one_time_keyboard=True)
        await update.effective_message.reply_text("Choose from the buttons:", reply_markup=kb)
        return ST_CREATE_GENDER

    context.user_data["gender"] = gender

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Share Location 📍", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.effective_message.reply_text(
        "Note: Don't worry, we only show the name of your country and city, region or state, we don't show your exact location. Location is required for Nearby Match. Share your location using the button below:",
        reply_markup=kb
    )
    return ST_CREATE_LOCATION

async def st_create_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message.location:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Share Location 📍", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await update.effective_message.reply_text("Use the button to share your location:", reply_markup=kb)
        return ST_CREATE_LOCATION

    lat = update.effective_message.location.latitude
    lon = update.effective_message.location.longitude

    city, country = None, None
    try:
        loc = geolocator.reverse((lat, lon), language="en", timeout=10)
        if loc and loc.raw and "address" in loc.raw:
            addr = loc.raw["address"]
            city = (
                addr.get("city") or addr.get("town") or addr.get("village") or
                addr.get("county") or addr.get("state") or addr.get("region")
            )
            country = addr.get("country")
    except (GeocoderTimedOut, GeocoderUnavailable):
        pass
    except Exception as e:
        logger.warning("Reverse geocode error: %s", e)

    context.user_data["lat"] = lat
    context.user_data["lon"] = lon
    context.user_data["city"] = city or "Nearby Area"
    context.user_data["country"] = country or "Unknown"

    await update.effective_message.reply_text(
        f"✅ Location saved: {context.user_data['city']}, {context.user_data['country']}\n"
        "Now upload your profile picture:",
        reply_markup=ReplyKeyboardRemove()
    )
    return ST_CREATE_PHOTO

async def st_create_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message.photo:
        await update.effective_message.reply_text("Upload Photo (profile picture):")
        return ST_CREATE_PHOTO

    file_id = update.effective_message.photo[-1].file_id

    session = db_session()
    try:
        user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if not user:
            await update.effective_message.reply_text("Error. /start again.")
            return ConversationHandler.END

        user.age = context.user_data.get("age")
        user.gender = context.user_data.get("gender")
        user.latitude = context.user_data.get("lat")
        user.longitude = context.user_data.get("lon")
        user.city = context.user_data.get("city")
        user.country = context.user_data.get("country")
        user.profile_picture_file_id = file_id
        user.is_registered = True
        user.referral_counted = user.referral_counted or False

        # Successful referral counting happens HERE (when profile completed)
        if user.referred_by_id and not user.referral_counted:
            ref = session.query(User).filter_by(telegram_id=user.referred_by_id).first()
            if ref:
                ref.referral_count += 1
                # every 3 successful referrals -> +1 free unlock
                if ref.referral_count % 3 == 0:
                    ref.free_unlocks += 1
                user.referral_counted = True

        session.commit()
    finally:
        session.close()

    await update.effective_message.reply_text("✅ Profile creation done!", reply_markup=main_menu_kb())
    return ConversationHandler.END

# ----------------- VIEW PROFILE -----------------
async def menu_view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user or not user.is_registered:
        await update.effective_message.reply_text("Create a profile first: /start")
        return

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="back:main")]])
    cap = profile_caption(user, show_username=True)

    if user.profile_picture_file_id:
        await update.effective_message.reply_photo(photo=user.profile_picture_file_id, caption=cap, reply_markup=kb)
    else:
        await update.effective_message.reply_text(cap, reply_markup=kb)

# ----------------- EDIT PROFILE -----------------
async def menu_edit_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = get_user(update.effective_user.id)
    if not user or not user.is_registered:
        await update.effective_message.reply_text("Create a profile first: /start")
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Edit Photo", callback_data="edit:photo")],
        [InlineKeyboardButton("Edit Age", callback_data="edit:age")],
        [InlineKeyboardButton("Edit Gender", callback_data="edit:gender")],
        [InlineKeyboardButton("Edit Location", callback_data="edit:location")],
        [InlineKeyboardButton("Back", callback_data="back:main")],
    ])
    await update.effective_message.reply_text("What do you want to edit?", reply_markup=kb)
    return ST_EDIT_MENU

async def cb_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    data = q.data
    if data == "edit:age":
        await q.message.reply_text("Enter new age (18-99):", reply_markup=ReplyKeyboardRemove())
        return ST_EDIT_AGE
    if data == "edit:gender":
        kb = ReplyKeyboardMarkup([["Male", "Female", "Other"]], resize_keyboard=True, one_time_keyboard=True)
        await q.message.reply_text("Select new gender:", reply_markup=kb)
        return ST_EDIT_GENDER
    if data == "edit:location":
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Share Location 📍", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await q.message.reply_text("Share new location:", reply_markup=kb)
        return ST_EDIT_LOCATION
    if data == "edit:photo":
        await q.message.reply_text("Upload new profile photo:", reply_markup=ReplyKeyboardRemove())
        return ST_EDIT_PHOTO

    return ST_EDIT_MENU

async def st_edit_age(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        age = int(update.effective_message.text.strip())
    except Exception:
        await update.effective_message.reply_text("Send Number (18-99):")
        return ST_EDIT_AGE

    if age < 18 or age > 99:
        await update.effective_message.reply_text("Send between 18-99:")
        return ST_EDIT_AGE

    session = db_session()
    try:
        user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if user:
            user.age = age
            session.commit()
    finally:
        session.close()

    await update.effective_message.reply_text("✅ Age updated!", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def st_edit_gender(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    gender = (update.effective_message.text or "").strip()
    if gender not in ["Male", "Female", "Other"]:
        kb = ReplyKeyboardMarkup([["Male", "Female", "Other"]], resize_keyboard=True, one_time_keyboard=True)
        await update.effective_message.reply_text("Choose from Buttons:", reply_markup=kb)
        return ST_EDIT_GENDER

    session = db_session()
    try:
        user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if user:
            user.gender = gender
            session.commit()
    finally:
        session.close()

    await update.effective_message.reply_text("✅ Gender updated!", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def st_edit_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message.location:
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("Share Location 📍", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await update.effective_message.reply_text("Share location with button:", reply_markup=kb)
        return ST_EDIT_LOCATION

    lat = update.effective_message.location.latitude
    lon = update.effective_message.location.longitude

    city, country = None, None
    try:
        loc = geolocator.reverse((lat, lon), language="en", timeout=10)
        if loc and loc.raw and "address" in loc.raw:
            addr = loc.raw["address"]
            city = (
                addr.get("city") or addr.get("town") or addr.get("village") or
                addr.get("county") or addr.get("state") or addr.get("region")
            )
            country = addr.get("country")
    except Exception:
        pass

    session = db_session()
    try:
        user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if user:
            user.latitude = lat
            user.longitude = lon
            user.city = city or "Nearby Area"
            user.country = country or "Unknown"
            session.commit()
    finally:
        session.close()

    await update.effective_message.reply_text("✅ Location updated!", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def st_edit_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_message.photo:
        await update.effective_message.reply_text("Upload Photo:")
        return ST_EDIT_PHOTO

    file_id = update.effective_message.photo[-1].file_id
    session = db_session()
    try:
        user = session.query(User).filter_by(telegram_id=update.effective_user.id).first()
        if user:
            user.profile_picture_file_id = file_id
            session.commit()
    finally:
        session.close()

    await update.effective_message.reply_text("✅ Photo updated!", reply_markup=main_menu_kb())
    return ConversationHandler.END

# ----------------- FIND MATCH -----------------
async def menu_find_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = get_user(update.effective_user.id)
    if not user or not user.is_registered:
        await update.effective_message.reply_text("Create a profile first: /start")
        return ConversationHandler.END

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Male", callback_data="fm:filter:Male")],
        [InlineKeyboardButton("Female", callback_data="fm:filter:Female")],
        [InlineKeyboardButton("Any", callback_data="fm:filter:Any")],
        [InlineKeyboardButton("Back", callback_data="back:main")],
    ])
    await update.effective_message.reply_text("Choose filter:", reply_markup=kb)
    return ST_FIND_FILTER

async def cb_find_filter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    parts = q.data.split(":")
    gender_filter = parts[2] if len(parts) > 2 else "Any"

    session = db_session()
    try:
        current = session.query(User).filter_by(telegram_id=q.from_user.id).first()
        if not current or not current.is_registered:
            await q.message.reply_text("Create a profile first: /start")
            return ConversationHandler.END
    finally:
        session.close()

    candidates = build_find_candidates(get_user(q.from_user.id), gender_filter)
    context.user_data["fm_candidates"] = candidates
    context.user_data["fm_pos"] = 0
    context.user_data["fm_filter"] = gender_filter

    await q.message.reply_text(f"Filter set: {gender_filter}. Profiles loading...")
    return await show_next_match(update, context)

async def cb_find_browse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    data = q.data
    uid = q.from_user.id

    if data == "fm:skip":
        return await show_next_match(update, context)

    if data.startswith("fm:dislike:"):
        target_id = int(data.split(":")[2])
        session = db_session()
        try:
            # block
            exists = session.query(BlockedProfile).filter_by(blocker_id=uid, blocked_id=target_id).first()
            if not exists:
                session.add(BlockedProfile(blocker_id=uid, blocked_id=target_id))
                session.commit()
        finally:
            session.close()
        await q.message.reply_text("👎 Disliked. Next profile:")
        return await show_next_match(update, context)

    if data.startswith("fm:like:"):
        target_id = int(data.split(":")[2])
        context.user_data["like_target_id"] = target_id

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Friendship", callback_data="fm:purpose:Friendship")],
            [InlineKeyboardButton("Relationship", callback_data="fm:purpose:Relationship")],
            [InlineKeyboardButton("Other purpose", callback_data="fm:purpose:Other")],
            [InlineKeyboardButton("Cancel", callback_data="fm:purpose:cancel")],
        ])
        await q.message.reply_text("Liked it ✅ Now choose your purpose:", reply_markup=kb)
        return ST_FIND_PURPOSE

    if data.startswith("fm:report:"):
        target_id = int(data.split(":")[2])
        context.user_data["report_target_id"] = target_id

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Fake profile", callback_data="fm:report_reason:Fake profile")],
            [InlineKeyboardButton("Spam", callback_data="fm:report_reason:Spam")],
            [InlineKeyboardButton("Harassment", callback_data="fm:report_reason:Harassment")],
            [InlineKeyboardButton("Nudity/Adult", callback_data="fm:report_reason:Nudity/Adult")],
            [InlineKeyboardButton("Other", callback_data="fm:report_reason:Other")],
            [InlineKeyboardButton("Cancel", callback_data="fm:report_reason:cancel")],
        ])
        await q.message.reply_text("Select Report reason:", reply_markup=kb)
        return ST_FIND_REPORT_REASON

    return ST_FIND_BROWSE

async def cb_find_purpose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    purpose = q.data.split(":", 2)[2]
    if purpose == "cancel":
        await q.message.reply_text("Canceled. Next profile:")
        return await show_next_match(update, context)

    requester_id = q.from_user.id
    target_id = context.user_data.get("like_target_id")
    if not target_id:
        await q.message.reply_text("Error. Try again.")
        return await show_next_match(update, context)

    session = db_session()
    try:
        # Already exists request?
        existing = session.query(MatchRequest).filter_by(requester_id=requester_id, target_id=target_id).first()
        if existing and existing.status in ["Pending", "Accepted"]:
            await q.message.reply_text("You have already sent a request. Next profileile:")
            return await show_next_match(update, context)

        # Create request
        req = MatchRequest(requester_id=requester_id, target_id=target_id, purpose=purpose, status="Pending")
        session.add(req)
        session.commit()

        requester = session.query(User).filter_by(telegram_id=requester_id).first()
        target = session.query(User).filter_by(telegram_id=target_id).first()

        # notify target
        if target:
            await context.bot.send_message(
                chat_id=target.telegram_id,
                text=(
                    "🔔 New Request received!\n\n"
                    f"From: {requester.name if requester else requester_id}\n"
                    f"Purpose: {purpose}\n\n"
                    "To view requests Main Menu -> Requests"
                ),
                reply_markup=main_menu_kb()
            )

        await q.message.reply_text("✅ Request sent. Next profile:")
        return await show_next_match(update, context)
    finally:
        session.close()

async def cb_find_report_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    reason = q.data.split(":", 2)[2]
    if reason == "cancel":
        await q.message.reply_text("Report canceled. Next profile:")
        return await show_next_match(update, context)

    if reason == "Other":
        await q.message.reply_text("Write other reason:")
        context.user_data["report_reason_base"] = "Other"
        return ST_FIND_REPORT_TEXT

    # save report directly
    await save_report_and_block(update, context, reason)
    await q.message.reply_text("✅ Report sent. Next profile:")
    return await show_next_match(update, context)

async def st_find_report_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    reason = f"Other: {text}" if text else "Other"
    await save_report_and_block(update, context, reason)
    await update.effective_message.reply_text("✅ Report sent. Next profile:")
    return await show_next_match(update, context)

async def save_report_and_block(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str):
    reporter_id = update.effective_user.id
    reported_id = context.user_data.get("report_target_id")
    if not reported_id:
        return

    session = db_session()
    try:
        session.add(Report(reporter_id=reporter_id, reported_id=reported_id, reason=reason, status="Pending"))
        # also block so it won't appear again
        exists = session.query(BlockedProfile).filter_by(blocker_id=reporter_id, blocked_id=reported_id).first()
        if not exists:
            session.add(BlockedProfile(blocker_id=reporter_id, blocked_id=reported_id))
        session.commit()
    finally:
        session.close()

    # notify admin
    if ADMIN_TELEGRAM_ID:
        await context.bot.send_message(
            chat_id=ADMIN_TELEGRAM_ID,
            text=f"🚩 New Report\nReporter: {reporter_id}\nReported: {reported_id}\nReason: {reason}"
        )

# ----------------- REQUESTS -----------------
async def menu_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session = db_session()
    try:
        reqs = session.query(MatchRequest).filter_by(target_id=uid, status="Pending").order_by(MatchRequest.created_at.desc()).all()
        if not reqs:
            await update.effective_message.reply_text("No pending requests.", reply_markup=main_menu_kb())
            return

        await update.effective_message.reply_text(f"Pending requests: {len(reqs)}")

        for r in reqs:
            sender = session.query(User).filter_by(telegram_id=r.requester_id).first()
            if not sender:
                continue

            cap = profile_caption(sender, show_username=False) + f"🎯 Purpose: {r.purpose}\n"
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Accept ✅", callback_data=f"rq:accept:{r.id}"),
                    InlineKeyboardButton("Reject ❌", callback_data=f"rq:reject:{r.id}"),
                ]
            ])
            if sender.profile_picture_file_id:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=sender.profile_picture_file_id,
                    caption=cap,
                    reply_markup=kb
                )
            else:
                await update.effective_message.reply_text(cap, reply_markup=kb)

    finally:
        session.close()

async def cb_request_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    action = q.data.split(":")[1]
    req_id = int(q.data.split(":")[2])
    uid = q.from_user.id

    session = db_session()
    try:
        req = session.query(MatchRequest).filter_by(id=req_id).first()
        if not req or req.target_id != uid or req.status != "Pending":
            await q.message.reply_text("Invalid/expired request.")
            return

        requester = session.query(User).filter_by(telegram_id=req.requester_id).first()
        target = session.query(User).filter_by(telegram_id=req.target_id).first()
        if not requester or not target:
            await q.message.reply_text("User not found.")
            return

        if action == "reject":
            req.status = "Rejected"
            session.commit()
            await q.message.reply_text("❌ Request rejected.")
            await context.bot.send_message(chat_id=requester.telegram_id, text="Your request was rejected.")
            return

        # accept
        req.status = "Accepted"
        u1, u2 = canonical_pair(requester.telegram_id, target.telegram_id)
        match = session.query(Match).filter_by(user1_id=u1, user2_id=u2).first()
        if not match:
            match = Match(user1_id=u1, user2_id=u2, purpose=req.purpose)
            session.add(match)
            session.commit()
            session.refresh(match)
        else:
            session.commit()

        await q.message.reply_text("✅ Match successful!")

        # notify both sides with unlock options
        await notify_match_created(context, match.id, requester.telegram_id, target.telegram_id)

    finally:
        session.close()

async def notify_match_created(context: ContextTypes.DEFAULT_TYPE, match_id: int, a: int, b: int):
    # send to user a
    for uid in [a, b]:
        u = get_user(uid)
        if not u:
            continue

        kb_rows = [[InlineKeyboardButton("Unlock Username (7 Stars ⭐)", callback_data=f"m:pay:{match_id}")]]
        if u.free_unlocks and u.free_unlocks > 0:
            kb_rows.insert(0, [InlineKeyboardButton("Use Free Unlock 🎁", callback_data=f"m:free:{match_id}")])
        kb_rows.append([InlineKeyboardButton("Later", callback_data="back:main")])

        kb = InlineKeyboardMarkup(kb_rows)
        await context.bot.send_message(
            chat_id=uid,
            text="🎉 Match successful!\nUsername unlocked to view:",
            reply_markup=kb
        )

def other_user_in_match(match: Match, current_id: int) -> Optional[int]:
    if match.user1_id == current_id:
        return match.user2_id
    if match.user2_id == current_id:
        return match.user1_id
    return None

def is_unlocked_for_user(match: Match, uid: int) -> bool:
    if match.user1_id == uid:
        return bool(match.user1_unlocked)
    if match.user2_id == uid:
        return bool(match.user2_unlocked)
    return False

def set_unlocked_for_user(match: Match, uid: int):
    if match.user1_id == uid:
        match.user1_unlocked = True
    elif match.user2_id == uid:
        match.user2_unlocked = True

async def cb_match_unlock_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    match_id = int(q.data.split(":")[2])
    uid = q.from_user.id

    session = db_session()
    try:
        match = session.query(Match).filter_by(id=match_id).first()
        user = session.query(User).filter_by(telegram_id=uid).first()
        if not match or not user:
            await q.message.reply_text("Invalid match.")
            return

        other_id = other_user_in_match(match, uid)
        if not other_id:
            await q.message.reply_text("You are not in this match.")
            return

        if is_unlocked_for_user(match, uid):
            other = session.query(User).filter_by(telegram_id=other_id).first()
            await q.message.reply_text(f"Already unlocked: @{other.username}" if other and other.username else "Already unlocked.")
            return

        if not user.free_unlocks or user.free_unlocks < 1:
            await q.message.reply_text("No free unlocks available.")
            return

        other = session.query(User).filter_by(telegram_id=other_id).first()
        if not other or not other.username:
            await q.message.reply_text("Other user's username not available.")
            return

        user.free_unlocks -= 1
        set_unlocked_for_user(match, uid)
        session.commit()

        await q.message.reply_text(
            f"🎁 Free unlock used!\nUsername: @{other.username}\nChat: https://t.me/{other.username}",
            disable_web_page_preview=True
        )
    finally:
        session.close()

async def cb_match_unlock_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    match_id = int(q.data.split(":")[2])
    uid = q.from_user.id

    session = db_session()
    try:
        match = session.query(Match).filter_by(id=match_id).first()
        if not match:
            await q.message.reply_text("Invalid match.")
            return

        other_id = other_user_in_match(match, uid)
        if not other_id:
            await q.message.reply_text("You are not in this match.")
            return

        if is_unlocked_for_user(match, uid):
            await q.message.reply_text("Already unlocked.")
            return
    finally:
        session.close()

    # Telegram Stars invoice (currency XTR, provider_token must be empty string)
    prices = [LabeledPrice("Unlock username", 7)]
    payload = f"unlock:{match_id}:{uid}"

    await context.bot.send_invoice(
        chat_id=uid,
        title="Unlock Username",
        description="Unlock your match's Telegram username",
        payload=payload,
        provider_token="",        # IMPORTANT for Telegram Stars
        currency="XTR",
        prices=prices,
    )

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    # Always approve (you can add validation)
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sp = update.effective_message.successful_payment
    payload = sp.invoice_payload  # "unlock:match_id:uid"

    if not payload.startswith("unlock:"):
        return

    try:
        _, match_id_str, uid_str = payload.split(":", 2)
        match_id = int(match_id_str)
        uid = int(uid_str)
    except Exception:
        return

    # Validate amount/currency
    if sp.currency != "XTR" or sp.total_amount != 7:
        await update.effective_message.reply_text("Payment received but invalid amount/currency.")
        return

    session = db_session()
    try:
        match = session.query(Match).filter_by(id=match_id).first()
        if not match:
            await update.effective_message.reply_text("Match not found.")
            return

        if uid != update.effective_user.id:
            await update.effective_message.reply_text("Payment user mismatch.")
            return

        other_id = other_user_in_match(match, uid)
        if not other_id:
            await update.effective_message.reply_text("You are not in this match.")
            return

        if is_unlocked_for_user(match, uid):
            await update.effective_message.reply_text("Already unlocked.")
            return

        other = session.query(User).filter_by(telegram_id=other_id).first()
        if not other or not other.username:
            await update.effective_message.reply_text("Other user's username not available.")
            return

        set_unlocked_for_user(match, uid)
        session.commit()

        await update.effective_message.reply_text(
            f"✅ Payment success! Username unlocked:\n@{other.username}\nChat: https://t.me/{other.username}",
            disable_web_page_preview=True
        )
    finally:
        session.close()

# ----------------- REFERRAL MENU -----------------
async def menu_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if not user or not user.is_registered:
        await update.effective_message.reply_text("Create a profile first: /start")
        return

    await update.effective_message.reply_text(
        "Refer system:\n"
        "✅ 3 successful referrals => 1 free username unlock\n\n"
        f"Your link:\n{referral_link(user)}\n\n"
        f"Successful referrals: {user.referral_count}\n"
        f"Free unlocks available: {user.free_unlocks}",
        disable_web_page_preview=True,
        reply_markup=main_menu_kb()
    )

# ----------------- DELETE PROFILE (/delete) -----------------
async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Yes, delete my profile", callback_data="del:yes")],
        [InlineKeyboardButton("No, cancel", callback_data="del:no")],
    ])
    await update.effective_message.reply_text(
        "Are you sure you want to delete your profile? It won't be restored 😔",
        reply_markup=kb
    )
    return ST_DELETE_CONFIRM

async def cb_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query
    await q.answer()

    if q.data == "del:no":
        await q.message.reply_text("Canceled.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    uid = q.from_user.id
    session = db_session()
    try:
        # remove relations
        session.query(BlockedProfile).filter(or_(BlockedProfile.blocker_id == uid, BlockedProfile.blocked_id == uid)).delete(synchronize_session=False)
        session.query(MatchRequest).filter(or_(MatchRequest.requester_id == uid, MatchRequest.target_id == uid)).delete(synchronize_session=False)
        session.query(Match).filter(or_(Match.user1_id == uid, Match.user2_id == uid)).delete(synchronize_session=False)
        session.query(Report).filter(or_(Report.reporter_id == uid, Report.reported_id == uid)).delete(synchronize_session=False)
        session.query(User).filter_by(telegram_id=uid).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()

    await q.message.reply_text("✅ Profile deleted successfully. /start anytime.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ----------------- BACK TO MAIN -----------------
async def cb_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("Main Menu:", reply_markup=main_menu_kb())

# ----------------- ADMIN PANEL -----------------
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    kb = ReplyKeyboardMarkup(
        [
            ["Statics", "Broadcast"],
            ["Reports", "View user"],
            ["Delete user"],
        ],
        resize_keyboard=True
    )
    await update.effective_message.reply_text("Admin Panel:", reply_markup=kb)

async def admin_statics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    session = db_session()
    try:
        total = session.query(User).count()
        registered = session.query(User).filter_by(is_registered=True).count()
        pending_reports = session.query(Report).filter_by(status="Pending").count()
        matches = session.query(Match).count()
        pending_requests = session.query(MatchRequest).filter_by(status="Pending").count()
    finally:
        session.close()

    await update.effective_message.reply_text(
        f"Statics:\n"
        f"Total users: {total}\n"
        f"Registered users: {registered}\n"
        f"Pending requests: {pending_requests}\n"
        f"Matches: {matches}\n"
        f"Pending reports: {pending_reports}"
    )

# --- Admin Broadcast (copy any message type) ---
async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    kb = ReplyKeyboardMarkup(
        [["All", "Male", "Female"], ["Other", "Cancel"]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.effective_message.reply_text("Broadcast audience choose करो:", reply_markup=kb)
    return ST_ADMIN_BC_AUDIENCE

async def admin_broadcast_audience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    aud = (update.effective_message.text or "").strip()
    if aud == "Cancel":
        await update.effective_message.reply_text("Broadcast canceled.", reply_markup=ReplyKeyboardRemove())
        return ConversationHandler.END

    if aud not in ["All", "Male", "Female", "Other"]:
        await update.effective_message.reply_text("Select a valid option All/Male/Female/Other or Cancel")
        return ST_ADMIN_BC_AUDIENCE

    context.user_data["bc_aud"] = aud
    await update.effective_message.reply_text(
        "Now whatever message you send (text/photo/video/audio/document) will be broadcast.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ST_ADMIN_BC_SEND

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    aud = context.user_data.get("bc_aud", "All")
    src_chat_id = update.effective_chat.id
    src_msg_id = update.effective_message.message_id

    session = db_session()
    try:
        q = session.query(User).filter(User.is_registered == True)
        if aud in ["Male", "Female", "Other"]:
            q = q.filter(User.gender == aud)
        targets = [u.telegram_id for u in q.all()]
    finally:
        session.close()

    sent = 0
    failed = 0
    for tid in targets:
        try:
            await context.bot.copy_message(chat_id=tid, from_chat_id=src_chat_id, message_id=src_msg_id)
            sent += 1
        except Exception:
            failed += 1

    await update.effective_message.reply_text(f"Broadcast done.\nSent: {sent}\nFailed: {failed}")
    return ConversationHandler.END

# --- Admin Reports ---
async def admin_reports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    session = db_session()
    try:
        reps = session.query(Report).filter_by(status="Pending").order_by(Report.created_at.desc()).limit(20).all()
        if not reps:
            await update.effective_message.reply_text("No pending reports.")
            return

        for r in reps:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Mark Reviewed", callback_data=f"admin:rep_review:{r.id}")]
            ])
            await update.effective_message.reply_text(
                f"Report ID: {r.id}\nReporter: {r.reporter_id}\nReported: {r.reported_id}\nReason: {r.reason}\nTime: {r.created_at}",
                reply_markup=kb
            )
    finally:
        session.close()

async def cb_admin_report_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return

    rid = int(q.data.split(":")[2])
    session = db_session()
    try:
        r = session.query(Report).filter_by(id=rid).first()
        if r:
            r.status = "Reviewed"
            session.commit()
            await q.message.reply_text(f"✅ Report {rid} marked reviewed.")
    finally:
        session.close()

# --- Admin View User ---
async def admin_view_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.effective_message.reply_text("Enter Telegram ID or @username:")
    return ST_ADMIN_VIEW_USER

async def admin_view_user_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    ident = (update.effective_message.text or "").strip()
    session = db_session()
    try:
        u = None
        if ident.startswith("@"):
            ident = ident[1:]
        try:
            tid = int(ident)
            u = session.query(User).filter_by(telegram_id=tid).first()
        except Exception:
            u = session.query(User).filter_by(username=ident).first()

        if not u:
            await update.effective_message.reply_text("User not found.")
            return ConversationHandler.END

        cap = (
            f"User Profile (Admin View)\n"
            f"Name: {u.name}\n"
            f"Telegram ID: {u.telegram_id}\n"
            f"Username: @{u.username if u.username else 'N/A'}\n"
            f"Age: {u.age}\nGender: {u.gender}\n"
            f"Location: {u.city}, {u.country}\n"
            f"Registered: {u.is_registered}\n"
            f"Referred by: {u.referred_by_id}\n"
            f"Referral count: {u.referral_count}\n"
            f"Free unlocks: {u.free_unlocks}\n"
        )
        if u.profile_picture_file_id:
            await update.effective_message.reply_photo(photo=u.profile_picture_file_id, caption=cap)
        else:
            await update.effective_message.reply_text(cap)
    finally:
        session.close()

    return ConversationHandler.END

# --- Admin Delete User ---
async def admin_delete_user_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    await update.effective_message.reply_text("Enter Telegram ID or @username to delete:")
    return ST_ADMIN_DELETE_USER

async def admin_delete_user_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    ident = (update.effective_message.text or "").strip()
    if ident.startswith("@"):
        ident = ident[1:]

    session = db_session()
    try:
        u = None
        try:
            tid = int(ident)
            u = session.query(User).filter_by(telegram_id=tid).first()
        except Exception:
            u = session.query(User).filter_by(username=ident).first()

        if not u:
            await update.effective_message.reply_text("User not found.")
            return ConversationHandler.END

        uid = u.telegram_id
        session.query(BlockedProfile).filter(or_(BlockedProfile.blocker_id == uid, BlockedProfile.blocked_id == uid)).delete(synchronize_session=False)
        session.query(MatchRequest).filter(or_(MatchRequest.requester_id == uid, MatchRequest.target_id == uid)).delete(synchronize_session=False)
        session.query(Match).filter(or_(Match.user1_id == uid, Match.user2_id == uid)).delete(synchronize_session=False)
        session.query(Report).filter(or_(Report.reporter_id == uid, Report.reported_id == uid)).delete(synchronize_session=False)
        session.query(User).filter_by(telegram_id=uid).delete(synchronize_session=False)
        session.commit()

        await update.effective_message.reply_text(f"✅ Deleted user: {uid}")
    finally:
        session.close()

    return ConversationHandler.END

# ----------------- FALLBACK -----------------
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user and user.is_registered:
        await update.effective_message.reply_text("Choose the option from the menu.", reply_markup=main_menu_kb())
    else:
        await update.effective_message.reply_text("Type /start")

# ----------------- MAIN -----------------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Start menu callbacks
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_start_menu, pattern=r"^start:(help|privacy|back)$"))

    # Create profile conversation
    create_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(cb_create_profile_entry, pattern=r"^start:create$"),
            CallbackQueryHandler(cb_check_username_and_continue, pattern=r"^start:check_username$"),
        ],
        states={
            ST_CREATE_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_create_age)],
            ST_CREATE_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_create_gender)],
            ST_CREATE_LOCATION: [MessageHandler(filters.LOCATION, st_create_location)],
            ST_CREATE_PHOTO: [MessageHandler(filters.PHOTO, st_create_photo)],
        },
        fallbacks=[],
        allow_reentry=True
    )
    app.add_handler(create_conv)

    # Edit profile conversation
    edit_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^Edit Profile$"), menu_edit_profile)],
        states={
            ST_EDIT_MENU: [CallbackQueryHandler(cb_edit_menu, pattern=r"^edit:(photo|age|gender|location)$")],
            ST_EDIT_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_edit_age)],
            ST_EDIT_GENDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_edit_gender)],
            ST_EDIT_LOCATION: [MessageHandler(filters.LOCATION, st_edit_location)],
            ST_EDIT_PHOTO: [MessageHandler(filters.PHOTO, st_edit_photo)],
        },
        fallbacks=[CallbackQueryHandler(cb_back_main, pattern=r"^back:main$")],
        allow_reentry=True
    )
    app.add_handler(edit_conv)

    # Find match conversation
    find_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^Find Match$"), menu_find_match)],
        states={
            ST_FIND_FILTER: [CallbackQueryHandler(cb_find_filter, pattern=r"^fm:filter:(Male|Female|Any)$")],
            ST_FIND_BROWSE: [CallbackQueryHandler(cb_find_browse, pattern=r"^fm:(skip|like:\d+|dislike:\d+|report:\d+)$")],
            ST_FIND_PURPOSE: [CallbackQueryHandler(cb_find_purpose, pattern=r"^fm:purpose:(Friendship|Relationship|Other|cancel)$")],
            ST_FIND_REPORT_REASON: [CallbackQueryHandler(cb_find_report_reason, pattern=r"^fm:report_reason:(.+)$")],
            ST_FIND_REPORT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, st_find_report_text)],
        },
        fallbacks=[CallbackQueryHandler(cb_back_main, pattern=r"^back:main$")],
        allow_reentry=True
    )
    app.add_handler(find_conv)

    # Main menu buttons
    app.add_handler(MessageHandler(filters.Regex(r"^View Your Profile$"), menu_view_profile))
    app.add_handler(MessageHandler(filters.Regex(r"^Requests$"), menu_requests))
    app.add_handler(MessageHandler(filters.Regex(r"^Refer 3 users to unlock 1 username$"), menu_referral))

    # Request accept/reject callback
    app.add_handler(CallbackQueryHandler(cb_request_action, pattern=r"^rq:(accept|reject):\d+$"))

    # Unlock username callbacks
    app.add_handler(CallbackQueryHandler(cb_match_unlock_free, pattern=r"^m:free:\d+$"))
    app.add_handler(CallbackQueryHandler(cb_match_unlock_pay, pattern=r"^m:pay:\d+$"))

    # Payments handlers (Stars)
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # /delete conversation
    delete_conv = ConversationHandler(
        entry_points=[CommandHandler("delete", cmd_delete)],
        states={
            ST_DELETE_CONFIRM: [CallbackQueryHandler(cb_delete_confirm, pattern=r"^del:(yes|no)$")]
        },
        fallbacks=[]
    )
    app.add_handler(delete_conv)

    # Back to main
    app.add_handler(CallbackQueryHandler(cb_back_main, pattern=r"^back:main$"))

    # Admin
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(MessageHandler(filters.Regex(r"^Statics$"), admin_statics))
    app.add_handler(MessageHandler(filters.Regex(r"^Reports$"), admin_reports))
    app.add_handler(CallbackQueryHandler(cb_admin_report_review, pattern=r"^admin:rep_review:\d+$"))

    admin_bc_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^Broadcast$"), admin_broadcast_start)],
        states={
            ST_ADMIN_BC_AUDIENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_audience)],
            ST_ADMIN_BC_SEND: [MessageHandler(~filters.COMMAND, admin_broadcast_send)],
        },
        fallbacks=[],
        allow_reentry=True
    )
    app.add_handler(admin_bc_conv)

    admin_view_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^View user$"), admin_view_user_start)],
        states={ST_ADMIN_VIEW_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_view_user_do)]},
        fallbacks=[],
        allow_reentry=True
    )
    app.add_handler(admin_view_conv)

    admin_del_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^Delete user$"), admin_delete_user_start)],
        states={ST_ADMIN_DELETE_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_delete_user_do)]},
        fallbacks=[],
        allow_reentry=True
    )
    app.add_handler(admin_del_conv)

    # Unknown
    app.add_handler(MessageHandler(filters.ALL, unknown))

    logger.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()