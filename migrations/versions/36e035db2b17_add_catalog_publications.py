"""Add explicit catalog publications."""

import sqlalchemy as sa
from alembic import op

revision = "36e035db2b17"
down_revision = "a7cfdd919de6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_publications",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("city", sa.String(80), nullable=True),
        sa.Column("direction", sa.String(80), nullable=False),
        sa.Column("cover_photo_id", sa.String(36), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["event_id"], ["events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cover_photo_id"], ["photos.id"], ondelete="SET NULL"),
    )


def downgrade() -> None:
    op.drop_table("catalog_publications")
