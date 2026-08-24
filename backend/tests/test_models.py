"""ORM mapping integrity.

The gap these close: the whole suite ran with the database dependency overridden, so no test
ever forced SQLAlchemy to *configure its mappers*. A relationship with an ambiguous join
therefore stayed invisible until the first real query — which is exactly how
``User.label_assignments`` shipped broken.

``configure_mappers()`` needs no database. It resolves every relationship, every join
condition and every string-referenced target, so these run in milliseconds and catch the
entire class of error.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import configure_mappers

from app.models import Base
from app.models.identity import Membership, Role, Team, User
from app.models.labels import Label, LabelAssignment
from app.models.proposals import Proposal, ProposalEvent
from app.models.rules import Rule, RuleSet


class TestMappersConfigure:
    def test_every_mapper_configures(self) -> None:
        """The regression test for the ambiguous-foreign-key bug.

        Fails loudly on any relationship SQLAlchemy cannot resolve — ambiguous joins,
        typo'd back_populates, unresolvable string targets.
        """
        configure_mappers()

    def test_every_model_is_registered(self) -> None:
        """A model missing from app.models never gets a migration."""
        assert len(Base.metadata.tables) >= 26

    def test_back_populates_pair_up(self) -> None:
        """Both halves of every bidirectional relationship must agree.

        A one-sided back_populates configures fine but silently fails to update the other
        side in memory, which is a genuinely confusing bug to chase.
        """
        configure_mappers()

        for mapper in Base.registry.mappers:
            for relationship in mapper.relationships:
                partner_name = relationship.back_populates
                if partner_name is None:
                    continue

                target = relationship.mapper
                assert partner_name in target.relationships, (
                    f"{mapper.class_.__name__}.{relationship.key} back_populates "
                    f"{partner_name!r}, which does not exist on {target.class_.__name__}"
                )
                partner = target.relationships[partner_name]
                assert partner.back_populates == relationship.key, (
                    f"{mapper.class_.__name__}.{relationship.key} and "
                    f"{target.class_.__name__}.{partner_name} do not point at each other"
                )


class TestEnumColumns:
    """Every ``Mapped[SomeEnum]`` must sit on an Enum column, not a String.

    Declaring the annotation over a plain String is a lie the type checker believes:
    SQLAlchemy stores and returns raw strings, so a row loaded from the database fails
    ``is`` comparisons and has no ``.value``. That shipped as a live authentication bug —
    ``user.status is not UserStatus.ACTIVE`` was true for every user, so nobody could sign in.
    """

    def test_enum_annotated_columns_use_an_enum_type(self) -> None:
        from enum import Enum as PyEnum

        from sqlalchemy import Enum as SAEnum

        problems: list[str] = []
        for mapper in Base.registry.mappers:
            for attr in mapper.column_attrs:
                annotation = mapper.class_.__annotations__.get(attr.key, "")
                text = str(annotation)
                # Mapped[UserStatus] -> look for a registered StrEnum inside the annotation.
                for enum_cls in _known_enums():
                    if enum_cls.__name__ not in text:
                        continue
                    column = attr.columns[0]
                    if not isinstance(column.type, SAEnum):
                        problems.append(
                            f"{mapper.class_.__name__}.{attr.key} is Mapped[{enum_cls.__name__}] "
                            f"but the column is {type(column.type).__name__}"
                        )
                    break

        assert not problems, "; ".join(problems)
        assert issubclass(next(iter(_known_enums())), PyEnum)

    def test_enum_columns_round_trip_by_value_not_name(self) -> None:
        """Existing rows hold "active", not "ACTIVE"; the mapping must agree."""
        from app.models.identity import User, UserStatus

        column_type = User.__table__.c.status.type
        assert sorted(column_type.enums) == sorted(m.value for m in UserStatus)

    def test_enum_columns_stay_varchar(self) -> None:
        """native_enum=False keeps the DDL identical to the String it replaced.

        A native Postgres ENUM would need a migration for every new member; this does not.
        """
        from sqlalchemy.dialects import postgresql

        from app.models.identity import User

        ddl = User.__table__.c.status.type.compile(dialect=postgresql.dialect())
        assert ddl.startswith("VARCHAR"), ddl


def _known_enums() -> list[type]:
    from enum import StrEnum

    import app.models.identity as identity
    import app.models.labels as labels
    import app.models.leave as leave
    import app.models.platform as platform
    import app.models.proposals as proposals

    found: list[type] = []
    for module in (identity, labels, platform, proposals, leave):
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, StrEnum) and value is not StrEnum:
                found.append(value)
    return found


class TestAmbiguousForeignKeys:
    """Every relationship whose target is referenced more than once must say which FK it uses."""

    def test_relationships_with_multiple_fk_paths_declare_foreign_keys(self) -> None:
        configure_mappers()

        problems: list[str] = []
        for mapper in Base.registry.mappers:
            for relationship in mapper.relationships:
                parent_table = mapper.local_table
                target_table = relationship.mapper.local_table
                if parent_table is None or target_table is None:
                    continue

                # How many FK constraints link the two tables in either direction?
                paths = sum(
                    1
                    for table in (parent_table, target_table)
                    for fk in table.foreign_key_constraints
                    if fk.referred_table
                    in ({parent_table, target_table} - {table})
                )
                if paths > 1 and not relationship.local_remote_pairs:
                    problems.append(f"{mapper.class_.__name__}.{relationship.key}")

        assert not problems, f"ambiguous relationships: {problems}"

    def test_label_assignment_user_resolves_to_the_holder(self) -> None:
        """label_assignments references users twice: user_id and assigned_by.

        The relationship must follow the holder, not the admin who granted it. Getting this
        backwards would silently attribute labels to whoever assigned them.
        """
        configure_mappers()

        pairs = inspect(User).relationships["label_assignments"].local_remote_pairs
        remote_columns = {remote.name for _, remote in pairs}
        assert remote_columns == {"user_id"}, remote_columns

        child_pairs = inspect(LabelAssignment).relationships["user"].local_remote_pairs
        assert {local.name for local, _ in child_pairs} == {"user_id"}


class TestSchemaShape:
    """Constraints the application logic quietly relies on."""

    @pytest.mark.parametrize(
        ("model", "columns"),
        [
            (Membership, {"user_id", "team_id"}),
            (Proposal, {"source", "source_id"}),
            (RuleSet, {"decision_point", "team_id", "name"}),
            (Rule, {"rule_set_id", "position"}),
        ],
    )
    def test_uniqueness_is_enforced_in_the_database(
        self, model: type, columns: set[str]
    ) -> None:
        """These are not merely tidy — code depends on them.

        The proposals one matters most: the SharePoint sync is only idempotent because
        (source, source_id) cannot repeat.
        """
        table = model.__table__  # type: ignore[attr-defined]
        found = any(
            {c.name for c in constraint.columns} == columns
            for constraint in table.constraints
            if hasattr(constraint, "columns") and constraint.__class__.__name__ == "UniqueConstraint"
        )
        assert found, f"{model.__name__} has no unique constraint on {sorted(columns)}"

    def test_team_scoped_tables_carry_team_id(self) -> None:
        """Row scoping filters on team_id, so a table without it cannot be scoped."""
        for model in (Proposal, RuleSet, Membership):
            assert "team_id" in model.__table__.c  # type: ignore[attr-defined]

    def test_audit_and_evaluation_tables_have_no_updated_at(self) -> None:
        """Append-only tables must not look updatable.

        An `updated_at` on an audit row invites code that edits history.
        """
        from app.models.platform import AuditLog
        from app.models.rules import RuleEvaluation

        for model in (AuditLog, RuleEvaluation):
            assert "updated_at" not in model.__table__.c  # type: ignore[attr-defined]

    def test_proposal_events_cascade_from_their_proposal(self) -> None:
        configure_mappers()
        assert "delete" in inspect(Proposal).relationships["events"].cascade

    def test_deleting_a_role_in_use_is_refused_by_the_database(self) -> None:
        """memberships.role_id is RESTRICT: a role in use cannot vanish under people."""
        fk = next(
            fk
            for fk in Membership.__table__.foreign_keys  # type: ignore[attr-defined]
            if fk.column.table.name == "roles"
        )
        assert fk.ondelete == "RESTRICT"


class TestReprsAreSafe:
    """__repr__ runs in tracebacks and logs, so it must never raise on an unloaded object."""

    @pytest.mark.parametrize(
        "model", [User, Team, Role, Membership, Label, LabelAssignment, Proposal, ProposalEvent]
    )
    def test_repr_on_an_empty_instance(self, model: type) -> None:
        assert isinstance(repr(model()), str)
