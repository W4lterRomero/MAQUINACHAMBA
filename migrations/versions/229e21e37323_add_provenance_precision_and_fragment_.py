"""add_provenance_precision_and_fragment_columns

Revision ID: 229e21e37323
Revises: e89a31a982df
Create Date: 2026-10-15 22:59:55.304402

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '229e21e37323'
down_revision: Union[str, Sequence[str], None] = 'e89a31a982df'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('skills', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_fragment', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('context_type', sa.String(length=30), nullable=True, server_default='declared'))
        batch_op.alter_column('proficiency', existing_type=sa.String(length=20), nullable=True)
        batch_op.alter_column('years_experience', existing_type=sa.Float(), nullable=True)

    with op.batch_alter_table('experiences', schema=None) as batch_op:
        batch_op.add_column(sa.Column('date_precision', sa.String(length=20), nullable=True, server_default='day'))
        batch_op.add_column(sa.Column('source_fragment', sa.Text(), nullable=True))
        batch_op.alter_column('start_date', existing_type=sa.Date(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('experiences', schema=None) as batch_op:
        batch_op.drop_column('source_fragment')
        batch_op.drop_column('date_precision')

    with op.batch_alter_table('skills', schema=None) as batch_op:
        batch_op.drop_column('context_type')
        batch_op.drop_column('source_fragment')
