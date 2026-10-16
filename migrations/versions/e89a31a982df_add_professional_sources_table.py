"""add_professional_sources_table

Revision ID: e89a31a982df
Revises: 3b8427cda858
Create Date: 2026-10-15 22:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite


# revision identifiers, used by Alembic.
revision: str = 'e89a31a982df'
down_revision: Union[str, Sequence[str], None] = '3b8427cda858'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'professional_sources',
        sa.Column('profile_id', sa.String(length=36), nullable=False),
        sa.Column('source_type', sa.String(length=50), nullable=False),
        sa.Column('source_name', sa.String(length=255), nullable=False),
        sa.Column('source_uri', sa.Text(), nullable=False),
        sa.Column('file_hash', sa.String(length=64), nullable=False),
        sa.Column('raw_text', sa.Text(), nullable=False),
        sa.Column('extracted_data', sqlite.JSON(), nullable=False),
        sa.Column('verification_status', sa.String(length=20), nullable=False),
        sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('verified_by', sa.String(length=50), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['user_profiles.id'], name=op.f('fk_professional_sources_profile_id_user_profiles')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_professional_sources'))
    )
    op.create_index(op.f('ix_professional_sources_profile_id'), 'professional_sources', ['profile_id'], unique=False)
    op.create_index(op.f('ix_professional_sources_tenant_id'), 'professional_sources', ['tenant_id'], unique=False)

    with op.batch_alter_table('skills', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_skills_source_id_professional_sources'), 'professional_sources', ['source_id'], ['id'])
        batch_op.create_index(batch_op.f('ix_skills_source_id'), ['source_id'], unique=False)

    with op.batch_alter_table('experiences', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_id', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_experiences_source_id_professional_sources'), 'professional_sources', ['source_id'], ['id'])
        batch_op.create_index(batch_op.f('ix_experiences_source_id'), ['source_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('experiences', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_experiences_source_id'))
        batch_op.drop_constraint(batch_op.f('fk_experiences_source_id_professional_sources'), type_='foreignkey')
        batch_op.drop_column('source_id')

    with op.batch_alter_table('skills', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_skills_source_id'))
        batch_op.drop_constraint(batch_op.f('fk_skills_source_id_professional_sources'), type_='foreignkey')
        batch_op.drop_column('source_id')

    op.drop_index(op.f('ix_professional_sources_tenant_id'), table_name='professional_sources')
    op.drop_index(op.f('ix_professional_sources_profile_id'), table_name='professional_sources')
    op.drop_table('professional_sources')
