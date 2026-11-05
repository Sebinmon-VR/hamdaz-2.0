"""What payroll's routes take from the app: Zoho Books and the mailer.

Dependencies rather than ``request.app.state`` lookups in each route, so a
test (which runs without the app's startup) can hand in a fake of either.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from app.zoho.client import ZohoBooks


def get_zoho(request: Request) -> ZohoBooks:
    return request.app.state.zoho


def get_mailer(request: Request) -> Any:
    # Absent only where the app's startup has not run; mail is then reported
    # as failed by the step that wanted it, never raised.
    return getattr(request.app.state, "payroll_mailer", None)


Zoho = Annotated[ZohoBooks, Depends(get_zoho)]
Mailer = Annotated[Any, Depends(get_mailer)]
