"""Entry point for billmanager_parser service.

Runs the parser on startup and then on a cron schedule.
"""

import asyncio
import signal
import sys

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import settings
from logging_config import configure_logging
from orchestrator import ParserOrchestrator

log = structlog.get_logger(__name__)


async def run_parse_job() -> None:
    orchestrator = ParserOrchestrator()
    try:
        await orchestrator.run_all_providers()
    except Exception as exc:
        log.error("Unhandled error in parse job", error=str(exc), exc_info=True)


async def main() -> None:
    configure_logging()

    log.info(
        "billmanager_parser starting",
        providers=len(settings.providers),
        schedule=settings.parse_cron,
        api_url=settings.cloudsell_api_url,
        llm_model=settings.anthropic_model,
    )

    scheduler = AsyncIOScheduler()

    cron_parts = settings.parse_cron.split()
    if len(cron_parts) == 5:
        minute, hour, day, month, day_of_week = cron_parts
    else:
        log.warning("Invalid cron expression, using default '0 2 * * *'", parse_cron=settings.parse_cron)
        minute, hour, day, month, day_of_week = "0", "2", "*", "*", "*"

    scheduler.add_job(
        run_parse_job,
        trigger=CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
        ),
        id="parse_pricing_plans",
        name="Parse BILLmanager pricing plans",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    log.info("Scheduler started", next_run=str(scheduler.get_job("parse_pricing_plans").next_run_time))

    # Run immediately on first start
    log.info("Running initial parse on startup...")
    await run_parse_job()

    # Keep running until SIGTERM / SIGINT
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _shutdown(*_: object) -> None:
        log.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    await stop_event.wait()
    scheduler.shutdown(wait=False)
    log.info("billmanager_parser stopped")


if __name__ == "__main__":
    asyncio.run(main())
