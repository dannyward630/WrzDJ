"""Encrypted DJ account credentials for cloud music providers."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.encryption import EncryptedText
from app.core.time import utcnow
from app.models.base import Base


class UserProviderConnection(Base):
    """One provider connection per DJ, retaining disconnected rows as tombstones."""

    __tablename__ = "user_provider_connections"
    __table_args__ = (UniqueConstraint("user_id", "provider_id", name="uq_user_provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider_id: Mapped[str] = mapped_column(String(50), nullable=False)
    account_display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    connection_state: Mapped[str] = mapped_column(String(30), nullable=False, default="connected")
    credential_type: Mapped[str] = mapped_column(String(50), nullable=False)
    credential_payload: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
