"""payroll

Employees on payroll, their salary structures, each month's run and its lines,
the payroll access list, settings, and the payroll log. See app/payroll.

Revision ID: c4e8a1f7d2b6
Revises: e8c4a2f6b190
Create Date: 2026-11-04 15:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'c4e8a1f7d2b6'
down_revision: str | None = 'e8c4a2f6b190'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())
MONEY = sa.Numeric(16, 2)


def _stamps() -> list[sa.Column]:
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    ]


def _id() -> sa.Column:
    return sa.Column('id', UUID, server_default=sa.text('gen_random_uuid()'), nullable=False)


def _user_fk(name: str) -> sa.Column:
    return sa.Column(name, UUID, sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)


def _json(name: str, empty: str) -> sa.Column:
    return sa.Column(name, JSONB, server_default=sa.text(f"'{empty}'::jsonb"), nullable=False)


def _num(name: str, type_: sa.types.TypeEngine, default: str) -> sa.Column:
    return sa.Column(name, type_, server_default=sa.text(default), nullable=False)


def upgrade() -> None:
    op.create_table(
        'payroll_settings',
        sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
        sa.Column('accounts_team_slug', sa.String(64), server_default=sa.text("'accounts'"), nullable=False),
        sa.Column('notify_in_app', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('ae_company_name', sa.String(200), server_default=sa.text("'Hamdaz'"), nullable=False),
        sa.Column('ae_company_address', sa.Text(), nullable=True),
        sa.Column('ae_day_basis', sa.String(12), server_default=sa.text("'fixed_30'"), nullable=False),
        _num('ae_hours_per_day', sa.Numeric(5, 2), '8'),
        _num('ae_ot_normal_rate', sa.Numeric(5, 2), '1.25'),
        _num('ae_ot_premium_rate', sa.Numeric(5, 2), '1.50'),
        sa.Column('ae_employer_mol_id', sa.String(20), nullable=True),
        sa.Column('ae_employer_bank_routing', sa.String(20), nullable=True),
        sa.Column('ae_employer_reference', sa.String(60), nullable=True),
        sa.Column('in_company_name', sa.String(200), server_default=sa.text("'Hamdaz'"), nullable=False),
        sa.Column('in_company_address', sa.Text(), nullable=True),
        sa.Column('in_day_basis', sa.String(12), server_default=sa.text("'calendar'"), nullable=False),
        sa.Column('in_pf_establishment', sa.String(40), nullable=True),
        _num('in_pf_ee_rate', sa.Numeric(5, 2), '12'),
        _num('in_pf_er_rate', sa.Numeric(5, 2), '12'),
        _num('in_eps_rate', sa.Numeric(5, 2), '8.33'),
        _num('in_pf_ceiling', MONEY, '15000'),
        _num('in_edli_rate', sa.Numeric(5, 2), '0.50'),
        _num('in_pf_admin_rate', sa.Numeric(5, 2), '0.50'),
        sa.Column('in_wage_code_rule', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        _num('in_esi_threshold', MONEY, '21000'),
        _num('in_esi_ee_rate', sa.Numeric(5, 2), '0.75'),
        _num('in_esi_er_rate', sa.Numeric(5, 2), '3.25'),
        _json('in_pt_slabs', '{}'),
        _json('in_tax_tables', '{}'),
        _user_fk('updated_by_id'),
        *_stamps(),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'payroll_members',
        _id(),
        sa.Column('user_id', UUID, sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('level', sa.String(12), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        _user_fk('added_by_id'),
        *_stamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id'),
    )

    op.create_table(
        'payroll_employees',
        _id(),
        _user_fk('user_id'),
        sa.Column('code', sa.String(40), nullable=False),
        sa.Column('full_name', sa.String(200), nullable=False),
        sa.Column('email', sa.String(320), nullable=True),
        sa.Column('designation', sa.String(120), nullable=True),
        sa.Column('department', sa.String(120), nullable=True),
        sa.Column('country', sa.String(2), nullable=False),
        sa.Column('joined_on', sa.Date(), nullable=False),
        sa.Column('left_on', sa.Date(), nullable=True),
        sa.Column('bank_name', sa.String(120), nullable=True),
        sa.Column('bank_account', sa.String(40), nullable=True),
        sa.Column('iban', sa.String(34), nullable=True),
        sa.Column('bank_routing', sa.String(20), nullable=True),
        sa.Column('mol_person_id', sa.String(20), nullable=True),
        sa.Column('ifsc', sa.String(11), nullable=True),
        sa.Column('pan', sa.String(10), nullable=True),
        sa.Column('uan', sa.String(12), nullable=True),
        sa.Column('esi_number', sa.String(17), nullable=True),
        sa.Column('pt_state', sa.String(4), nullable=True),
        sa.Column('pf_enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('pf_on_actual', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('eps_member', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('esi_mode', sa.String(4), server_default=sa.text("'auto'"), nullable=False),
        sa.Column('pt_override', MONEY, nullable=True),
        sa.Column('tds_monthly_override', MONEY, nullable=True),
        sa.Column('opening_fy', sa.Integer(), nullable=True),
        _num('opening_taxable', MONEY, '0'),
        _num('opening_tds', MONEY, '0'),
        sa.Column('notes', sa.Text(), nullable=True),
        _user_fk('created_by_id'),
        _user_fk('updated_by_id'),
        *_stamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id'),
    )
    op.create_index('ix_payroll_employees_code', 'payroll_employees', ['code'], unique=True)
    op.create_index('ix_payroll_employees_country', 'payroll_employees', ['country'])

    op.create_table(
        'payroll_salaries',
        _id(),
        sa.Column(
            'employee_id', UUID,
            sa.ForeignKey('payroll_employees.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('effective_from', sa.Date(), nullable=False),
        _json('components', '[]'),
        sa.Column('note', sa.Text(), nullable=True),
        _user_fk('created_by_id'),
        *_stamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('employee_id', 'effective_from', name='uq_payroll_salary_from'),
    )
    op.create_index('ix_payroll_salaries_employee_id', 'payroll_salaries', ['employee_id'])

    op.create_table(
        'payroll_runs',
        _id(),
        sa.Column('country', sa.String(2), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('month', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('status', sa.String(12), nullable=False),
        _num('headcount', sa.Integer(), '0'),
        _num('gross', MONEY, '0'),
        _num('deductions', MONEY, '0'),
        _num('net', MONEY, '0'),
        _num('employer_cost', MONEY, '0'),
        _json('warnings', '[]'),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('calculated_at', sa.DateTime(timezone=True), nullable=True),
        _user_fk('created_by_id'),
        _user_fk('submitted_by_id'),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        _user_fk('approved_by_id'),
        sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('returned_note', sa.Text(), nullable=True),
        _user_fk('paid_by_id'),
        sa.Column('paid_on', sa.Date(), nullable=True),
        *_stamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('country', 'year', 'month', name='uq_payroll_run_period'),
    )
    op.create_index('ix_payroll_runs_status', 'payroll_runs', ['status'])

    op.create_table(
        'payroll_lines',
        _id(),
        sa.Column('run_id', UUID, sa.ForeignKey('payroll_runs.id', ondelete='CASCADE'), nullable=False),
        sa.Column(
            'employee_id', UUID,
            sa.ForeignKey('payroll_employees.id', ondelete='RESTRICT'), nullable=False,
        ),
        sa.Column('employee_code', sa.String(40), nullable=False),
        sa.Column('employee_name', sa.String(200), nullable=False),
        sa.Column('designation', sa.String(120), nullable=True),
        _json('identifiers', '{}'),
        _num('lop_adjust', sa.Numeric(6, 2), '0'),
        _num('ot_normal_hours', sa.Numeric(7, 2), '0'),
        _num('ot_premium_hours', sa.Numeric(7, 2), '0'),
        _json('extras', '[]'),
        sa.Column('tds_override', MONEY, nullable=True),
        sa.Column('on_hold', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        _num('leave_lop_days', sa.Numeric(6, 2), '0'),
        _num('basis_days', sa.Numeric(6, 2), '30'),
        _num('paid_days', sa.Numeric(6, 2), '0'),
        _num('unpaid_days', sa.Numeric(6, 2), '0'),
        _json('earnings', '[]'),
        _json('deductions', '[]'),
        _json('employer', '[]'),
        _num('gross', MONEY, '0'),
        _num('total_deductions', MONEY, '0'),
        _num('net', MONEY, '0'),
        _num('employer_cost', MONEY, '0'),
        _num('taxable', MONEY, '0'),
        _num('tds', MONEY, '0'),
        _json('workings', '{}'),
        _json('warnings', '[]'),
        *_stamps(),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'employee_id', name='uq_payroll_line_employee'),
    )
    op.create_index('ix_payroll_lines_employee', 'payroll_lines', ['employee_id'])

    op.create_table(
        'payroll_events',
        _id(),
        _user_fk('actor_id'),
        sa.Column('run_id', UUID, sa.ForeignKey('payroll_runs.id', ondelete='SET NULL'), nullable=True),
        sa.Column(
            'employee_id', UUID,
            sa.ForeignKey('payroll_employees.id', ondelete='SET NULL'), nullable=True,
        ),
        sa.Column('action', sa.String(40), nullable=False),
        sa.Column('summary', sa.Text(), nullable=False),
        _json('detail', '{}'),
        *_stamps(),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_payroll_events_created', 'payroll_events', ['created_at'])
    op.create_index('ix_payroll_events_run_id', 'payroll_events', ['run_id'])
    op.create_index('ix_payroll_events_employee_id', 'payroll_events', ['employee_id'])


def downgrade() -> None:
    op.drop_table('payroll_events')
    op.drop_table('payroll_lines')
    op.drop_table('payroll_runs')
    op.drop_table('payroll_salaries')
    op.drop_table('payroll_employees')
    op.drop_table('payroll_members')
    op.drop_table('payroll_settings')
