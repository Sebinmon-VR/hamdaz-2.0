"""Trial helpers for this machine (IT_SUPPORT_TRIAL_ONLY; see ``settings``).

The once-only messages (the daily asks, the team's day, alerts) are kept as
sent in ``itsupport_sent``, so in a trial they go once a day. To send them
again to the trial person, forget what was sent to them (the agent's own
table only; nothing else is touched)::

    uv run python -m app.itsupport.trial reset sebin@hamdaz.com
    uv run python -m app.itsupport.trial reset sebin@hamdaz.com --kind status-ask
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import delete

from app.core import db
from app.core.config import get_settings
from app.models.itsupport import ITSupportSent


async def reset(email: str, kind: str | None) -> int:
    settings = get_settings()
    trial = [e.strip().lower() for e in settings.it_support_trial_only.split(",") if e.strip()]
    if email.lower() not in trial:
        raise SystemExit(f"{email} isn't in IT_SUPPORT_TRIAL_ONLY; only a trial person's "
                         "sent messages are forgotten.")
    db.init_engine(settings)
    try:
        async with db.get_session_factory()() as session:
            query = delete(ITSupportSent).where(ITSupportSent.email == email.lower())
            if kind:
                query = query.where(ITSupportSent.kind == kind)
            result = await session.execute(query)
            await session.commit()
            return result.rowcount or 0
    finally:
        await db.dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("reset", help="forget what was sent to a trial person")
    r.add_argument("email")
    r.add_argument("--kind", help="only this kind: status-ask, reason-ask, team-brief, …")
    args = parser.parse_args()
    if args.command == "reset":
        n = asyncio.run(reset(args.email, args.kind))
        print(f"forgot {n} sent message(s) for {args.email}; they can go again")


if __name__ == "__main__":
    main()
