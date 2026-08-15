"""Railway single-service supervisor for the Web API and its one Worker."""

from __future__ import annotations

import multiprocessing
from collections.abc import Callable
from typing import Any

from app.config import Settings


def _worker_entry() -> None:
    from app.worker import main

    main()


def _new_worker_process(_settings: Settings) -> multiprocessing.Process:
    return multiprocessing.Process(target=_worker_entry, name="research-worker")


def _new_server(application: Any, settings: Settings):
    import uvicorn

    return uvicorn.Server(
        uvicorn.Config(
            application,
            host=settings.app_host,
            port=settings.app_port,
            log_level=settings.log_level.lower(),
        )
    )


def run_single_service(
    settings: Settings | None = None,
    *,
    application: Any | None = None,
    worker_process_factory: Callable[[Settings], Any] = _new_worker_process,
    server_factory: Callable[[Any, Settings], Any] = _new_server,
) -> None:
    """Run exactly one API server and one Worker in a deployable service.

    The Worker remains a separate process, preserving the local execution
    model and isolating long-running model/tool calls from the Web event loop.
    ``finally`` ensures Railway shutdown signals are propagated to it.
    """

    settings = settings or Settings.from_env()
    if application is None:
        from app.main import create_app

        application = create_app(settings)
    worker_process = worker_process_factory(settings)
    server = server_factory(application, settings)
    worker_process.start()
    try:
        server.run()
    finally:
        if worker_process.is_alive():
            worker_process.terminate()
        worker_process.join(timeout=30)


def main() -> None:
    run_single_service()


if __name__ == "__main__":
    main()
