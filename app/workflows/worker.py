"""Waking the runs that are waiting on the world.

A run waiting on the person wakes when they answer. A run waiting on a
supplier's mail or a quote's approval has nobody to wake it, so this loop
does: every ``poll_seconds`` it takes the runs whose ``wake_at`` has passed
and advances each, in its own session, so one run's failure is one run's
failure.

The same advisory-lock arrangement as the intake worker, for the same reason:
on App Service there is more than one instance, and two of them advancing the
same run would send the same mail twice.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.workflows import service
from app.workflows.engine import Services, advance

logger = logging.getLogger("hamdaz.workflows.worker")

WORKFLOW_LOCK = 812_403
BACKOFF_SECONDS = 300


async def _take_lock(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": WORKFLOW_LOCK})
    )


async def _release(session: AsyncSession) -> None:
    await session.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": WORKFLOW_LOCK})


class WorkflowWorker:
    def __init__(self, *, factory: async_sessionmaker[AsyncSession], services: Services) -> None:
        self._factory = factory
        self._services = services
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())
            logger.info("workflow worker started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _sleep(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            return True
        return False

    async def _loop(self) -> None:
        if not await self._sleep(15):
            return
        while not self._stopping.is_set():
            interval = 60
            try:
                interval = await self.tick()
            except Exception:  # noqa: BLE001 - a loop must not die
                logger.exception("workflow tick failed")
                interval = BACKOFF_SECONDS
            if not await self._sleep(interval):
                return

    async def tick(self) -> int:
        """One pass. Returns how long to wait before the next."""
        async with self._factory() as lock_session:
            if not await _take_lock(lock_session):
                return 60
            try:
                async with self._factory() as session:
                    settings = await service.get_settings(session)
                    interval = max(30, int(settings.poll_seconds or 60))
                    # The agent steps use the assistant's model. Read each
                    # tick, so a super admin changing it takes effect here too.
                    try:
                        from app.assistant import service as assistant_service

                        self._services.model_key = (
                            await assistant_service.get_settings(session)
                        ).model_key
                    except Exception:  # noqa: BLE001 - a flow with no agent step does not care
                        pass
                    due = await service.wake_due(session)
                    ids = [run.id for run in due]
                for run_id in ids:
                    async with self._factory() as session:
                        try:
                            run = await service.get_run(session, run_id)
                            settings = await service.get_settings(session)
                            await advance(session, run, settings, self._services)
                            await session.commit()
                        except Exception:  # noqa: BLE001
                            logger.exception("workflow run %s could not be advanced", run_id)
                            await session.rollback()
                # Flows on the task_assigned trigger: a task newly assigned to
                # somebody on the flow's team gets its run started for them.
                async with self._factory() as session:
                    try:
                        settings = await service.get_settings(session)
                        begun = await service.auto_start(
                            session, settings=settings, services=self._services
                        )
                        await session.commit()
                        if begun:
                            logger.info("auto-started %d workflow run(s)", len(begun))
                    except Exception:  # noqa: BLE001
                        logger.exception("workflow auto-start failed")
                        await session.rollback()
                return interval
            finally:
                await _release(lock_session)
