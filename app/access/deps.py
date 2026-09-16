"""One module gate, for routers to hang on themselves.

The quote requests router grew its own ``require_module`` first, and the comment
it carries is the important part: the gate goes on the *router*, not on each
route, because a gate that has to be remembered every time a route is added is a
gate that will eventually be missed — and the route that misses it will be the
one nobody thinks to check.

This is that dependency, as a factory, so the next module to need one does not
copy it a fourth time and drift.

**Only ``super_admin`` passes without a grant.** Not the CEO, not a manager:
``effective_access`` hands everything to the access administrators alone, and
anybody else reaches a module through a team that was granted it. That is worth
knowing before adding a gate to a module people already use — turning one on
without backfilling the grants locks out everyone but one person.
"""

from __future__ import annotations

from typing import Annotated, Callable, Coroutine, Any

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import service as access_service
from app.auth.deps import CurrentUser
from app.core.db import get_session
from app.roles.deps import CurrentRoles

Session = Annotated[AsyncSession, Depends(get_session)]


def module_guard(
    module_key: str, module_name: str
) -> Callable[..., Coroutine[Any, Any, None]]:
    """A dependency that refuses anyone whose teams lack ``module_key``.

    ``module_name`` is how the module is named to a person in the refusal — the
    catalogue's own wording, so the message matches the switch an administrator
    would go and look for.
    """

    async def guard(user: CurrentUser, roles: CurrentRoles, session: Session) -> None:
        if not await access_service.can_reach(
            session, user_id=user.id, global_roles=roles, module_key=module_key
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Your team does not have the {module_name} module. "
                    f"A super admin can grant it."
                ),
            )

    return guard
