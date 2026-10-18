"""Хранилище диалогов: SQLite локально, Postgres на Render. Синхронный SQLAlchemy, вызывается через asyncio.to_thread."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, create_engine,
                        func, insert, select, update)
from sqlalchemy.exc import IntegrityError

metadata = MetaData()

conversations = Table(
    "conversations", metadata,
    Column("user_id", String(64), primary_key=True),
    Column("state", Text, nullable=False, default="{}"),
    Column("paused_until", DateTime, nullable=True),
    Column("manual_mode", Boolean, nullable=False, default=False),
    Column("opted_out", Boolean, nullable=False, default=False),
    Column("followup_sent", Boolean, nullable=False, default=False),
    Column("last_user_at", DateTime, nullable=True),
    Column("last_bot_at", DateTime, nullable=True),
    Column("last_manager_at", DateTime, nullable=True),
    Column("created_at", DateTime, nullable=False, default=lambda: utcnow()),
)

messages = Table(
    "messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", String(64), nullable=False, index=True),
    Column("role", String(16), nullable=False),  # user | assistant | manager
    Column("content", Text, nullable=False, default=""),
    Column("mid", String(255), nullable=True, unique=True),
    Column("processed", Boolean, nullable=False, default=True),
    Column("created_at", DateTime, nullable=False, default=lambda: utcnow()),
)


def utcnow() -> datetime:
    """Наивное UTC-время (одинаково ведёт себя в SQLite и Postgres)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Store:
    def __init__(self, url: str):
        kwargs: dict[str, Any] = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(url, **kwargs)
        metadata.create_all(self.engine)

    # ------------------------------------------------------------- диалоги
    def get_conv(self, user_id: str) -> dict:
        with self.engine.begin() as c:
            row = c.execute(select(conversations).where(conversations.c.user_id == user_id)).mappings().first()
            if row is None:
                try:
                    c.execute(insert(conversations).values(user_id=user_id, state="{}", manual_mode=False,
                                                           opted_out=False, followup_sent=False,
                                                           created_at=utcnow()))
                except IntegrityError:
                    pass
                row = c.execute(select(conversations).where(conversations.c.user_id == user_id)).mappings().first()
            d = dict(row)
            d["state"] = json.loads(d["state"] or "{}")
            return d

    def update_conv(self, user_id: str, **fields) -> None:
        if "state" in fields:
            fields["state"] = json.dumps(fields["state"], ensure_ascii=False)
        with self.engine.begin() as c:
            c.execute(update(conversations).where(conversations.c.user_id == user_id).values(**fields))

    # ------------------------------------------------------------- сообщения
    def add_message(self, user_id: str, role: str, content: str, mid: str | None = None,
                    processed: bool = True, at: datetime | None = None) -> bool:
        """False — если сообщение с таким mid уже есть (повтор вебхука)."""
        try:
            with self.engine.begin() as c:
                c.execute(insert(messages).values(user_id=user_id, role=role, content=content, mid=mid,
                                                  processed=processed, created_at=at or utcnow()))
            return True
        except IntegrityError:
            return False

    def has_mid(self, mid: str) -> bool:
        with self.engine.begin() as c:
            return c.execute(select(messages.c.id).where(messages.c.mid == mid)).first() is not None

    def message_role_by_mid(self, mid: str) -> str | None:
        with self.engine.begin() as c:
            r = c.execute(select(messages.c.role).where(messages.c.mid == mid)).first()
            return r[0] if r else None

    def unprocessed(self, user_id: str) -> list[dict]:
        with self.engine.begin() as c:
            rows = c.execute(select(messages).where(messages.c.user_id == user_id, messages.c.role == "user",
                                                    messages.c.processed.is_(False))
                             .order_by(messages.c.id)).mappings().all()
            return [dict(r) for r in rows]

    def mark_processed(self, ids: list[int]) -> None:
        if not ids:
            return
        with self.engine.begin() as c:
            c.execute(update(messages).where(messages.c.id.in_(ids)).values(processed=True))

    def mark_all_processed_before(self, user_id: str, before: datetime) -> None:
        with self.engine.begin() as c:
            c.execute(update(messages).where(messages.c.user_id == user_id, messages.c.role == "user",
                                             messages.c.processed.is_(False), messages.c.created_at <= before)
                      .values(processed=True))

    def history(self, user_id: str, limit: int) -> list[dict]:
        with self.engine.begin() as c:
            rows = c.execute(select(messages).where(messages.c.user_id == user_id)
                             .order_by(messages.c.id.desc()).limit(limit)).mappings().all()
            return [dict(r) for r in reversed(rows)]

    def last_message(self, user_id: str) -> dict | None:
        h = self.history(user_id, 1)
        return h[0] if h else None

    # ------------------------------------------------------------- планировщик
    def convs_to_resume(self, now: datetime) -> list[str]:
        """Пауза менеджера закончилась, а от клиента есть необработанные сообщения."""
        with self.engine.begin() as c:
            pending = select(messages.c.user_id).where(messages.c.role == "user",
                                                       messages.c.processed.is_(False)).distinct()
            rows = c.execute(select(conversations.c.user_id).where(
                conversations.c.paused_until.is_not(None), conversations.c.paused_until <= now,
                conversations.c.manual_mode.is_(False), conversations.c.user_id.in_(pending))).all()
            return [r[0] for r in rows]

    def clear_expired_pauses(self, now: datetime) -> None:
        with self.engine.begin() as c:
            c.execute(update(conversations).where(conversations.c.paused_until.is_not(None),
                                                  conversations.c.paused_until <= now)
                      .values(paused_until=None))

    def convs_for_followup(self, now: datetime, after: timedelta, window: timedelta) -> list[str]:
        with self.engine.begin() as c:
            rows = c.execute(select(conversations.c.user_id).where(
                conversations.c.followup_sent.is_(False),
                conversations.c.opted_out.is_(False),
                conversations.c.manual_mode.is_(False),
                conversations.c.paused_until.is_(None),
                conversations.c.last_bot_at.is_not(None),
                conversations.c.last_user_at.is_not(None),
                conversations.c.last_bot_at > conversations.c.last_user_at,
                conversations.c.last_bot_at <= now - after,
                conversations.c.last_user_at >= now - window,
            )).all()
            out = []
            for (uid,) in rows:
                conv = c.execute(select(conversations.c.last_manager_at, conversations.c.last_user_at)
                                 .where(conversations.c.user_id == uid)).first()
                if conv and conv[0] and conv[0] > conv[1]:
                    continue  # менеджер уже вёл разговор после клиента
                out.append(uid)
            return out

    def stats(self) -> dict:
        with self.engine.begin() as c:
            return {
                "conversations": c.execute(select(func.count()).select_from(conversations)).scalar(),
                "messages": c.execute(select(func.count()).select_from(messages)).scalar(),
            }
