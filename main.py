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

T = TypeVar("T")

_redis_client = None

# --- Database Setup ---
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

# --- Bot Setup ---
if REDIS_URL:
    from redis.asyncio import Redis
    from aiogram.fsm.storage.redis import RedisStorage

    _redis_client = Redis.from_url(REDIS_URL)
    storage = RedisStorage(redis=_redis_client)
else:
    storage = MemoryStorage()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=storage)


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


class AdminStates(StatesGroup):
    waiting_broadcast_message = State()
    waiting_broadcast_audience = State()


def _free_channel_tg_link() -> str:
    cid = FREE_CHANNEL_ID
    s = str(cid)
    if s.startswith("-100"):
        inner = s[4:]
    else:
        inner = str(abs(cid))
    return f"https://t.me/c/{inner}"


def get_admin_keyboard():
    kb = [
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📢 Рассылка")],
        [KeyboardButton(text="🌐 Открыть панель", web_app=WebAppInfo(url=MINIAPP_URL))],
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)


def get_cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True
    )


def _audience_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Все", callback_data="aud_all"),
                InlineKeyboardButton(text="Free", callback_data="aud_free"),
                InlineKeyboardButton(text="VIP", callback_data="aud_vip"),
            ]
        ]
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
    defaults = [
        ("welcome_text", "Missed me? 🫦 I've been thinking about u all day."),
        ("button1_text", "🫦 Enter the Sanctuary"),
        ("button1_url", _free_channel_tg_link()),
        ("button2_text", "⭐ VIP (Stars)"),
        ("button2_url", ""),
        ("paid_channel_link", PAID_CHANNEL_LINK),
    ]
    for key, val in defaults:
        await session.execute(
            text(
                "INSERT INTO settings (key, value) VALUES (:k, :v) "
                "ON CONFLICT (key) DO NOTHING"
            ),
            {"k": key, "v": val},
        )
    await session.commit()


@dp.message(F.text == "❌ Отмена", F.from_user.id == ADMIN_ID)
async def admin_cancel(m: types.Message, state: FSMContext):
    await state.clear()
    await m.answer("Действие отменено.", reply_markup=get_admin_keyboard())


async def setup_bot_commands():
    commands = [types.BotCommand(command="start", description="Main Menu 🫦")]
    await bot.set_my_commands(commands)


