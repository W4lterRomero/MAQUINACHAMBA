"""add_search_preferences_audit_logs_portal_auth_and_vacancy_fields

Revision ID: b1c2d3e4f5a6
Revises: a4b7c8d9e0f1
Create Date: 2026-10-27 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a4b7c8d9e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema with job_search_preferences, audit_logs, portal_authorizations, and remaining vacancy fields."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    # 1. Crear tabla job_search_preferences si no existe
    if 'job_search_preferences' not in existing_tables:
        op.create_table(
            'job_search_preferences',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('tenant_id', sa.String(length=36), nullable=False),
            sa.Column('desired_job_titles', sqlite.JSON(), nullable=True),
            sa.Column('skills', sqlite.JSON(), nullable=True),
            sa.Column('locations', sqlite.JSON(), nullable=True),
            sa.Column('remote_modalities', sqlite.JSON(), nullable=True),
            sa.Column('experience_level', sa.String(length=50), nullable=False, server_default='any'),
            sa.Column('salary_min', sa.Numeric(precision=10, scale=2), nullable=True),
            sa.Column('salary_currency', sa.String(length=3), nullable=False, server_default='USD'),
            sa.Column('excluded_keywords', sqlite.JSON(), nullable=True),
            sa.Column('excluded_companies', sqlite.JSON(), nullable=True),
            sa.Column('is_periodic_enabled', sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column('interval_hours', sa.Integer(), nullable=False, server_default='24'),
            sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_status', sa.String(length=30), nullable=False, server_default='idle'),
            sa.Column('last_error', sa.Text(), nullable=True),
            sa.Column('last_results_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_new_jobs_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('source_access_authorized', sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column('authorized_sources', sqlite.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id', name=op.f('pk_job_search_preferences')),
        )
        op.create_index(op.f('ix_job_search_preferences_tenant_id'), 'job_search_preferences', ['tenant_id'], unique=False)

    # 2. Crear tabla audit_logs si no existe
    if 'audit_logs' not in existing_tables:
        op.create_table(
            'audit_logs',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('tenant_id', sa.String(length=36), nullable=False),
            sa.Column('action', sa.String(length=100), nullable=False),
            sa.Column('entity_type', sa.String(length=50), nullable=False),
            sa.Column('entity_id', sa.String(length=36), nullable=True),
            sa.Column('actor', sa.String(length=50), nullable=False, server_default='user'),
            sa.Column('details', sqlite.JSON(), nullable=True),
            sa.Column('ip_address', sa.String(length=45), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs')),
        )
        op.create_index(op.f('ix_audit_logs_tenant_id'), 'audit_logs', ['tenant_id'], unique=False)
        op.create_index(op.f('ix_audit_logs_action'), 'audit_logs', ['action'], unique=False)
        op.create_index(op.f('ix_audit_logs_entity_type'), 'audit_logs', ['entity_type'], unique=False)
        op.create_index(op.f('ix_audit_logs_entity_id'), 'audit_logs', ['entity_id'], unique=False)

    # 3. Crear tabla portal_authorizations si no existe
    if 'portal_authorizations' not in existing_tables:
        op.create_table(
            'portal_authorizations',
            sa.Column('id', sa.String(length=36), nullable=False),
            sa.Column('tenant_id', sa.String(length=36), nullable=False),
            sa.Column('portal_name', sa.String(length=50), nullable=False),
            sa.Column('action', sa.String(length=50), nullable=False),
            sa.Column('authorized', sa.Boolean(), nullable=False, server_default=sa.text('0')),
            sa.Column('authorized_by', sa.String(length=50), nullable=False, server_default='user'),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint('id', name=op.f('pk_portal_authorizations')),
        )
        op.create_index(op.f('ix_portal_authorizations_tenant_id'), 'portal_authorizations', ['tenant_id'], unique=False)
        op.create_index(op.f('ix_portal_authorizations_portal_name'), 'portal_authorizations', ['portal_name'], unique=False)
        op.create_index(op.f('ix_portal_authorizations_action'), 'portal_authorizations', ['action'], unique=False)

    # 4. Agregar columnas nuevas a vacancies de forma aditiva
    if 'vacancies' in existing_tables:
        vac_cols = [c['name'] for c in inspector.get_columns('vacancies')]
        with op.batch_alter_table('vacancies', schema=None) as batch_op:
            if 'fetched_at' not in vac_cols:
                batch_op.add_column(sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=True))
            if 'is_real_search_result' not in vac_cols:
                batch_op.add_column(sa.Column('is_real_search_result', sa.Boolean(), nullable=False, server_default=sa.text('0')))
            if 'raw_source_payload' not in vac_cols:
                batch_op.add_column(sa.Column('raw_source_payload', sqlite.JSON(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if 'portal_authorizations' in existing_tables:
        op.drop_table('portal_authorizations')
    if 'audit_logs' in existing_tables:
        op.drop_table('audit_logs')
    if 'job_search_preferences' in existing_tables:
        op.drop_table('job_search_preferences')
