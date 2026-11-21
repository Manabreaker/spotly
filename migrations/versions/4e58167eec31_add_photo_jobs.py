"""Queue photo processing jobs in PostgreSQL."""

import sqlalchemy as sa
from alembic import op

revision = "4e58167eec31"
down_revision = "a7cfdd919de6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "photo_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("invitation_key", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("upload_id", sa.String(36), nullable=False),
        sa.Column("session_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("original", sa.String(300), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("claim_token", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("process_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("invitation_key", "upload_id"),
    )
    op.create_index("ix_photo_jobs_attempt_id", "photo_jobs", ["attempt_id"])
    op.create_index(
        "ix_photo_jobs_claim",
        "photo_jobs",
        ["created_at", "id"],
        postgresql_where=sa.text("status IN ('queued', 'processing')"),
    )


def downgrade():
    op.drop_table("photo_jobs")
