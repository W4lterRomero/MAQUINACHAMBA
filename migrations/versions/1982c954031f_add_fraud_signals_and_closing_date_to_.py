"""add_fraud_signals_and_closing_date_to_vacancies

Revision ID: 1982c954031f
Revises: 229e21e37323
Create Date: 2026-10-16 20:50:41.557332

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = '1982c954031f'
down_revision: Union[str, Sequence[str], None] = '229e21e37323'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if 'api_key_configs' not in existing_tables:
        op.create_table('api_key_configs',
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=False),
        sa.Column('key_reference', sa.String(length=255), nullable=True),
        sa.Column('key_preview', sa.String(length=32), nullable=True),
        sa.Column('monthly_budget_usd', sa.Float(), nullable=False),
        sa.Column('current_month_usage_usd', sa.Float(), nullable=False),
        sa.Column('reserved_budget_usd', sa.Float(), nullable=False),
        sa.Column('max_tokens_per_request', sa.Integer(), nullable=False),
        sa.Column('max_concurrent_requests', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('billing_period_month', sa.String(length=7), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_api_key_configs'))
        )
        op.create_index(op.f('ix_api_key_configs_tenant_id'), 'api_key_configs', ['tenant_id'], unique=False)
    else:
        api_cols = [c['name'] for c in inspector.get_columns('api_key_configs')]
        if 'billing_period_month' not in api_cols:
            with op.batch_alter_table('api_key_configs', schema=None) as batch_op:
                batch_op.add_column(
                    sa.Column('billing_period_month', sa.String(length=7), nullable=False, server_default='2026-10')
                )

    if 'cost_logs' not in existing_tables:
        op.create_table('cost_logs',
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('model_used', sa.String(length=100), nullable=False),
        sa.Column('operation', sa.String(length=50), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False),
        sa.Column('output_tokens', sa.Integer(), nullable=False),
        sa.Column('estimated_cost_usd', sa.Float(), nullable=False),
        sa.Column('is_simulation', sa.Boolean(), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_cost_logs'))
        )
        op.create_index(op.f('ix_cost_logs_tenant_id'), 'cost_logs', ['tenant_id'], unique=False)

    if 'communication_profiles' not in existing_tables:
        op.create_table('communication_profiles',
        sa.Column('profile_id', sa.String(length=36), nullable=True),
        sa.Column('tone', sa.String(length=50), nullable=False),
        sa.Column('language_preferences', sqlite.JSON(), nullable=False),
        sa.Column('writing_rules', sqlite.JSON(), nullable=False),
        sa.Column('questionnaire_answers', sqlite.JSON(), nullable=False),
        sa.Column('example_texts', sqlite.JSON(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['user_profiles.id'], name=op.f('fk_communication_profiles_profile_id_user_profiles')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_communication_profiles'))
        )
        op.create_index(op.f('ix_communication_profiles_profile_id'), 'communication_profiles', ['profile_id'], unique=False)
        op.create_index(op.f('ix_communication_profiles_tenant_id'), 'communication_profiles', ['tenant_id'], unique=False)

    if 'candidacy_documents' not in existing_tables:
        op.create_table('candidacy_documents',
        sa.Column('candidacy_id', sa.String(length=36), nullable=False),
        sa.Column('document_type', sa.String(length=20), nullable=False),
        sa.Column('file_path', sa.Text(), nullable=False),
        sa.Column('file_hash', sa.String(length=64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['candidacy_id'], ['candidacies.id'], name=op.f('fk_candidacy_documents_candidacy_id_candidacies')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_candidacy_documents'))
        )
        op.create_index(op.f('ix_candidacy_documents_candidacy_id'), 'candidacy_documents', ['candidacy_id'], unique=False)
        op.create_index(op.f('ix_candidacy_documents_file_hash'), 'candidacy_documents', ['file_hash'], unique=False)
        op.create_index(op.f('ix_candidacy_documents_tenant_id'), 'candidacy_documents', ['tenant_id'], unique=False)

    if 'vacancies' in existing_tables:
        vac_cols = [c['name'] for c in inspector.get_columns('vacancies')]
        with op.batch_alter_table('vacancies', schema=None) as batch_op:
            if 'closing_date' not in vac_cols:
                batch_op.add_column(sa.Column('closing_date', sa.Date(), nullable=True))
            if 'fraud_signals' not in vac_cols:
                batch_op.add_column(sa.Column('fraud_signals', sqlite.JSON(), nullable=False, server_default=sa.text("'[]'")))
            if 'closure_reason' not in vac_cols:
                batch_op.add_column(sa.Column('closure_reason', sa.String(length=255), nullable=True))
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('vacancies', schema=None) as batch_op:
        batch_op.drop_column('closure_reason')
        batch_op.drop_column('fraud_signals')
        batch_op.drop_column('closing_date')
    op.drop_index(op.f('ix_candidacy_documents_tenant_id'), table_name='candidacy_documents')
    op.drop_index(op.f('ix_candidacy_documents_file_hash'), table_name='candidacy_documents')
    op.drop_index(op.f('ix_candidacy_documents_candidacy_id'), table_name='candidacy_documents')
    op.drop_table('candidacy_documents')
    op.drop_index(op.f('ix_communication_profiles_tenant_id'), table_name='communication_profiles')
    op.drop_index(op.f('ix_communication_profiles_profile_id'), table_name='communication_profiles')
    op.drop_table('communication_profiles')
    op.drop_index(op.f('ix_cost_logs_tenant_id'), table_name='cost_logs')
    op.drop_table('cost_logs')
    op.drop_index(op.f('ix_api_key_configs_tenant_id'), table_name='api_key_configs')
    op.drop_table('api_key_configs')
    # ### end Alembic commands ###
