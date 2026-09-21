"""SQLAlchemy ORM models.

Tables are registered on ``Base.metadata`` (see ``mirenai.db``) and created at
startup with ``create_all``. Timestamps default to ``datetime('now', 'localtime')``
so SQLite records them in the container's local time zone (``TZ``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mirenai.db import Base

# datetime('now','localtime') resolves against the container's TZ env var.
_LOCAL_NOW = text("(datetime('now', 'localtime'))")


class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("client", "domain", name="uq_policies_client_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    override_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_ttl: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class Upstream(Base):
    __tablename__ = "upstreams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str] = mapped_column(String(64), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=53)
    protocol: Mapped[str] = mapped_column(String(8), nullable=False, default="udp")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class QueryLog(Base):
    __tablename__ = "query_log"
    __table_args__ = (
        UniqueConstraint("client", "domain", "qtype", name="uq_query_log_client_domain_qtype"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    qtype: Mapped[str] = mapped_column(String(16), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    last_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )
