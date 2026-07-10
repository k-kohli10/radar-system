"""make foreign key constraints deferrable

Deferring FK checks to COMMIT lets a service INSERT a parent and its child in the
same transaction in any order (ingestion and the watcher write an incident and
its alert together) without manual flush ordering. The models are FK-column-only
by design — no ORM relationships, to avoid async lazy-load I/O — so nothing else
orders these inserts. Applying it to every parent->child FK means insert order
never matters anywhere in the pipeline.

Revision ID: 0f503c715266
Revises: a43594b399c6
Create Date: 2026-07-09 17:30:28.457930

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0f503c715266"
down_revision: str | None = "a43594b399c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every parent->child foreign key in the schema. Making all of them deferrable
# (not only incident->alert) covers every agent that writes a parent and child
# in one transaction, now and later.
_FOREIGN_KEYS: list[tuple[str, str]] = [
    ("alerts", "alerts_incident_id_fkey"),
    ("investigation_plans", "investigation_plans_incident_id_fkey"),
    ("recommendations", "recommendations_incident_id_fkey"),
    ("recommendations", "recommendations_plan_id_fkey"),
    ("feedback", "feedback_recommendation_id_fkey"),
    ("feedback", "feedback_incident_id_fkey"),
]


def upgrade() -> None:
    for table, constraint in _FOREIGN_KEYS:
        op.execute(
            f"ALTER TABLE {table} "
            f"ALTER CONSTRAINT {constraint} DEFERRABLE INITIALLY DEFERRED"
        )


def downgrade() -> None:
    for table, constraint in _FOREIGN_KEYS:
        op.execute(f"ALTER TABLE {table} ALTER CONSTRAINT {constraint} NOT DEFERRABLE")
