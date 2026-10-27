"""add_multisource_vacancies_and_import_batches

Revision ID: a4b7c8d9e0f1
Revises: 1982c954031f
Create Date: 2026-10-18 22:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = 'a4b7c8d9e0f1'
down_revision: Union[str, Sequence[str], None] = '1982c954031f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema with multi-source vacancy fields and import_batches table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. Crear tabla import_batches si no existe
    if 'import_batches' not in existing_tables:
        op.create_table(
            'import_batches',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('tenant_id', sa.String(length=36), nullable=False),
            sa.Column('provider', sa.String(length=50), nullable=False, server_default='manual'),
            sa.Column('import_type', sa.String(length=50), nullable=False, server_default='manual_paste'),
            sa.Column('status', sa.String(length=50), nullable=False, server_default='completed'),
            sa.Column('vacancies_found', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('vacancies_imported', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('vacancies_deduplicated', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('raw_content_preview', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id', name=op.f('pk_import_batches')),
        )
        op.create_index(op.f('ix_import_batches_tenant_id'), 'import_batches', ['tenant_id'], unique=False)

    # 2. Agregar columnas a vacancies de forma aditiva
    if 'vacancies' in existing_tables:
        vac_cols = [c['name'] for c in inspector.get_columns('vacancies')]
        with op.batch_alter_table('vacancies', schema=None) as batch_op:
            if 'allowed_countries' not in vac_cols:
                batch_op.add_column(sa.Column('allowed_countries', sqlite.JSON(), nullable=True))
            if 'technical_requirements' not in vac_cols:
                batch_op.add_column(sa.Column('technical_requirements', sqlite.JSON(), nullable=True))
            if 'experience_level' not in vac_cols:
                batch_op.add_column(sa.Column('experience_level', sa.String(length=50), nullable=True, server_default='unknown'))
            if 'discovered_date' not in vac_cols:
                batch_op.add_column(sa.Column('discovered_date', sa.Date(), nullable=True))
            if 'original_snippet' not in vac_cols:
                batch_op.add_column(sa.Column('original_snippet', sa.Text(), nullable=True))
            if 'verification_status' not in vac_cols:
                batch_op.add_column(sa.Column('verification_status', sa.String(length=50), nullable=True, server_default='pending_verification'))
            if 'unknown_fields' not in vac_cols:
                batch_op.add_column(sa.Column('unknown_fields', sqlite.JSON(), nullable=True))
            if 'sources_detail' not in vac_cols:
                batch_op.add_column(sa.Column('sources_detail', sqlite.JSON(), nullable=True))
            if 'import_batch_id' not in vac_cols:
                batch_op.add_column(sa.Column('import_batch_id', sa.String(length=36), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if 'import_batches' in existing_tables:
        op.drop_table('import_batches')
