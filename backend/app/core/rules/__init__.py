"""The rules engine (§5.2-5.4).

``registry``   — decision points, facts and actions; the fixed vocabulary.
``evaluator``  — pure condition/action evaluation with a full trace.
``assignment`` — the ``proposal.assign`` policy engine.

Nothing here touches the database or the network. Persistence lives in
:mod:`app.services.rules_service`.
"""

from app.core.rules.registry import (
    DECISION_POINTS,
    DECISION_POINTS_BY_KEY,
    DecisionPoint,
    FactType,
    Operator,
    UnknownDecisionPointError,
    get_decision_point,
)

__all__ = [
    "DECISION_POINTS",
    "DECISION_POINTS_BY_KEY",
    "DecisionPoint",
    "FactType",
    "Operator",
    "UnknownDecisionPointError",
    "get_decision_point",
]
