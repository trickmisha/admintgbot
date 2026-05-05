import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from datetime import datetime, UTC
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, AsyncGenerator, Literal, TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from main import is_admin

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def telegram_with_flood_retry(factory: Callable[[], Awaitable[T]]) -> T:
    while True:
        try:
            return await factory()
        except TelegramRetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 0) -> dict[str, str]:
    parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    hash_received = parsed.pop("hash", None)
    if not hash_received:
        raise ValueError("missing hash")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode(), hashlib.sha256
    ).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(calculated, hash_received):
        raise ValueError("invalid hash")
    auth_date_raw = parsed.get("auth_date")
    if max_age_seconds and auth_date_raw:
        auth_date = int(auth_date_raw)
        if int(time.time()) - auth_date > max_age_seconds:
            raise ValueError("auth_date expired")
    return parsed


class StatsResponse(BaseModel):
    total_users: int
    vip_count: int
    in_drip: int
    blocked_count: int
    conversion: float


class DripStepBody(BaseModel):
    text: str | None = None
    media_file_id: str | None = None
    media_type: str = "none"
    button_text: str | None = None
    button_url: str | None = None
    delay_hours: int = Field(default=24, ge=0, le=8760)
    is_active: bool = True


class DripStepOut(BaseModel):
    step: int
    text: str | None
    media_file_id: str | None
    media_type: str
    button_text: str | None
    button_url: str | None
    delay_hours: int
    is_active: bool


class SettingsBody(BaseModel):
    welcome_text: str | None = None
    button1_text: str | None = None
    button1_url: str | None = None
    button2_text: str | None = None
    button2_url: str | None = None


class SettingsOut(BaseModel):
    welcome_text: str
    button1_text: str
    button1_url: str
    button2_text: str
    button2_url: str
    paid_channel_link: str


class BroadcastBody(BaseModel):
    text: str | None = None
    media_file_id: str | None = None
    target: Literal["all", "free", "vip"]


class PostCreateBody(BaseModel):
    channel: Literal["free", "paid"]
    text: str | None = None
    media_id: str | None = None
    button_text: str | None = None
    button_url: str | None = None
    scheduled_at: datetime | None = None
    publish_now: bool = False


class PostOut(BaseModel):
    id: int
    channel: str
    text: str | None
    media_id: str | None
    button_text: str | None
    button_url: str | None
    scheduled_at: datetime | None
    status: str


