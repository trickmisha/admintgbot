import os
import asyncio
import logging
import signal
from datetime import datetime, timedelta, UTC
from collections.abc import Awaitable, Callable
from typing import TypeVar

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatJoinRequest,
    ChatMemberUpdated,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputPaidMediaPhoto,
    WebAppInfo,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from sqlalchemy import Column, BigInteger, String, Boolean, Integer, DateTime, ForeignKey, text, select, UniqueConstraint
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

load_dotenv()


def _require_env(name: str) -> str:
    v = os.getenv(name)
    if v is None or v == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return v


BOT_TOKEN = _require_env("BOT_TOKEN")
ADMIN_ID = int(_require_env("ADMIN_ID"))
FREE_CHANNEL_ID = int(_require_env("FREE_CHANNEL_ID"))
PAID_CHANNEL_ID = int(_require_env("PAID_CHANNEL_ID"))
PAID_CHANNEL_LINK = _require_env("PAID_CHANNEL_LINK")
DATABASE_URL = _require_env("DATABASE_URL")
MINIAPP_URL = _require_env("MINIAPP_URL")
PORT = int(_require_env("PORT"))

REDIS_URL = os.getenv("REDIS_URL")

# --- Мульти-админ ---
# Чтобы добавить второго админа: в Render → Environment Variables
# добавь ADMIN_IDS=123456789,987654321 (через запятую, без пробелов)
# Если ADMIN_IDS не задан — используется только ADMIN_ID
_admin_ids_raw = os.getenv("ADMIN_IDS", "")
if _admin_ids_raw.strip():
    ADMIN_IDS: frozenset[int] = frozenset(
        int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip().isdigit()
    )
    ADMIN_IDS = ADMIN_IDS | {ADMIN_ID}
else:
    ADMIN_IDS = frozenset({ADMIN_ID})


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


T = TypeVar("T")
_redis_client = None

# Хранилище message_id последних сообщений бота для каждого админа
# { user_id: [msg_id1, msg_id2, ...] }
_admin_last_msgs: dict[int, list[int]] = {}

Base = declarative_base()

_MEMBER_LIKE = frozenset(
    {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    }
)


class User(Base):
    __tablename__ = "users"
    id = Column(BigInteger, primary_key=True)
    username = Column(String)
    joined_at = Column(DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))
    has_gift_claimed = Column(Boolean, default=False)
    total_spent = Column(BigInteger, default=0)
    is_vip = Column(Boolean, default=False)
    blocked = Column(Boolean, default=False)


class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    item_id = Column(Integer)
    amount = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(String)


class ContentQueue(Base):
    __tablename__ = "content_queue"
    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(String)
    media_id = Column(String)
    price = Column(Integer, default=0)
    post_type = Column(String, default="free")
    channel = Column(String, default="free")
    button_text = Column(String)
    button_url = Column(String)
    scheduled_at = Column(DateTime)
    status = Column(String, default="pending")


class DripMessage(Base):
    __tablename__ = "drip_messages"
    __table_args__ = (UniqueConstraint("step", name="uq_drip_messages_step"),)
    id = Column(Integer, primary_key=True, autoincrement=True)
    step = Column(Integer, nullable=False)
    text = Column(String)
    media_file_id = Column(String)
    media_type = Column(String)
    button_text = Column(String)
    button_url = Column(String)
    delay_hours = Column(Integer, default=24)
    is_active = Column(Boolean, default=True)


class DripProgress(Base):
    __tablename__ = "drip_progress"
    user_id = Column(BigInteger, primary_key=True)
    current_step = Column(Integer, default=0)
    next_send_at = Column(DateTime)
    is_active = Column(Boolean, default=True)


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    text = Column(String)
    media_file_id = Column(String)
    target = Column(String)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=lambda: datetime.now(UTC).replace(tzinfo=None))


engine = create_async_engine(
    DATABASE_URL,
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    },
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

if REDIS_URL:
    from redis.asyncio import Redis
    from aiogram.fsm.storage.redis import RedisStorage
    _redis_client = Redis.from_url(REDIS_URL)
    storage = RedisStorage(redis=_redis_client)
else:
    storage = MemoryStorage()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)


# --- FSM States ---
class AdminStates(StatesGroup):
    # Broadcast
    waiting_broadcast_message = State()
    waiting_broadcast_audience = State()


class PostStates(StatesGroup):
    choosing_channel = State()
    waiting_media = State()
    waiting_text = State()
    waiting_button_text = State()
    waiting_button_url = State()
    choosing_time = State()
    waiting_custom_time = State()
    confirming = State()


class DripStates(StatesGroup):
    viewing = State()
    editing_text = State()
    editing_media = State()
    editing_button_text = State()
    editing_button_url = State()
    editing_delay = State()


# --- Helpers ---
async def telegram_with_flood_retry(factory: Callable[[], Awaitable[T]]) -> T:
    while True:
        try:
            return await factory()
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)


