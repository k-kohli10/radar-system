"""add slack_message_ts to recommendations

Records the Slack message the RCA card was delivered as — NULL until delivered,
set once by feedback-service after chat.postMessage returns. Two jobs, and the
column serves both:

- **Delivery record.** Its presence means "this recommendation was delivered."
  feedback-service reads it under a row lock before posting: already set -> skip
  (a redelivery, or the loser of a concurrent race), NULL -> post then set it.
  That NULL-check under FOR UPDATE is the no-double-post mechanism.

- **Cross-row integrity (the UNIQUE).** No two recommendations may claim the same
  Slack message timestamp. That is the OTHER direction from "one delivery per
  recommendation" (which the single recommendation row already gives): it catches
  a bug that recorded one message ref against two RCAs. The UNIQUE is emphatically
  NOT what prevents a double post — two concurrent deliveries of one recommendation
  would post two DIFFERENT messages with two DIFFERENT timestamps, which do not
  collide. See feedback-service's delivery module docstring.

NOT in the implementation plan's DDL — added deliberately in Phase 9. The locked
"no new tables for the bot" decision is honored: this is a column on the row that
already owns the recommendation, not a parallel schema.

Safe on existing data: the column is nullable with no default, so every existing
recommendation gets NULL (correctly "not yet delivered"), and a UNIQUE index over
NULLs is fine — Postgres treats NULLs as distinct, so any number of undelivered
recommendations coexist.

Revision ID: 75ae7863ea57
Revises: eeb12a7f924c
Create Date: 2026-07-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "75ae7863ea57"
down_revision: str | None = "eeb12a7f924c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "recommendations",
        sa.Column("slack_message_ts", sa.String(length=64), nullable=True),
    )
    # A unique INDEX, matching idx_rec_one_per_incident's style (the repo uses
    # unique indexes, not named constraints, and has no naming convention set).
    op.create_index(
        "idx_rec_slack_ts",
        "recommendations",
        ["slack_message_ts"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_rec_slack_ts", table_name="recommendations")
    op.drop_column("recommendations", "slack_message_ts")