def create_api_app(
    *,
    bot: Bot,
    bot_token: str,
    admin_id: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> FastAPI:
    app = FastAPI(title="TG Bot Admin API")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    async def get_db() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    async def require_admin(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        logger.info("AUTH HEADER: %s", repr(authorization))
        if not authorization or not authorization.lower().startswith("tma "):
            raise HTTPException(status_code=401, detail="Missing or invalid Authorization")
        init_data = authorization[4:].strip()
        try:
            data = validate_init_data(init_data, bot_token)
        except ValueError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        user_raw = data.get("user")
        if not user_raw:
            raise HTTPException(status_code=401, detail="Missing user in init data")
        try:
            user = json.loads(user_raw)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=401, detail="Invalid user json") from e
        if int(user.get("id", 0)) != admin_id:
            raise HTTPException(status_code=403, detail="Admin only")
        return user

    Admin = Annotated[dict[str, Any], Depends(require_admin)]
    Db = Annotated[AsyncSession, Depends(get_db)]

    @app.api_route("/", methods=["GET", "HEAD"])
    async def health():
        return {"ok": True}

    @app.get("/api/stats", response_model=StatsResponse)
    async def api_stats(_admin: Admin, db: Db):
        total = (await db.execute(text("SELECT count(*) FROM users"))).scalar() or 0
        vip = (
            await db.execute(text("SELECT count(*) FROM users WHERE is_vip = true"))
        ).scalar() or 0
        in_drip = (
            await db.execute(
                text("SELECT count(*) FROM drip_progress WHERE is_active = true")
            )
        ).scalar() or 0
        blocked = (
            await db.execute(text("SELECT count(*) FROM users WHERE blocked = true"))
        ).scalar() or 0
        conv = (float(vip) / float(total) * 100.0) if total else 0.0
        return StatsResponse(
            total_users=int(total),
            vip_count=int(vip),
            in_drip=int(in_drip),
            blocked_count=int(blocked),
            conversion=round(conv, 2),
        )

    @app.get("/api/drip", response_model=list[DripStepOut])
    async def api_drip_get(_admin: Admin, db: Db):
        rows = (
            await db.execute(
                text(
                    "SELECT step, text, media_file_id, media_type, button_text, "
                    "button_url, delay_hours, is_active FROM drip_messages ORDER BY step"
                )
            )
        ).mappings().all()
        by_step = {int(r["step"]): dict(r) for r in rows}
        out: list[DripStepOut] = []
        for s in range(5):
            r = by_step.get(s)
            if r:
                out.append(
                    DripStepOut(
                        step=s,
                        text=r.get("text"),
                        media_file_id=r.get("media_file_id"),
                        media_type=r.get("media_type") or "none",
                        button_text=r.get("button_text"),
                        button_url=r.get("button_url"),
                        delay_hours=int(r.get("delay_hours") or 24),
                        is_active=bool(r.get("is_active")),
                    )
                )
            else:
                out.append(
                    DripStepOut(
                        step=s,
                        text=None,
                        media_file_id=None,
                        media_type="none",
                        button_text=None,
                        button_url=None,
                        delay_hours=24,
                        is_active=True,
                    )
                )
        return out

    @app.put("/api/drip/{step}", response_model=DripStepOut)
    async def api_drip_put(
        step: int,
        body: DripStepBody,
        _admin: Admin,
        db: Db,
    ):
        if step < 0 or step > 4:
            raise HTTPException(status_code=400, detail="step must be 0-4")
        await db.execute(
            text(
                """
                INSERT INTO drip_messages
                  (step, text, media_file_id, media_type, button_text, button_url, delay_hours, is_active)
                VALUES
                  (:step, :text, :mfi, :mt, :bt, :bu, :dh, :ia)
                ON CONFLICT (step) DO UPDATE SET
                  text = EXCLUDED.text,
                  media_file_id = EXCLUDED.media_file_id,
                  media_type = EXCLUDED.media_type,
                  button_text = EXCLUDED.button_text,
                  button_url = EXCLUDED.button_url,
                  delay_hours = EXCLUDED.delay_hours,
                  is_active = EXCLUDED.is_active
                """
            ),
            {
                "step": step,
                "text": body.text,
                "mfi": body.media_file_id,
                "mt": body.media_type or "none",
                "bt": body.button_text,
                "bu": body.button_url,
                "dh": body.delay_hours,
                "ia": body.is_active,
            },
        )
        await db.commit()
        row = (
            await db.execute(
                text(
                    "SELECT step, text, media_file_id, media_type, button_text, "
                    "button_url, delay_hours, is_active FROM drip_messages WHERE step = :s"
                ),
                {"s": step},
            )
        ).mappings().one()
        return DripStepOut(
            step=int(row["step"]),
            text=row.get("text"),
            media_file_id=row.get("media_file_id"),
            media_type=row.get("media_type") or "none",
            button_text=row.get("button_text"),
            button_url=row.get("button_url"),
            delay_hours=int(row.get("delay_hours") or 24),
            is_active=bool(row.get("is_active")),
        )

    async def _load_settings(db: AsyncSession) -> dict[str, str]:
        rows = (await db.execute(text("SELECT key, value FROM settings"))).mappings().all()
        return {str(r["key"]): (r["value"] or "") for r in rows}

    @app.get("/api/settings", response_model=SettingsOut)
    async def api_settings_get(_admin: Admin, db: Db):
        m = await _load_settings(db)
        return SettingsOut(
            welcome_text=m.get("welcome_text", ""),
            button1_text=m.get("button1_text", ""),
            button1_url=m.get("button1_url", ""),
            button2_text=m.get("button2_text", ""),
            button2_url=m.get("button2_url", ""),
            paid_channel_link=m.get("paid_channel_link", ""),
        )

    @app.put("/api/settings", response_model=SettingsOut)
    async def api_settings_put(body: SettingsBody, _admin: Admin, db: Db):
        updates: dict[str, str | None] = {
            "welcome_text": body.welcome_text,
            "button1_text": body.button1_text,
            "button1_url": body.button1_url,
            "button2_text": body.button2_text,
            "button2_url": body.button2_url,
        }
        for key, val in updates.items():
            if val is None:
                continue
            await db.execute(
                text(
                    "INSERT INTO settings (key, value) VALUES (:k, :v) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                ),
                {"k": key, "v": val},
            )
        await db.commit()
        m = await _load_settings(db)
        return SettingsOut(
            welcome_text=m.get("welcome_text", ""),
            button1_text=m.get("button1_text", ""),
            button1_url=m.get("button1_url", ""),
            button2_text=m.get("button2_text", ""),
            button2_url=m.get("button2_url", ""),
            paid_channel_link=m.get("paid_channel_link", ""),
        )

    @app.post("/api/broadcast")
    async def api_broadcast(body: BroadcastBody, _admin: Admin, db: Db):
        if not (body.text or "").strip() and not (body.media_file_id or "").strip():
            raise HTTPException(
                status_code=400, detail="text or media_file_id is required"
            )
        now = datetime.now(UTC).replace(tzinfo=None)
        bid = (
            await db.execute(
                text(
                    "INSERT INTO broadcasts (text, media_file_id, target, status, created_at) "
                    "VALUES (:t, :m, :tg, 'sending', :ca) RETURNING id"
                ),
                {
                    "t": body.text,
                    "m": body.media_file_id,
                    "tg": body.target,
                    "ca": now,
                },
            )
        ).scalar_one()
        await db.commit()

        if body.target == "all":
            res = await db.execute(text("SELECT id FROM users WHERE blocked = false"))
        elif body.target == "free":
            res = await db.execute(
                text("SELECT id FROM users WHERE blocked = false AND is_vip = false")
            )
        else:
            res = await db.execute(
                text("SELECT id FROM users WHERE blocked = false AND is_vip = true")
            )
        user_ids = [int(x) for x in res.scalars().all()]
        total = len(user_ids)
        sent = 0
        try:
            for uid in user_ids:
                try:
                    if body.media_file_id:
                        await telegram_with_flood_retry(
                            lambda u=uid: bot.send_photo(
                                u,
                                body.media_file_id,
                                caption=body.text or None,
                            )
                        )
                    else:
                        await telegram_with_flood_retry(
                            lambda u=uid: bot.send_message(u, body.text or "")
                        )
                    sent += 1
                except TelegramForbiddenError:
                    await db.execute(
                        text("UPDATE users SET blocked = true WHERE id = :id"),
                        {"id": uid},
                    )
                    await db.commit()
                except Exception as e:
                    logger.error("broadcast to %s: %s", uid, e)
        finally:
            if total == 0:
                status = "completed"
            elif sent >= total:
                status = "completed"
            elif sent == 0:
                status = "failed"
            else:
                status = "partial"

            await db.execute(
                text("UPDATE broadcasts SET status = :st WHERE id = :id"),
                {"st": status, "id": bid},
            )
            await db.commit()

        return {"sent": sent, "total": total, "status": status}

    @app.get("/api/posts", response_model=list[PostOut])
    async def api_posts_list(_admin: Admin, db: Db):
        rows = (
            await db.execute(
                text(
                    "SELECT id, channel, text, media_id, button_text, button_url, "
                    "scheduled_at, status FROM content_queue "
                    "WHERE status = 'pending' ORDER BY scheduled_at ASC NULLS LAST"
                )
            )
        ).mappings().all()
        return [
            PostOut(
                id=int(r["id"]),
                channel=str(r.get("channel") or "free"),
                text=r.get("text"),
                media_id=r.get("media_id"),
                button_text=r.get("button_text"),
                button_url=r.get("button_url"),
                scheduled_at=r.get("scheduled_at"),
                status=str(r.get("status") or "pending"),
            )
            for r in rows
        ]

    @app.post("/api/posts", response_model=PostOut)
    async def api_posts_create(body: PostCreateBody, _admin: Admin, db: Db):
        if not (body.text or "").strip() and not (body.media_id or "").strip():
            raise HTTPException(status_code=400, detail="text or media_id is required")
        now = datetime.now(UTC).replace(tzinfo=None)
        if body.publish_now:
            sched = now
        elif body.scheduled_at:
            sched = body.scheduled_at
            if sched.tzinfo is not None:
                sched = sched.astimezone(UTC).replace(tzinfo=None)
        else:
            raise HTTPException(status_code=400, detail="scheduled_at or publish_now is required")
        post_type = "free"
        await db.execute(
            text(
                "INSERT INTO content_queue "
                "(text, media_id, price, post_type, channel, button_text, button_url, scheduled_at, status) "
                "VALUES (:text, :mid, 0, :pt, :ch, :bt, :bu, :sa, 'pending')"
            ),
            {
                "text": body.text,
                "mid": body.media_id,
                "pt": post_type,
                "ch": body.channel,
                "bt": body.button_text,
                "bu": body.button_url,
                "sa": sched,
            },
        )
        await db.commit()
        row = (
            await db.execute(
                text(
                    "SELECT id, channel, text, media_id, button_text, button_url, "
                    "scheduled_at, status FROM content_queue ORDER BY id DESC LIMIT 1"
                )
            )
        ).mappings().one()
        return PostOut(
            id=int(row["id"]),
            channel=str(row.get("channel") or "free"),
            text=row.get("text"),
            media_id=row.get("media_id"),
            button_text=row.get("button_text"),
            button_url=row.get("button_url"),
            scheduled_at=row.get("scheduled_at"),
            status=str(row.get("status") or "pending"),
        )

    @app.delete("/api/posts/{post_id}")
    async def api_posts_delete(post_id: int, _admin: Admin, db: Db):
        r = await db.execute(
            text("DELETE FROM content_queue WHERE id = :id RETURNING id"),
            {"id": post_id},
        )
        await db.commit()
        if r.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Not found")
        return {"ok": True}

    return app