async def interruptible_sleep(seconds: float, stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


async def advisory_try_lock_key(session: AsyncSession, key: str) -> bool:
    r = await session.execute(
        text("SELECT pg_try_advisory_lock(abs(hashtext(CAST(:k AS TEXT))))"),
        {"k": key},
    )
    return bool(r.scalar())


async def advisory_unlock_key(session: AsyncSession, key: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_unlock(abs(hashtext(CAST(:k AS TEXT))))"),
        {"k": key},
    )


async def fetch_settings_map(session: AsyncSession) -> dict[str, str]:
    r = await session.execute(select(Setting))
    return {row.key: (row.value or "") for row in r.scalars().all()}


def build_welcome_inline_keyboard(settings: dict[str, str]) -> InlineKeyboardMarkup:
    rows = []
    b1t, b1u = settings.get("button1_text") or "", settings.get("button1_url") or ""
    b2t = settings.get("button2_text") or ""
    b2u = settings.get("paid_channel_link") or ""
    if b1t and b1u:
        rows.append([InlineKeyboardButton(text=b1t, url=b1u)])
    if b2t and b2u:
        rows.append([InlineKeyboardButton(text=b2t, url=b2u)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def seed_default_settings(session: AsyncSession) -> None:
    cid = FREE_CHANNEL_ID
    s = str(cid)
    inner = s[4:] if s.startswith("-100") else str(abs(cid))
    free_link = f"https://t.me/c/{inner}"
    defaults = [
        ("welcome_text", "Missed me? 🫦 I've been thinking about u all day."),
        ("button1_text", "🫦 Enter the Sanctuary"),
        ("button1_url", free_link),
        ("button2_text", "⭐ VIP (Stars)"),
        ("button2_url", ""),
        ("paid_channel_link", PAID_CHANNEL_LINK),
    ]
    for key, val in defaults:
        await session.execute(
            text("INSERT INTO settings (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING"),
            {"k": key, "v": val},
        )
    await session.commit()


def get_admin_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📢 Рассылка")],
        [KeyboardButton(text="📝 Посты"), KeyboardButton(text="💧 Drip")],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def get_cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True
    )


def get_skip_cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⏭ Пропустить"), KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


def _audience_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Все", callback_data="aud_all"),
            InlineKeyboardButton(text="Free", callback_data="aud_free"),
            InlineKeyboardButton(text="VIP", callback_data="aud_vip"),
        ]]
    )


async def setup_bot_commands():
    commands = [types.BotCommand(command="start", description="Main Menu 🫦")]
    await bot.set_my_commands(commands)


# --- Удаление предыдущих сообщений бота ---
async def delete_prev_msgs(chat_id: int) -> None:
    msgs = _admin_last_msgs.pop(chat_id, [])
    for mid in msgs:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


def remember_msg(chat_id: int, msg: types.Message) -> None:
    _admin_last_msgs.setdefault(chat_id, []).append(msg.message_id)


# --- Авто-удаляемое уведомление ---
async def notify_and_delete(chat_id: int, text_msg: str, delay: int = 5) -> None:
    try:
        m = await bot.send_message(chat_id, text_msg)
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id, m.message_id)
    except Exception:
        pass


# =====================
# HANDLERS
# =====================

@dp.message(F.text == "❌ Отмена")
async def admin_cancel(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    await state.clear()
    await delete_prev_msgs(m.chat.id)
    try:
        await m.delete()
    except Exception:
        pass
    msg = await m.answer("Действие отменено.", reply_markup=get_admin_keyboard())
    remember_msg(m.chat.id, msg)


@dp.message(F.text == "⏭ Пропустить")
async def admin_skip(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    current = await state.get_state()
    try:
        await m.delete()
    except Exception:
        pass
    if current == PostStates.waiting_media:
        await state.update_data(media_id=None, media_type=None)
        await _ask_post_text(m, state)
    elif current == PostStates.waiting_text:
        await state.update_data(post_text=None)
        await _ask_post_button(m, state)
    elif current == PostStates.waiting_button_text:
        await state.update_data(button_text=None, button_url=None)
        await _ask_post_time(m, state)


# --- /start ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    await delete_prev_msgs(message.chat.id)

    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        if not user:
            session.add(User(
                id=message.from_user.id,
                username=message.from_user.username,
                joined_at=datetime.now(UTC).replace(tzinfo=None),
            ))
            await session.commit()

    if is_admin(message.from_user.id):
        m1 = await message.answer("Добро пожаловать, Босс.", reply_markup=get_admin_keyboard())
        m2 = await message.answer(
            "Панель управления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🌐 Открыть панель", web_app=WebAppInfo(url=MINIAPP_URL))
            ]])
        )
        remember_msg(message.chat.id, m1)
        remember_msg(message.chat.id, m2)
        return

    async with AsyncSessionLocal() as session:
        settings = await fetch_settings_map(session)
        welcome = settings.get("welcome_text") or ""
        markup = build_welcome_inline_keyboard(settings)
    await message.answer(welcome, reply_markup=markup)


# --- Статистика ---
@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    async with AsyncSessionLocal() as session:
        total = (await session.execute(text("SELECT count(*) FROM users"))).scalar() or 0
        vip = (await session.execute(text("SELECT count(*) FROM users WHERE is_vip = true"))).scalar() or 0
        in_drip = (await session.execute(text("SELECT count(*) FROM drip_progress WHERE is_active = true"))).scalar() or 0
        blocked = (await session.execute(text("SELECT count(*) FROM users WHERE blocked = true"))).scalar() or 0
        conv = (vip / total * 100.0) if total else 0.0
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        f"📊 *Статистика*\n\n"
        f"👥 Всего юзеров: `{total}`\n"
        f"👑 VIP: `{vip}`\n"
        f"💧 В drip: `{in_drip}`\n"
        f"🚫 Заблокировали: `{blocked}`\n"
        f"📈 Конверсия: `{conv:.1f}%`",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard(),
    )
    remember_msg(message.chat.id, msg)


# --- Рассылка ---
@dp.message(F.text == "📢 Рассылка")
async def admin_broadcast_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.set_state(AdminStates.waiting_broadcast_message)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "📢 *Рассылка*\n\nПришлите сообщение для рассылки (текст, фото, видео).",
        parse_mode="Markdown",
        reply_markup=get_cancel_kb(),
    )
    remember_msg(message.chat.id, msg)


