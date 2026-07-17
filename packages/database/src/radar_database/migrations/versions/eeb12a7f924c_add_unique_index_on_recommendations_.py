"""add unique index on recommendations incident_id

One recommendation per incident, mirroring idx_plans_one_per_incident.

NOT in the implementation plan's DDL — added deliberately in Phase 7. An incident
gets exactly ONE root-cause analysis: a second recommendation means the engineer
receives two contradictory Slack cards for one incident and cannot tell which is
current. The reasoner is also the only stage that spends money, so the redelivery
that would produce a duplicate is the same one that charges OpenAI twice.

The reasoner's dispatch timeout is ordered against its LLM budget precisely so that
race cannot occur (radar_common.timeouts, asserted at import). This index is the
backstop for when it happens anyway: a crash mid-call, a manual requeue of a dead
letter, an operator replaying an event. With it, the guarantee rests on the SCHEMA
rather than on two numbers staying in the correct order forever.

Safe on existing data: recommendations is empty until the reasoner ships, and this
migration runs before it does. On a table that already held duplicates the index
creation would fail loudly, which is the correct behaviour — it would mean two RCAs
had already been issued for one incident.

Revision ID: eeb12a7f924c
Revises: 0f503c715266
Create Date: 2026-07-13 23:38:52.901114

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'eeb12a7f924c'
down_revision: str | None = '0f503c715266'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "idx_rec_one_per_incident",
        "recommendations",
        ["incident_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("idx_rec_one_per_incident", table_name="recommendations")
