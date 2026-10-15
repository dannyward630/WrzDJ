"""Add encrypted provider connections shared by DJ accounts.

Revision ID: 071
Revises: 070
"""

import sqlalchemy as sa

from alembic import op
from app.core.encryption import EncryptedText

revision = "071"
down_revision = "070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_provider_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_id", sa.String(length=50), nullable=False),
        sa.Column("account_display_name", sa.String(length=100), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("connection_state", sa.String(length=30), nullable=False),
        sa.Column("credential_type", sa.String(length=50), nullable=False),
        sa.Column("authorization_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("credential_payload", EncryptedText(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "provider_id", name="uq_user_provider"),
    )
    op.create_index(
        "ix_user_provider_connections_user_id", "user_provider_connections", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_user_provider_connections_user_id", table_name="user_provider_connections")
    op.drop_table("user_provider_connections")
