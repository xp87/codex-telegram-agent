from __future__ import annotations

import logging
import sys

from .bot import TelegramCodexBot
from .config import configure_logging, load_projects, load_settings, validate_startup
from .db import AgentDb


logger = logging.getLogger(__name__)


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_path)
    logger.info("Starting Codex Telegram agent")

    try:
        projects = load_projects()
        validate_startup(settings, projects)
        db = AgentDb(settings.db_path)
        db.init()
        failed_running = db.fail_running_jobs_on_startup()
        if failed_running:
            logger.warning("Marked stale running jobs as failed on startup: count=%s", failed_running)
    except Exception as exc:
        logger.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return 1

    application = TelegramCodexBot(settings, db, projects).build_application()
    try:
        application.run_polling(allowed_updates=["message", "callback_query"])
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