@dp.message(AdminStates.waiting_broadcast_message)
async def admin_broadcast_got_message(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.update_data(bc_chat_id=message.chat.id, bc_message_id=message.message_id)
    await state.set_state(AdminStates.waiting_broadcast_audience)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer("Выберите аудиторию:", reply_markup=_audience_keyboard())
    remember_msg(message.chat.id, msg)


@dp.callback_query(
    AdminStates.waiting_broadcast_audience,
    F.data.in_(("aud_all", "aud_free", "aud_vip")),
)
async def admin_broadcast_run(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    target = query.data.split("_", 1)[1]
    data = await state.get_data()
    chat_id = data.get("bc_chat_id")
    msg_id = data.get("bc_message_id")
    await state.clear()
    await query.message.edit_reply_markup(reply_markup=None)

    async with AsyncSessionLocal() as session:
        if target == "all":
            res = await session.execute(select(User.id).where(User.blocked.is_(False)))
        elif target == "free":
            res = await session.execute(select(User.id).where(User.blocked.is_(False), User.is_vip.is_(False)))
        else:
            res = await session.execute(select(User.id).where(User.blocked.is_(False), User.is_vip.is_(True)))
        user_ids = list(res.scalars().all())

    sent = 0
    status_msg = await query.message.answer(f"🚀 Рассылка ({target}): {len(user_ids)} получателей…")
    for uid in user_ids:
        try:
            await telegram_with_flood_retry(
                lambda u=uid: bot.copy_message(chat_id=u, from_chat_id=chat_id, message_id=msg_id)
            )
            sent += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError:
            async with AsyncSessionLocal() as session:
                u = await session.get(User, uid)
                if u:
                    u.blocked = True
                    await session.commit()
        except Exception as e:
            logging.error(f"Broadcast error for {uid}: {e}")

    try:
        await bot.delete_message(query.message.chat.id, status_msg.message_id)
    except Exception:
        pass

    await delete_prev_msgs(query.message.chat.id)
    msg = await query.message.answer(
        f"✅ *Рассылка завершена*\n\nДоставлено: `{sent}` из `{len(user_ids)}`",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard(),
    )
    remember_msg(query.message.chat.id, msg)
    await query.answer()


# =====================
# ПОСТЫ — FSM
# =====================

async def _ask_post_text(message: types.Message, state: FSMContext):
    await state.set_state(PostStates.waiting_text)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "✏️ Введите текст поста или нажмите *Пропустить*:",
        parse_mode="Markdown",
        reply_markup=get_skip_cancel_kb(),
    )
    remember_msg(message.chat.id, msg)


async def _ask_post_button(message: types.Message, state: FSMContext):
    await state.set_state(PostStates.waiting_button_text)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "🔘 Введите текст кнопки или нажмите *Пропустить*:",
        parse_mode="Markdown",
        reply_markup=get_skip_cancel_kb(),
    )
    remember_msg(message.chat.id, msg)


async def _ask_post_time(message: types.Message, state: FSMContext):
    await state.set_state(PostStates.choosing_time)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "🕐 Когда опубликовать?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚡ Сейчас", callback_data="post_time_now"),
            InlineKeyboardButton(text="📅 Запланировать", callback_data="post_time_later"),
        ]]),
    )
    remember_msg(message.chat.id, msg)


@dp.message(F.text == "📝 Посты")
async def admin_posts_menu(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "📝 *Посты*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать пост", callback_data="post_create")],
            [InlineKeyboardButton(text="📋 Запланированные", callback_data="post_list")],
        ]),
    )
    remember_msg(message.chat.id, msg)


@dp.callback_query(F.data == "post_create")
async def post_create_start(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    await state.set_state(PostStates.choosing_channel)
    await state.update_data(media_id=None, media_type=None, post_text=None,
                             button_text=None, button_url=None)
    await delete_prev_msgs(query.message.chat.id)
    msg = await query.message.answer(
        "📡 Выберите канал для публикации:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🆓 Free", callback_data="post_ch_free"),
            InlineKeyboardButton(text="👑 Paid", callback_data="post_ch_paid"),
        ]]),
    )
    remember_msg(query.message.chat.id, msg)
    await query.answer()


