"""The website's contact form posts here. No authentication, and no way into the ERP.

Mounted at ``/website``, **outside** ``settings.api_prefix``, for the reasons
spelled out in ``app.hr.public``: nothing under the prefix may be reached
without assuming a colleague and a cookie, and nothing here assumes either.
No authentication dependency is imported, so no session can be read or set.

The one thing a caller can do is leave a message. The answer is a sentence:
no id, no status, nothing that confirms a row exists or could be addressed.

**Every answer carries the public headers — refusals too.** A refusal raised
as ``HTTPException`` is answered by FastAPI's own handler, which knows nothing
of them, and a cross-origin browser then hides the body and reports a network
failure. The website could never show the visitor "please check your email
address" — so refusals are returned here, built like the success.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi import status as http_status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.db import get_session, get_session_factory
from app.core.public import client_address, harden, rate_limited
from app.website import service
from app.website.schemas import PublicThanksOut

logger = logging.getLogger("hamdaz.website.public")

Session = Annotated[AsyncSession, Depends(get_session)]
Factory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Config = Annotated[Settings, Depends(get_settings)]

router = APIRouter(prefix="/website", tags=["website (public)"])


def _answer(status_code: int, body: dict[str, str]) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content=body)
    harden(response)
    return response


def _thanks(found: service.CleanEnquiry) -> JSONResponse:
    return _answer(
        http_status.HTTP_200_OK,
        PublicThanksOut(
            message=f"Thank you, {found.first_name}. We have your message and will be in touch soon."
        ).model_dump(),
    )


@router.post(
    "/enquiry",
    response_model=PublicThanksOut,
    summary="Leave a message from the website's contact form",
    responses={400: {"description": "Something to fix, said as a sentence"}, 429: {}},
)
async def enquiry(
    request: Request,
    session: Session,
    factory: Factory,
    settings: Config,
    background: BackgroundTasks,
) -> JSONResponse:
    if rate_limited(f"website:{client_address(request)}", limit=settings.website_enquiry_rate_limit):
        return _answer(
            http_status.HTTP_429_TOO_MANY_REQUESTS,
            {"detail": "You have sent several messages already. Please try again later, or email us."},
        )

    try:
        form = await request.form()
        found = service.clean(form.multi_items())
    except service.EnquiryInputError as exc:
        return _answer(http_status.HTTP_400_BAD_REQUEST, {"detail": str(exc)})
    except Exception:  # noqa: BLE001 - an unreadable body is the visitor's to resend
        logger.info("website enquiry: unreadable form", exc_info=True)
        return _answer(
            http_status.HTTP_400_BAD_REQUEST,
            {"detail": "We could not read the form. Please try again."},
        )

    if found.trapped:
        # A bot filled the field a person never sees. Thank it exactly as a
        # person is thanked, so it learns nothing to change, and keep nothing.
        logger.info("website enquiry: honeypot filled, ignored")
        return _thanks(found)

    row = await service.create(session, found)
    # Committed here rather than left to the session dependency: the people
    # told below open it from a link, so it has to exist before they are told,
    # whichever order FastAPI happens to tear dependencies down and run
    # background work in.
    await session.commit()
    logger.info("website enquiry %s from %s", row.id, found.page or "an unknown page")

    state = request.app.state
    background.add_task(
        service.tell,
        service.Teller(
            settings=settings,
            factory=factory,
            http=getattr(state, "http", None),
            mailer=getattr(state, "website_mailer", None),
        ),
        row.id,
    )
    return _thanks(found)
