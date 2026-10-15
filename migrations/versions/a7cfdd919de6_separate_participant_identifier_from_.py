"""Separate participant identifier from invitation access key."""

from alembic import op

revision = "a7cfdd919de6"
down_revision = "8b92596f140f"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("activities", "invitation_key", new_column_name="participant_id")
    op.execute(
        "UPDATE activities SET participant_id = encode(sha256(convert_to(participant_id, 'UTF8')), 'hex')"
    )
    op.execute("ALTER INDEX ix_activities_invitation_key RENAME TO ix_activities_participant_id")


def downgrade():
    op.execute(
        "UPDATE activities AS a SET participant_id = t.invitation_key FROM attempts AS t WHERE a.attempt_id = t.id"
    )
    op.alter_column("activities", "participant_id", new_column_name="invitation_key")
    op.execute("ALTER INDEX ix_activities_participant_id RENAME TO ix_activities_invitation_key")