@dp.callback_query(PostStates.choosing_channel, F.data.in_(("post_ch_free", "post_ch_paid")))
async def post_choose_channel(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    channel = "free" if query.data == "post_ch_free" else "paid"
    await state.update_data(channel=channel)
    await state.set_state(PostStates.waiting_media)
    await delete_prev_msgs(query.message.chat.id)
    msg = await query.message.answer(
        f"📡 Канал: *{'Free' if channel == 'free' else 'Paid'}*\n\n"
        "🖼 Отправьте фото, видео или аудио (будет как голосовое).\n"
        "Или нажмите *Пропустить* если пост без медиа.",
        parse_mode="Markdown",
        reply_markup=get_skip_cancel_kb(),
    )
    remember_msg(query.message.chat.id, msg)
    await query.answer()


@dp.message(PostStates.waiting_media, F.photo | F.video | F.audio | F.voice)
async def post_got_media(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    if message.photo:
        media_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        media_id = message.video.file_id
        media_type = "video"
    elif message.voice:
        media_id = message.voice.file_id
        media_type = "voice"
    elif message.audio:
        media_id = message.audio.file_id
        media_type = "voice"  # публикуем аудио как голосовое
    else:
        media_id = None
        media_type = None
    await state.update_data(media_id=media_id, media_type=media_type)
    await _ask_post_text(message, state)


@dp.message(PostStates.waiting_text)
async def post_got_text(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(post_text=message.text)
    await _ask_post_button(message, state)


@dp.message(PostStates.waiting_button_text)
async def post_got_button_text(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(button_text=message.text)
    await state.set_state(PostStates.waiting_button_url)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "🔗 Введите URL для кнопки:",
        reply_markup=get_cancel_kb(),
    )
    remember_msg(message.chat.id, msg)


@dp.message(PostStates.waiting_button_url)
async def post_got_button_url(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(button_url=message.text)
    await _ask_post_time(message, state)


@dp.callback_query(PostStates.choosing_time, F.data.in_(("post_time_now", "post_time_later")))
async def post_choose_time(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    if query.data == "post_time_now":
        await state.update_data(publish_now=True, scheduled_at=None)
        await _show_post_preview(query.message, state)
    else:
        await state.set_state(PostStates.waiting_custom_time)
        await delete_prev_msgs(query.message.chat.id)
        msg = await query.message.answer(
            "📅 Введите дату и время публикации в формате:\n`ДД.ММ.ГГГГ ЧЧ:ММ`\n\nНапример: `25.12.2025 15:00`",
            parse_mode="Markdown",
            reply_markup=get_cancel_kb(),
        )
        remember_msg(query.message.chat.id, msg)
    await query.answer()


@dp.message(PostStates.waiting_custom_time)
async def post_got_custom_time(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        msg = await message.answer(
            "❌ Неверный формат. Попробуйте ещё раз:\n`ДД.ММ.ГГГГ ЧЧ:ММ`",
            parse_mode="Markdown",
        )
        remember_msg(message.chat.id, msg)
        return
    await state.update_data(publish_now=False, scheduled_at=dt)
    await _show_post_preview(message, state)


async def _show_post_preview(message: types.Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(PostStates.confirming)
    await delete_prev_msgs(message.chat.id)

    channel = data.get("channel", "free")
    media_id = data.get("media_id")
    media_type = data.get("media_type")
    post_text = data.get("post_text") or ""
    button_text = data.get("button_text")
    button_url = data.get("button_url")
    publish_now = data.get("publish_now", True)
    scheduled_at = data.get("scheduled_at")

    # Строим клавиатуру превью
    preview_kb = None
    if button_text and button_url:
        preview_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=button_text, url=button_url)
        ]])

    # Показываем превью
    info = (
        f"👁 *Превью поста*\n\n"
        f"📡 Канал: *{'Free' if channel == 'free' else 'Paid'}*\n"
        f"⏰ {'Публикация: сейчас' if publish_now else f'Запланировано: {scheduled_at.strftime(chr(37)+chr(100)+chr(46)+chr(37)+chr(109)+chr(46)+chr(37)+chr(89)+chr(32)+chr(37)+chr(72)+chr(58)+chr(37)+chr(77))}'}\n\n"
        f"Так будет выглядеть пост:"
    )
    msg_info = await message.answer(info, parse_mode="Markdown")
    remember_msg(message.chat.id, msg_info)

    # Отправляем само превью
    try:
        if media_type == "photo" and media_id:
            preview_msg = await message.answer_photo(photo=media_id, caption=post_text or None, reply_markup=preview_kb)
        elif media_type == "video" and media_id:
            preview_msg = await message.answer_video(video=media_id, caption=post_text or None, reply_markup=preview_kb)
        elif media_type == "voice" and media_id:
            preview_msg = await message.answer_voice(voice=media_id, caption=post_text or None, reply_markup=preview_kb)
        elif post_text:
            preview_msg = await message.answer(post_text, reply_markup=preview_kb)
        else:
            preview_msg = await message.answer("_(пустой пост)_", parse_mode="Markdown")
        remember_msg(message.chat.id, preview_msg)
    except Exception as e:
        err_msg = await message.answer(f"⚠️ Не удалось показать превью: {e}")
        remember_msg(message.chat.id, err_msg)

    # Кнопки подтверждения
    confirm_msg = await message.answer(
        "Подтвердите публикацию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Опубликовать", callback_data="post_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="post_cancel"),
        ]]),
    )
    remember_msg(message.chat.id, confirm_msg)


@dp.callback_query(PostStates.confirming, F.data == "post_cancel")
async def post_cancel_confirm(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    await state.clear()
    await delete_prev_msgs(query.message.chat.id)
    msg = await query.message.answer("❌ Отменено.", reply_markup=get_admin_keyboard())
    remember_msg(query.message.chat.id, msg)
    await query.answer()


@dp.callback_query(PostStates.confirming, F.data == "post_confirm")
async def post_confirm(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    data = await state.get_data()
    await state.clear()

    channel = data.get("channel", "free")
    media_id = data.get("media_id")
    media_type = data.get("media_type")
    post_text = data.get("post_text")
    button_text = data.get("button_text")
    button_url = data.get("button_url")
    publish_now = data.get("publish_now", True)
    scheduled_at = data.get("scheduled_at")

    now = datetime.now(UTC).replace(tzinfo=None)
    sched = now if publish_now else scheduled_at

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "INSERT INTO content_queue "
                "(text, media_id, price, post_type, channel, button_text, button_url, scheduled_at, status) "
                "VALUES (:text, :mid, 0, 'free', :ch, :bt, :bu, :sa, 'pending')"
            ),
            {"text": post_text, "mid": media_id, "ch": channel,
             "bt": button_text, "bu": button_url, "sa": sched},
        )
        await session.commit()

    await delete_prev_msgs(query.message.chat.id)
    if publish_now:
        result_text = "✅ *Пост добавлен в очередь на публикацию!*\nБудет опубликован в течение минуты."
    else:
        result_text = f"🕐 *Пост запланирован*\nДата: `{scheduled_at.strftime('%d.%m.%Y %H:%M')}`"

    msg = await query.message.answer(result_text, parse_mode="Markdown", reply_markup=get_admin_keyboard())
    remember_msg(query.message.chat.id, msg)
    await query.answer("✅ Готово!")