@dp.chat_join_request()
async def handle_join_request(event: ChatJoinRequest):
    # Деплой: бот — админ FREE-канала с правом одобрять заявки на вступление.
    await event.approve()
    now_naive = datetime.now(UTC).replace(tzinfo=None)
    async with AsyncSessionLocal() as session:
        user = await session.get(User, event.from_user.id)
        if not user:
            session.add(
                User(
                    id=event.from_user.id,
                    username=event.from_user.username,
                    joined_at=now_naive,
                )
            )
        settings = await fetch_settings_map(session)
        welcome = settings.get("welcome_text") or ""
        markup = build_welcome_inline_keyboard(settings)

        dm = (
            await session.execute(
                select(DripMessage).where(
                    DripMessage.step == 0,
                    DripMessage.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if dm:
            next_at = now_naive + timedelta(hours=dm.delay_hours or 24)
            await session.merge(
                DripProgress(
                    user_id=event.from_user.id,
                    current_step=0,
                    next_send_at=next_at,
                    is_active=True,
                )
            )
        await session.commit()

    try:
        await bot.send_message(
            event.from_user.id,
            welcome,
            reply_markup=markup,
        )
    except TelegramForbiddenError:
        async with AsyncSessionLocal() as session:
            u = await session.get(User, event.from_user.id)
            if u:
                u.blocked = True
                await session.commit()


@dp.chat_member(F.chat.id == PAID_CHANNEL_ID)
async def on_paid_channel_member_updated(event: ChatMemberUpdated):
    # Деплой: бот — админ PAID-канала с правом «добавлять участников» /
    # видеть обновления участников, иначе события chat_member не приходят.
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
            session.add(
                User(
                    id=uid,
                    username=subject.username,
                    joined_at=datetime.now(UTC).replace(tzinfo=None),
                    is_vip=True,
                )
            )
        else:
            u.is_vip = True
        await session.execute(
            text(
                "UPDATE drip_progress SET is_active = false WHERE user_id = :uid"
            ),
            {"uid": uid},
        )
        await session.commit()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    async with AsyncSessionLocal() as session:
        user = await session.get(User, message.from_user.id)
        if not user:
            session.add(
                User(
                    id=message.from_user.id,
                    username=message.from_user.username,
                    joined_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            await session.commit()

    if message.from_user.id == ADMIN_ID:
        await message.answer("Добро пожаловать, Босс.", reply_markup=get_admin_keyboard())
        await message.answer(
            "Панель управления:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🌐 Открыть панель", web_app=WebAppInfo(url=MINIAPP_URL))
            ]])
        )
        return

    async with AsyncSessionLocal() as session:
        settings = await fetch_settings_map(session)
        welcome = settings.get("welcome_text") or ""
        markup = build_welcome_inline_keyboard(settings)
    await message.answer(welcome, reply_markup=markup)


@dp.message(F.text == "📊 Статистика", F.from_user.id == ADMIN_ID)
async def admin_stats(message: types.Message):
    async with AsyncSessionLocal() as session:
        total = (await session.execute(text("SELECT count(*) FROM users"))).scalar() or 0
        vip = (
            await session.execute(
                text("SELECT count(*) FROM users WHERE is_vip = true")
            )
        ).scalar() or 0
        in_drip = (
            await session.execute(
                text(
                    "SELECT count(*) FROM drip_progress WHERE is_active = true"
                )
            )
        ).scalar() or 0
        blocked = (
            await session.execute(
                text("SELECT count(*) FROM users WHERE blocked = true")
            )
        ).scalar() or 0
        conv = (vip / total * 100.0) if total else 0.0
        await message.answer(
            f"📊 Всего юзеров: {total}\n"
            f"👑 VIP: {vip}\n"
            f"💧 В drip (активны): {in_drip}\n"
            f"🚫 Blocked: {blocked}\n"
            f"📈 Конверсия: {conv:.1f}%"
        )


@dp.message(F.text == "📢 Рассылка", F.from_user.id == ADMIN_ID)
async def admin_broadcast_start(message: types.Message, state: FSMContext):
    await state.set_state(AdminStates.waiting_broadcast_message)
    await message.answer(
        "Пришлите сообщение для рассылки (текст, фото, видео и т.д.).",
        reply_markup=get_cancel_kb(),
    )


@dp.message(AdminStates.waiting_broadcast_message, F.from_user.id == ADMIN_ID)
async def admin_broadcast_got_message(message: types.Message, state: FSMContext):
    await state.update_data(
        bc_chat_id=message.chat.id,
        bc_message_id=message.message_id,
    )
    await state.set_state(AdminStates.waiting_broadcast_audience)
    await message.answer(
        "Выберите аудиторию:",
        reply_markup=_audience_keyboard(),
    )


@dp.callback_query(
    AdminStates.waiting_broadcast_audience,
    F.data.in_(("aud_all", "aud_free", "aud_vip")),
    F.from_user.id == ADMIN_ID,
)
async def admin_broadcast_run(query: types.CallbackQuery, state: FSMContext):
    target = query.data.split("_", 1)[1]
    data = await state.get_data()
    chat_id = data.get("bc_chat_id")
    msg_id = data.get("bc_message_id")
    await state.clear()
    await query.message.edit_reply_markup(reply_markup=None)

    async with AsyncSessionLocal() as session:
        if target == "all":
            res = await session.execute(
                select(User.id).where(User.blocked.is_(False))
            )
        elif target == "free":
            res = await session.execute(
                select(User.id).where(
                    User.blocked.is_(False),
                    User.is_vip.is_(False),
                )
            )
        else:
            res = await session.execute(
                select(User.id).where(
                    User.blocked.is_(False),
                    User.is_vip.is_(True),
                )
            )
        user_ids = list(res.scalars().all())

    sent = 0
    await query.message.answer(f"🚀 Рассылка ({target}): {len(user_ids)} получателей…")
    for uid in user_ids:
        try:
            await telegram_with_flood_retry(
                lambda u=uid: bot.copy_message(
                    chat_id=u,
                    from_chat_id=chat_id,
                    message_id=msg_id,
                )
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

    await query.message.answer(
        f"✅ Готово. Доставлено: {sent} из {len(user_ids)}.",
        reply_markup=get_admin_keyboard(),
    )
    await query.answer()


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
                        dm = (
                            await session.execute(
                                select(DripMessage).where(
                                    DripMessage.step == step,
                                    DripMessage.is_active.is_(True),
                                )
                            )
                        ).scalar_one_or_none()
                        if not dm:
                            prog.is_active = False
                            await session.commit()
                            continue

                        kb = None
                        if (dm.button_text or "").strip() and (
                            dm.button_url or ""
                        ).strip():
                            kb = InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [
                                        InlineKeyboardButton(
                                            text=dm.button_text,
                                            url=dm.button_url,
                                        )
                                    ]
                                ]
                            )
                        text_body = dm.text or ""
                        mtype = (dm.media_type or "none").lower()
                        mid = dm.media_file_id

                        try:
                            if mtype == "video" and mid:
                                await telegram_with_flood_retry(
                                    lambda: bot.send_video(
                                        uid,
                                        video=mid,
                                        caption=text_body or None,
                                        reply_markup=kb,
                                    )
                                )
                            elif mtype == "photo" and mid:
                                await telegram_with_flood_retry(
                                    lambda: bot.send_photo(
                                        uid,
                                        photo=mid,
                                        caption=text_body or None,
                                        reply_markup=kb,
                                    )
                                )
                            else:
                                await telegram_with_flood_retry(
                                    lambda: bot.send_message(
                                        uid, text_body, reply_markup=kb
                                    )
                                )
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
                            nxt = (
                                await session.execute(
                                    select(DripMessage).where(
                                        DripMessage.step == prog.current_step,
                                        DripMessage.is_active.is_(True),
                                    )
                                )
                            ).scalar_one_or_none()
                            if not nxt:
                                prog.is_active = False
                                prog.next_send_at = None
                            else:
                                prog.next_send_at = now + timedelta(
                                    hours=nxt.delay_hours or 24
                                )
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
                    text(
                        "SELECT id FROM content_queue "
                        "WHERE status = 'pending' AND scheduled_at <= :now "
                        "ORDER BY scheduled_at ASC"
                    ),
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
                        row = (
                            await session.execute(
                                text(
                                    "SELECT id, text, media_id, price, post_type, channel, "
                                    "button_text, button_url FROM content_queue "
                                    "WHERE id = :id AND status = 'pending' "
                                    "AND scheduled_at <= :now"
                                ),
                                {"id": post_id, "now": now},
                            )
                        ).mappings().first()
                        if not row:
                            continue

                        post = dict(row)
                        ch = (post.get("channel") or "free").lower()
                        target = (
                            PAID_CHANNEL_ID if ch == "paid" else FREE_CHANNEL_ID
                        )
                        btn_t = post.get("button_text")
                        btn_u = post.get("button_url")
                        reply_markup = None
                        if (btn_t or "").strip() and (btn_u or "").strip():
                            reply_markup = InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [
                                        InlineKeyboardButton(
                                            text=btn_t,
                                            url=btn_u,
                                        )
                                    ]
                                ]
                            )

                        try:
                            if post.get("post_type") == "paid":
                                await telegram_with_flood_retry(
                                    lambda: bot.send_paid_media(
                                        chat_id=target,
                                        star_count=post["price"] or 0,
                                        media=[
                                            InputPaidMediaPhoto(
                                                media=post["media_id"]
                                            )
                                        ],
                                        caption=post.get("text"),
                                        reply_markup=reply_markup,
                                    )
                                )
                            else:
                                await telegram_with_flood_retry(
                                    lambda: bot.send_photo(
                                        chat_id=target,
                                        photo=post["media_id"],
                                        caption=post.get("text"),
                                        reply_markup=reply_markup,
                                    )
                                )
                        except Exception as e:
                            logging.error(f"Post error id={post_id}: {e}")
                            continue

                        await session.execute(
                            text(
                                "UPDATE content_queue SET status = 'published' WHERE id = :id"
                            ),
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
    # === Критические колонки для Telegram ID и новых полей ===
    await connection.execute(
        text("ALTER TABLE users ALTER COLUMN id TYPE BIGINT")
    )
    await connection.execute(
        text("ALTER TABLE users ADD COLUMN IF NOT EXISTS joined_at TIMESTAMP WITHOUT TIME ZONE")
    )
    await connection.execute(
        text("ALTER TABLE users ADD COLUMN IF NOT EXISTS has_gift_claimed BOOLEAN DEFAULT false")
    )

    # === Остальные миграции (из Cursor) ===
    await connection.execute(
        text("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_spent BIGINT DEFAULT 0")
    )
    await connection.execute(
        text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_vip BOOLEAN DEFAULT false")
    )
    await connection.execute(
        text("ALTER TABLE users ADD COLUMN IF NOT EXISTS blocked BOOLEAN DEFAULT false")
    )
    await connection.execute(
        text("ALTER TABLE content_queue ADD COLUMN IF NOT EXISTS channel TEXT DEFAULT 'free'")
    )
    await connection.execute(
        text("ALTER TABLE content_queue ADD COLUMN IF NOT EXISTS button_text TEXT")
    )
    await connection.execute(
        text("ALTER TABLE content_queue ADD COLUMN IF NOT EXISTS button_url TEXT")
    )

@dp.message(F.from_user.id == ADMIN_ID, F.photo | F.video)
async def admin_get_file_id(message: types.Message):
    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "photo"
    else:
        file_id = message.video.file_id
        media_type = "video"
    await message.reply(f"media_type: `{media_type}`\nfile_id: `{file_id}`", parse_mode="Markdown")

async def main():
    import uvicorn

    from api import create_api_app

    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)   # ← сначала создаём таблицы
        await run_migrations(c)                    # ← миграции пока закомментированы

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
        uv_cfg = uvicorn.Config(
            fastapi_app,
            host="0.0.0.0",
            port=PORT,
            log_level="info",
            loop="asyncio",
            install_signal_handlers=False,
        )
    except TypeError:
        uv_cfg = uvicorn.Config(
            fastapi_app,
            host="0.0.0.0",
            port=PORT,
            log_level="info",
            loop="asyncio",
        )
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
        logging.warning("Signal handlers not installed; graceful shutdown relies on Ctrl+C/OS defaults")

    await bot.delete_webhook(drop_pending_updates=True)

    poll_task = asyncio.create_task(dp.start_polling(bot))
    shutdown_wait = asyncio.create_task(shutdown_req.wait())
    await asyncio.wait(
        {poll_task, shutdown_wait},
        return_when=asyncio.FIRST_COMPLETED,
    )

    stop_workers.set()

    if not shutdown_wait.done():
        shutdown_wait.cancel()
    if not poll_task.done():
        poll_task.cancel()

    await asyncio.gather(
        shutdown_wait,
        poll_task,
        drip_task,
        posts_task,
        return_exceptions=True,
    )

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
