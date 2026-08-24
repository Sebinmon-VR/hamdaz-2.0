"""ORM models. Importing this package registers every table on ``Base.metadata``.

Alembic autogenerate relies on that, so a model that is not imported here is a model that
silently never gets a migration.
"""

from app.models.base import Base
from app.models.identity import (
    Membership,
    Permission,
    Role,
    RolePermission,
    Team,
    User,
    UserStatus,
)
from app.models.labels import Label, LabelAssignment, LabelKind
from app.models.leave import (
    Holiday,
    LeaveBalance,
    LeaveRequest,
    LeaveSetting,
    LeaveStatus,
    LeaveType,
)
from app.models.platform import (
    AuditLog,
    ConnectorMode,
    ConnectorStatus,
    EmailOutbox,
    FeatureFlag,
    JobRun,
    JobStatus,
)
from app.models.proposals import (
    Notification,
    Proposal,
    ProposalEvent,
    ProposalEventType,
    ProposalSource,
    ProposalStatus,
)
from app.models.rules import (
    AssignmentPolicy,
    AssignmentPolicyVersion,
    DistributionMode,
    Rule,
    RuleEvaluation,
    RuleSet,
    RuleSetVersion,
)

__all__ = [
    "AssignmentPolicy",
    "AssignmentPolicyVersion",
    "AuditLog",
    "Base",
    "ConnectorMode",
    "ConnectorStatus",
    "DistributionMode",
    "EmailOutbox",
    "FeatureFlag",
    "Holiday",
    "JobRun",
    "JobStatus",
    "Label",
    "LabelAssignment",
    "LabelKind",
    "LeaveBalance",
    "LeaveRequest",
    "LeaveSetting",
    "LeaveStatus",
    "LeaveType",
    "Membership",
    "Notification",
    "Permission",
    "Proposal",
    "ProposalEvent",
    "ProposalEventType",
    "ProposalSource",
    "ProposalStatus",
    "Role",
    "RolePermission",
    "Rule",
    "RuleEvaluation",
    "RuleSet",
    "RuleSetVersion",
    "Team",
    "User",
    "UserStatus",
]