@dp.callback_query(F.data == "post_list")
async def post_list(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            text("SELECT id, channel, text, scheduled_at FROM content_queue WHERE status='pending' ORDER BY scheduled_at ASC NULLS LAST LIMIT 10")
        )).mappings().all()

    if not rows:
        await delete_prev_msgs(query.message.chat.id)
        msg = await query.message.answer("📋 Нет запланированных постов.", reply_markup=get_admin_keyboard())
        remember_msg(query.message.chat.id, msg)
        await query.answer()
        return

    text_lines = ["📋 *Запланированные посты:*\n"]
    buttons = []
    for r in rows:
        dt = r["scheduled_at"]
        dt_str = dt.strftime("%d.%m %H:%M") if dt else "сейчас"
        preview = (r["text"] or "")[:30] or "(медиа)"
        text_lines.append(f"• `{dt_str}` [{r['channel']}] {preview}")
        buttons.append([InlineKeyboardButton(
            text=f"🗑 #{r['id']} {dt_str}",
            callback_data=f"post_del_{r['id']}"
        )])

    await delete_prev_msgs(query.message.chat.id)
    msg = await query.message.answer(
        "\n".join(text_lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    remember_msg(query.message.chat.id, msg)
    await query.answer()


@dp.callback_query(F.data.startswith("post_del_"))
async def post_delete(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    post_id = int(query.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        await session.execute(text("DELETE FROM content_queue WHERE id = :id"), {"id": post_id})
        await session.commit()
    await query.answer("🗑 Удалено")
    await post_list(query, state)


# =====================
# DRIP — FSM
# =====================

@dp.message(F.text == "💧 Drip")
async def admin_drip_menu(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.clear()
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            text("SELECT step, text, media_type, is_active, delay_hours FROM drip_messages ORDER BY step")
        )).mappings().all()
    by_step = {r["step"]: r for r in rows}

    buttons = []
    for s in range(5):
        r = by_step.get(s)
        if r:
            status = "✅" if r["is_active"] else "⏸"
            preview = (r["text"] or "")[:20] or f"[{r['media_type'] or 'медиа'}]"
            label = f"{status} Шаг {s} · {r['delay_hours']}ч · {preview}"
        else:
            label = f"➕ Шаг {s} (не настроен)"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"drip_step_{s}")])

    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "💧 *Drip-цепочка*\n\nВыберите шаг для просмотра или редактирования:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    remember_msg(message.chat.id, msg)


@dp.callback_query(F.data.startswith("drip_step_"))
async def drip_view_step(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    step = int(query.data.split("_")[-1])
    await state.update_data(drip_editing_step=step)

    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("SELECT * FROM drip_messages WHERE step = :s"),
            {"s": step}
        )).mappings().first()

    await delete_prev_msgs(query.message.chat.id)

    if row:
        status = "✅ Активен" if row["is_active"] else "⏸ Выключен"
        info = (
            f"💧 *Шаг {step}*\n\n"
            f"Статус: {status}\n"
            f"Задержка: `{row['delay_hours']}` ч\n"
            f"Медиа: `{row['media_type'] or 'нет'}`\n\n"
            f"Текст:\n{row['text'] or '_(нет)_'}"
        )
        toggle_text = "⏸ Выключить" if row["is_active"] else "✅ Включить"
        buttons = [
            [InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"drip_edit_{step}")],
            [InlineKeyboardButton(text=toggle_text, callback_data=f"drip_toggle_{step}")],
            [InlineKeyboardButton(text="👁 Превью", callback_data=f"drip_preview_{step}")],
            [InlineKeyboardButton(text="« Назад", callback_data="drip_back")],
        ]
    else:
        info = f"💧 *Шаг {step}*\n\n_(не настроен)_"
        buttons = [
            [InlineKeyboardButton(text="➕ Настроить", callback_data=f"drip_edit_{step}")],
            [InlineKeyboardButton(text="« Назад", callback_data="drip_back")],
        ]

    msg = await query.message.answer(
        info, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    remember_msg(query.message.chat.id, msg)
    await query.answer()


@dp.callback_query(F.data == "drip_back")
async def drip_back(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    await state.clear()
    # пересоздаём меню drip
    class FakeMsg:
        chat = query.message.chat
        from_user = query.from_user
        async def delete(self): pass
        async def answer(self, *a, **kw): return await query.message.answer(*a, **kw)
    await admin_drip_menu(FakeMsg(), state)
    await query.answer()


@dp.callback_query(F.data.startswith("drip_preview_"))
async def drip_preview(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    step = int(query.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("SELECT * FROM drip_messages WHERE step = :s"), {"s": step}
        )).mappings().first()

    if not row:
        await query.answer("Шаг не настроен", show_alert=True)
        return

    kb = None
    if (row["button_text"] or "").strip() and (row["button_url"] or "").strip():
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=row["button_text"], url=row["button_url"])
        ]])
    try:
        mtype = (row["media_type"] or "none").lower()
        mid = row["media_file_id"]
        if mtype == "photo" and mid:
            preview_msg = await query.message.answer_photo(photo=mid, caption=row["text"] or None, reply_markup=kb)
        elif mtype == "video" and mid:
            preview_msg = await query.message.answer_video(video=mid, caption=row["text"] or None, reply_markup=kb)
        elif mtype == "voice" and mid:
            preview_msg = await query.message.answer_voice(voice=mid, caption=row["text"] or None, reply_markup=kb)
        else:
            preview_msg = await query.message.answer(row["text"] or "_(пустой шаг)_", parse_mode="Markdown", reply_markup=kb)
        remember_msg(query.message.chat.id, preview_msg)
    except Exception as e:
        await query.answer(f"Ошибка: {e}", show_alert=True)
        return
    await query.answer("👁 Превью отправлено")


@dp.callback_query(F.data.startswith("drip_toggle_"))
async def drip_toggle(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    step = int(query.data.split("_")[-1])
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            text("SELECT is_active FROM drip_messages WHERE step = :s"), {"s": step}
        )).mappings().first()
        if row:
            new_val = not row["is_active"]
            await session.execute(
                text("UPDATE drip_messages SET is_active = :v WHERE step = :s"),
                {"v": new_val, "s": step}
            )
            await session.commit()
    await query.answer("✅ Статус изменён")
    await drip_view_step(query, state)


