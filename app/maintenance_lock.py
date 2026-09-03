from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import Settings


class MaintenanceLockBusy(RuntimeError):
    pass


def lock_path(settings: Settings) -> Path:
    return settings.storage_root.resolve() / ".mvp-maintenance.lock"


@contextmanager
def maintenance_lock(
    settings: Settings, *, exclusive: bool, blocking: bool = False
) -> Iterator[None]:
    path = lock_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        try:
            fcntl.flock(stream.fileno(), operation)
        except BlockingIOError as exc:
            raise MaintenanceLockBusy("系统正在执行研究任务或维护操作") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