@dp.callback_query(F.data.startswith("drip_edit_"))
async def drip_edit_start(query: types.CallbackQuery, state: FSMContext):
    if not is_admin(query.from_user.id):
        return
    step = int(query.data.split("_")[-1])
    await state.update_data(drip_editing_step=step)
    await state.set_state(DripStates.editing_text)
    await delete_prev_msgs(query.message.chat.id)
    msg = await query.message.answer(
        f"✏️ *Редактирование шага {step}*\n\nВведите текст сообщения или нажмите *Пропустить*:",
        parse_mode="Markdown",
        reply_markup=get_skip_cancel_kb(),
    )
    remember_msg(query.message.chat.id, msg)
    await query.answer()


@dp.message(DripStates.editing_text)
async def drip_got_text(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(drip_new_text=message.text)
    await state.set_state(DripStates.editing_media)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "🖼 Отправьте медиа (фото/видео/аудио) или нажмите *Пропустить*:",
        parse_mode="Markdown",
        reply_markup=get_skip_cancel_kb(),
    )
    remember_msg(message.chat.id, msg)


@dp.message(DripStates.editing_media, F.photo | F.video | F.audio | F.voice)
async def drip_got_media(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    if message.photo:
        media_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        media_id = message.video.file_id
        media_type = "video"
    else:
        media_id = (message.voice or message.audio).file_id
        media_type = "voice"
    await state.update_data(drip_new_media=media_id, drip_new_media_type=media_type)
    await _drip_ask_button(message, state)


async def _drip_ask_button(message: types.Message, state: FSMContext):
    await state.set_state(DripStates.editing_button_text)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "🔘 Введите текст кнопки или нажмите *Пропустить*:",
        parse_mode="Markdown",
        reply_markup=get_skip_cancel_kb(),
    )
    remember_msg(message.chat.id, msg)


@dp.message(DripStates.editing_button_text)
async def drip_got_button_text(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(drip_new_btn_text=message.text)
    await state.set_state(DripStates.editing_button_url)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer("🔗 Введите URL кнопки:", reply_markup=get_cancel_kb())
    remember_msg(message.chat.id, msg)


@dp.message(DripStates.editing_button_url)
async def drip_got_button_url(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    await state.update_data(drip_new_btn_url=message.text)
    await state.set_state(DripStates.editing_delay)
    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        "⏱ Через сколько часов отправить этот шаг после предыдущего?\nВведите число (например: `24`):",
        parse_mode="Markdown",
        reply_markup=get_cancel_kb(),
    )
    remember_msg(message.chat.id, msg)


@dp.message(DripStates.editing_delay)
async def drip_got_delay(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        await message.delete()
    except Exception:
        pass
    try:
        delay = int(message.text.strip())
    except ValueError:
        msg = await message.answer("❌ Введите число. Например: `24`", parse_mode="Markdown")
        remember_msg(message.chat.id, msg)
        return

    data = await state.get_data()
    step = data.get("drip_editing_step", 0)
    new_text = data.get("drip_new_text")
    new_media = data.get("drip_new_media")
    new_media_type = data.get("drip_new_media_type", "none")
    new_btn_text = data.get("drip_new_btn_text")
    new_btn_url = data.get("drip_new_btn_url")
    await state.clear()

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO drip_messages (step, text, media_file_id, media_type, button_text, button_url, delay_hours, is_active)
                VALUES (:s, :t, :m, :mt, :bt, :bu, :dh, true)
                ON CONFLICT (step) DO UPDATE SET
                    text = EXCLUDED.text,
                    media_file_id = EXCLUDED.media_file_id,
                    media_type = EXCLUDED.media_type,
                    button_text = EXCLUDED.button_text,
                    button_url = EXCLUDED.button_url,
                    delay_hours = EXCLUDED.delay_hours,
                    is_active = true
            """),
            {"s": step, "t": new_text, "m": new_media, "mt": new_media_type or "none",
             "bt": new_btn_text, "bu": new_btn_url, "dh": delay},
        )
        await session.commit()

    await delete_prev_msgs(message.chat.id)
    msg = await message.answer(
        f"✅ *Шаг {step} сохранён!*",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard(),
    )
    remember_msg(message.chat.id, msg)


# Пропуск медиа при редактировании drip (обрабатывается общим хендлером skip выше)
# Но нам нужен отдельный для drip_editing_media
@dp.message(DripStates.editing_media)
async def drip_skip_media_text(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    # Если не медиа-файл — игнорируем, кнопка Пропустить обработана выше
    pass


# =====================
# FILE ID HELPER
# =====================

@dp.message(F.photo | F.video | F.audio | F.voice)
async def admin_get_file_id(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    current_state = await state.get_state()
    # Если в FSM-состоянии — не перехватываем
    if current_state is not None:
        return
    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "photo"
    elif message.video:
        file_id = message.video.file_id
        media_type = "video"
    elif message.voice:
        file_id = message.voice.file_id
        media_type = "voice"
    elif message.audio:
        file_id = message.audio.file_id
        media_type = "voice"
    else:
        return
    await message.reply(
        f"media_type: `{media_type}`\nfile_id: `{file_id}`",
        parse_mode="Markdown"
    )


# =====================
# JOIN REQUEST / CHAT MEMBER
# =====================

@dp.chat_join_request()
async def handle_join_request(event: ChatJoinRequest):
    await event.approve()
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    async with AsyncSessionLocal() as session:
        user = await session.get(User, event.from_user.id)
        if not user:
            session.add(User(
                id=event.from_user.id,
                username=event.from_user.username,
                joined_at=now_naive,
            ))
        settings = await fetch_settings_map(session)
        welcome = settings.get("welcome_text") or ""
        markup = build_welcome_inline_keyboard(settings)

        dm = (await session.execute(
            select(DripMessage).where(DripMessage.step == 0, DripMessage.is_active.is_(True))
        )).scalar_one_or_none()

        if dm:
            next_at = now_naive + timedelta(hours=dm.delay_hours or 24)
            await session.merge(DripProgress(
                user_id=event.from_user.id,
                current_step=1,
                next_send_at=next_at,
                is_active=True,
            ))
        await session.commit()

    try:
        await bot.send_message(event.from_user.id, welcome, reply_markup=markup)
        # Отправляем step 0 сразу
        if dm:
            kb = None
            if (dm.button_text or "").strip() and (dm.button_url or "").strip():
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=dm.button_text, url=dm.button_url)
                ]])
            text_body = dm.text or ""
            mtype = (dm.media_type or "none").lower()
            mid = dm.media_file_id
            if mtype == "video" and mid:
                await bot.send_video(event.from_user.id, video=mid, caption=text_body or None, reply_markup=kb)
            elif mtype == "photo" and mid:
                await bot.send_photo(event.from_user.id, photo=mid, caption=text_body or None, reply_markup=kb)
            elif mtype == "voice" and mid:
                await bot.send_voice(event.from_user.id, voice=mid, caption=text_body or None, reply_markup=kb)
            elif text_body:
                await bot.send_message(event.from_user.id, text_body, reply_markup=kb)
    except TelegramForbiddenError:
        async with AsyncSessionLocal() as session:
            u = await session.get(User, event.from_user.id)
            if u:
                u.blocked = True
                await session.commit()
        return

    # Уведомляем всех админов
    uname = f"@{event.from_user.username}" if event.from_user.username else f"id:{event.from_user.id}"
    for admin_id in ADMIN_IDS:
        asyncio.create_task(notify_and_delete(
            admin_id,
            f"👤 Новый подписчик: {uname}",
            delay=10
        ))


@dp.chat_member(F.chat.id == PAID_CHANNEL_ID)
async def on_paid_channel_member_updated(event: ChatMemberUpdated):
    subject = event.new_chat_member.user
    if subject.is_bot:
        return
    old = event.old_chat_member.status
    new = event.new_chat_member.status
    if new not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return
    if old in _MEMBER_LIKE:
        return

    uid = subject.id
    async with AsyncSessionLocal() as session:
        u = await session.get(User, uid)
        if not u:
            session.add(User(
                id=uid,
                username=subject.username,
                joined_at=datetime.now(UTC).replace(tzinfo=None),
                is_vip=True,
            ))
        else:
            u.is_vip = True
        await session.execute(
            text("UPDATE drip_progress SET is_active = false WHERE user_id = :uid"),
            {"uid": uid},
        )
        await session.commit()

    # Уведомляем всех админов
    uname = f"@{subject.username}" if subject.username else f"id:{uid}"
    for admin_id in ADMIN_IDS:
        asyncio.create_task(notify_and_delete(
            admin_id,
            f"👑 Новый VIP: {uname}",
            delay=10
        ))


# =====================
# WORKERS
# =====================

async def drip_worker(stop: asyncio.Event):
    while True:
        if stop.is_set():
            break
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            async with AsyncSessionLocal() as session:
                q = await session.execute(
                    select(DripProgress.user_id).where(
                        DripProgress.is_active.is_(True),
                        DripProgress.next_send_at.is_not(None),
                        DripProgress.next_send_at <= now,
                    )
                )
                due_user_ids = list(q.scalars().all())

            for uid in due_user_ids:
                if stop.is_set():
                    break
                async with AsyncSessionLocal() as session:
                    lock_key = f"bot_drip:{uid}"
                    if not await advisory_try_lock_key(session, lock_key):
                        continue
                    try:
                        prog = await session.get(DripProgress, uid)
                        if not prog or not prog.is_active or not prog.next_send_at:
                            continue
                        if prog.next_send_at > now:
                            continue

                        user = await session.get(User, uid)
                        if user and user.is_vip:
                            prog.is_active = False
                            await session.commit()
                            continue
                        if user and user.blocked:
                            prog.is_active = False
                            await session.commit()
                            continue

                        step = prog.current_step
                        dm = (await session.execute(
                            select(DripMessage).where(
                                DripMessage.step == step,
                                DripMessage.is_active.is_(True),
                            )
                        )).scalar_one_or_none()
                        if not dm:
                            prog.is_active = False
                            await session.commit()
                            continue

                        kb = None
                        if (dm.button_text or "").strip() and (dm.button_url or "").strip():
                            kb = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text=dm.button_text, url=dm.button_url)
                            ]])
                        text_body = dm.text or ""
                        mtype = (dm.media_type or "none").lower()
                        mid = dm.media_file_id

                        try:
                            if mtype == "video" and mid:
                                await telegram_with_flood_retry(lambda: bot.send_video(uid, video=mid, caption=text_body or None, reply_markup=kb))
                            elif mtype == "photo" and mid:
                                await telegram_with_flood_retry(lambda: bot.send_photo(uid, photo=mid, caption=text_body or None, reply_markup=kb))
                            elif mtype == "voice" and mid:
                                await telegram_with_flood_retry(lambda: bot.send_voice(uid, voice=mid, caption=text_body or None, reply_markup=kb))
                            else:
                                await telegram_with_flood_retry(lambda: bot.send_message(uid, text_body, reply_markup=kb))
                        except TelegramForbiddenError:
                            urow = await session.get(User, uid)
                            if urow:
                                urow.blocked = True
                            prog.is_active = False
                            await session.commit()
                            continue

                        prog.current_step = step + 1
                        if prog.current_step > 4:
                            prog.is_active = False
                            prog.next_send_at = None
                        else:
                            nxt = (await session.execute(
                                select(DripMessage).where(
                                    DripMessage.step == prog.current_step,
                                    DripMessage.is_active.is_(True),
                                )
                            )).scalar_one_or_none()
                            if not nxt:
                                prog.is_active = False
                                prog.next_send_at = None
                            else:
                                prog.next_send_at = now + timedelta(hours=nxt.delay_hours or 24)
                        await session.commit()
                    finally:
                        await advisory_unlock_key(session, lock_key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Drip worker error: {e}")
        await interruptible_sleep(300.0, stop)


async def check_scheduled_posts(stop: asyncio.Event):
    while True:
        if stop.is_set():
            break
        try:
            now = datetime.now(UTC).replace(tzinfo=None)
            async with AsyncSessionLocal() as session:
                res = await session.execute(
                    text("SELECT id FROM content_queue WHERE status = 'pending' AND scheduled_at <= :now ORDER BY scheduled_at ASC"),
                    {"now": now},
                )
                post_ids = list(res.scalars().all())

            for post_id in post_ids:
                if stop.is_set():
                    break
                async with AsyncSessionLocal() as session:
                    lock_key = f"bot_post:{post_id}"
                    if not await advisory_try_lock_key(session, lock_key):
                        continue
                    try:
                        row = (await session.execute(
                            text("SELECT id, text, media_id, price, post_type, channel, button_text, button_url FROM content_queue WHERE id = :id AND status = 'pending' AND scheduled_at <= :now"),
                            {"id": post_id, "now": now},
                        )).mappings().first()
                        if not row:
                            continue

                        post = dict(row)
                        ch = (post.get("channel") or "free").lower()
                        target = PAID_CHANNEL_ID if ch == "paid" else FREE_CHANNEL_ID
                        btn_t = post.get("button_text")
                        btn_u = post.get("button_url")
                        reply_markup = None
                        if (btn_t or "").strip() and (btn_u or "").strip():
                            reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(text=btn_t, url=btn_u)
                            ]])

                        try:
                            if post.get("post_type") == "paid":
                                await telegram_with_flood_retry(lambda: bot.send_paid_media(
                                    chat_id=target,
                                    star_count=post["price"] or 0,
                                    media=[InputPaidMediaPhoto(media=post["media_id"])],
                                    caption=post.get("text"),
                                    reply_markup=reply_markup,
                                ))
                            elif post.get("media_id"):
                                await telegram_with_flood_retry(lambda: bot.send_photo(
                                    chat_id=target,
                                    photo=post["media_id"],
                                    caption=post.get("text"),
                                    reply_markup=reply_markup,
                                ))
                            elif post.get("text"):
                                await telegram_with_flood_retry(lambda: bot.send_message(
                                    chat_id=target,
                                    text=post["text"],
                                    reply_markup=reply_markup,
                                ))
                            else:
                                logging.error(f"Post id={post_id} has no text or media, skipping")
                                continue
                        except Exception as e:
                            logging.error(f"Post error id={post_id}: {e}")
                            continue

                        await session.execute(
                            text("UPDATE content_queue SET status = 'published' WHERE id = :id"),
                            {"id": post["id"]},
                        )
                        await session.commit()
                    finally:
                        await advisory_unlock_key(session, lock_key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Scheduler error: {e}")
        await interruptible_sleep(60.0, stop)


async def run_migrations(connection):
    await connection.execute(text("ALTER TABLE users ALTER COLUMN id TYPE BIGINT"))
    await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP WITHOUT TIME ZONE"))
    await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS has_gift_claimed BOOLEAN DEFAULT false"))
    await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_spent BIGINT DEFAULT 0"))
    await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_vip BOOLEAN DEFAULT false"))
    await connection.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT false"))
    await connection.execute(text("ALTER TABLE content_queue ADD COLUMN IF NOT EXISTS channel TEXT DEFAULT 'free'"))
    await connection.execute(text("ALTER TABLE content_queue ADD COLUMN IF NOT EXISTS button_text TEXT"))
    await connection.execute(text("ALTER TABLE content_queue ADD COLUMN IF NOT EXISTS button_url TEXT"))


async def main():
    import uvicorn
    from api import create_api_app

    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
        await run_migrations(c)

    async with AsyncSessionLocal() as session:
        await seed_default_settings(session)

    await setup_bot_commands()

    fastapi_app = create_api_app(
        bot=bot,
        bot_token=BOT_TOKEN,
        admin_id=ADMIN_ID,
        session_factory=AsyncSessionLocal,
    )
    try:
        uv_cfg = uvicorn.Config(fastapi_app, host="0.0.0.0", port=PORT, log_level="info", loop="asyncio", install_signal_handlers=False)
    except TypeError:
        uv_cfg = uvicorn.Config(fastapi_app, host="0.0.0.0", port=PORT, log_level="info", loop="asyncio")
    uv_server = uvicorn.Server(uv_cfg)

    stop_workers = asyncio.Event()
    shutdown_req = asyncio.Event()

    serve_task = asyncio.create_task(uv_server.serve())
    drip_task = asyncio.create_task(drip_worker(stop_workers))
    posts_task = asyncio.create_task(check_scheduled_posts(stop_workers))

    loop = asyncio.get_running_loop()

    def _request_shutdown():
        shutdown_req.set()

    try:
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            loop.add_signal_handler(sig, _request_shutdown)
    except (NotImplementedError, RuntimeError):
        logging.warning("Signal handlers not installed")

    await bot.delete_webhook(drop_pending_updates=True)

    poll_task = asyncio.create_task(dp.start_polling(bot))
    shutdown_wait = asyncio.create_task(shutdown_req.wait())
    await asyncio.wait({poll_task, shutdown_wait}, return_when=asyncio.FIRST_COMPLETED)

    stop_workers.set()

    if not shutdown_wait.done():
        shutdown_wait.cancel()
    if not poll_task.done():
        poll_task.cancel()

    await asyncio.gather(shutdown_wait, poll_task, drip_task, posts_task, return_exceptions=True)

    uv_server.should_exit = True
    try:
        await serve_task
    except asyncio.CancelledError:
        pass
    try:
        await uv_server.shutdown()
    except Exception:
        pass

    try:
        await bot.session.close()
    except Exception:
        pass
    try:
        await engine.dispose()
    except Exception:
        pass
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
