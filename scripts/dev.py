"""开发环境重启助手：管理 web (app.main) 与 worker (app.worker) 子进程。

常驻 web/worker 进程都不带 --reload，改了源码或 profile 后仍在跑旧代码。
本脚本用 ``python scripts/dev.py <cmd>`` 一键完成：
  start   启动 web + worker（各自新进程组，pgid==pid，便于连桥接子进程一起停）
  stop    停止 web + worker（杀进程组，连带 reap Node 桥接子进程）
  restart stop → 清 __pycache__/*.pyc → start（每次都加载新代码）
  status  查看运行状态，并比对 app/ 源码 mtime 与进程启动时间，提示"运行旧代码"
  clean   仅清理 __pycache__ 与 *.pyc（刷新编译缓存，不重启进程）

本地开发专用，不改任何 app/ 运行时代码，不动 docker-compose.yml。
子进程继承当前环境（app 自身会 load_dotenv 取 live key）；绝不打印任何密钥。
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
LOGS_DIR = PROJECT_ROOT / "logs"
WEB_PID = LOGS_DIR / "web.pid"
WORKER_PID = LOGS_DIR / "worker.pid"
WEB_OUT = LOGS_DIR / "web.out"
WORKER_OUT = LOGS_DIR / "worker.out"
APP_DIR = PROJECT_ROOT / "app"
PROFILES_DIR = APP_DIR / "profiles"

WEB_MODULE = "app.main"
WORKER_MODULE = "app.worker"
WORKER_READY_MARKER = "Research Worker 已启动"

HEALTH_URL = "http://127.0.0.1:8000/api/health"
HEALTH_TIMEOUT_SEC = 15.0
WORKER_READY_TIMEOUT_SEC = 15.0
STOP_GRACE_SEC = 20.0
WEB_STOP_GRACE_SEC = 10.0
POLL_INTERVAL_SEC = 0.5

# 清 pycache 时绝不下钻这些目录（避免删 .venv site-packages / node_modules / git）
EXCLUDE_DIRS = {".venv", "node_modules", ".git"}


# --------------------------------------------------------------------------- #
# PID 存活与陈旧校验
# --------------------------------------------------------------------------- #
def _read_pid(path: Path) -> int | None:
    """读 PID 文件，返回 int；缺失或非纯数字返回 None。"""
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def _is_alive(pid: int) -> bool:
    """进程是否存活。无权限（EACCES）视为存活。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_cmdline(pid: int) -> str:
    """返回 pid 的命令行（``ps -o command=``）；不存活或无 ps 时返回 ''。"""
    try:
        out = subprocess.run(
            # macOS 默认会按终端宽度截断长 Python 路径，导致真实的
            # ``python -m app.main/worker`` 被误判为无关进程。
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except FileNotFoundError:
        return ""
    return out


def _is_app_process(pid: int, module: str) -> bool:
    """pid 是否在跑目标 app 模块，兼容 ``main`` 和 ``app.main``。"""
    cmd = _process_cmdline(pid)
    if not cmd:
        return False
    module_name = module if module.startswith("app.") else f"app.{module}"
    return f"-m {module_name}" in cmd and "python" in cmd.lower()


def _scan_app_pids(module: str) -> list[int]:
    """全系统扫描所有跑 ``python -m app.<module>`` 的进程 PID。

    用于检测 PID 文件之外的"游离"app 进程（手动启动 / 上一次 dev.py 未正常停止遗留）。
    用 ``ps -eo pid=,command=`` 而非 pgrep：pgrep -f 在 macOS 上对长命令行的匹配不可靠。
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except FileNotFoundError:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        module_name = module if module.startswith("app.") else f"app.{module}"
        if not line or f"-m {module_name}" not in line:
            continue
        if "python" not in line.lower():
            continue
        # 行首第一个 token 是 pid
        head = line.split(None, 1)[0]
        if head.isdigit():
            pids.append(int(head))
    return pids


def _validate_or_cleanup(pid_file: Path, module: str) -> tuple[int | None, bool]:
    """校验 PID 文件：返回 (pid | None, is_running)。

    - pid 不存活 → 删 PID 文件，打印"过期…已清理"，返回 (None, False)。
    - pid 存活但非 app 进程（被复用）→ 删 PID 文件，打印"指向无关进程…已忽略"，返回 (None, False)。
    - pid 存活且是 app 进程 → (pid, True)。
    绝不盲杀可能已被复用的 pid。
    """
    pid = _read_pid(pid_file)
    if pid is None:
        return (None, False)
    if not _is_alive(pid):
        pid_file.unlink(missing_ok=True)
        print(f"检测到过期 {pid_file.name} (pid={pid})，已清理")
        return (None, False)
    if not _is_app_process(pid, module):
        cmd = _process_cmdline(pid)
        pid_file.unlink(missing_ok=True)
        print(f"{pid_file.name} 指向无关进程 (pid={pid}, cmd={cmd[:80]})，已忽略，未发送信号")
        return (None, False)
    return (pid, True)


# --------------------------------------------------------------------------- #
# 进程启动 / 停止
# --------------------------------------------------------------------------- #
def _launch(module: str, pid_file: Path, out_file: Path) -> subprocess.Popen:
    """用 .venv/bin/python -m <module> 启动子进程（新会话，pgid==pid），写 PID 文件。

    返回 Popen 句柄，供调用方用 ``poll()`` 判断子进程是否已死（如端口被占时秒退）。
    """
    if not VENV_PYTHON.is_file():
        raise SystemExit(f".venv/bin/python 不存在: {VENV_PYTHON}")
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # 追加模式保留多次重启的输出；父进程在 Popen 后关闭句柄（子进程有自己的 dup）
    out_handle = open(out_file, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [str(VENV_PYTHON), "-m", module],
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),
            stdout=out_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # 新会话+新进程组，pgid==pid
            close_fds=True,
        )
    finally:
        out_handle.close()
    pid_file.write_text(str(proc.pid))
    return proc


def _process_gone(pid: int) -> bool:
    """进程是否已终止并（若是我们 fork 的）已 reap。

    - 我们 fork 的子进程：``os.waitpid(pid, WNOHANG)`` 能 reap 僵尸；reap 后即视为 gone。
      否则仍运行中。
    - 非我们 fork 的进程（继承/手动起的）：``waitpid`` 抛 ChildProcessError，退回
      ``os.kill(pid,0)`` 判存活。注意僵尸进程对 ``kill(0)`` 仍返回成功，故必须先 waitpid。
    """
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return True  # 已 reap（原本是僵尸）
    except ChildProcessError:
        pass  # 不是我们的子进程，无法 waitpid
    return not _is_alive(pid)


def _stop_pid(pid: int, *, use_group: bool = True, grace: float = STOP_GRACE_SEC) -> bool:
    """SIGTERM 进程（或进程组），grace 秒后仍存活则 SIGKILL。返回是否最终已停。"""
    sig_term = signal.SIGTERM
    sig_kill = signal.SIGKILL

    def _send(sig: int) -> None:
        try:
            if use_group:
                os.killpg(pid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass

    _send(sig_term)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if _process_gone(pid):
            break
        time.sleep(POLL_INTERVAL_SEC)
    if not _process_gone(pid):
        print(f"  SIGTERM 超时，SIGKILL (pid={pid})")
        _send(sig_kill)
        time.sleep(POLL_INTERVAL_SEC)
    # 尝试 reap 残留僵尸（仅当我们是父进程时有效；否则 ChildProcessError 忽略）
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    return _process_gone(pid) or not _is_alive(pid)


def _wait_health(url: str, timeout: float) -> bool:
    """轮询 health 端点直到 200 或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(POLL_INTERVAL_SEC)
    return False


def _wait_log_marker(log_path: Path, marker: str, timeout: float, start_offset: int) -> bool:
    """轮询日志文件 start_offset 之后是否出现 marker（保证是本次启动的新行）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            size = log_path.stat().st_size
            if size > start_offset:
                with open(log_path, "rb") as fh:
                    fh.seek(start_offset)
                    tail = fh.read(size - start_offset).decode("utf-8", "replace")
                if marker in tail:
                    return True
        except FileNotFoundError:
            pass
        except OSError:
            pass
        time.sleep(POLL_INTERVAL_SEC)
    return False


# --------------------------------------------------------------------------- #
# pycache / 源码 mtime
# --------------------------------------------------------------------------- #
def _clean_pycache() -> tuple[int, int]:
    """删除 PROJECT_ROOT 下的 __pycache__ 目录与 *.pyc 文件，排除 .venv/node_modules/.git。

    返回 (已删目录数, 已删文件数)。纯 Python os.walk，绝不触碰依赖目录。
    """
    dirs_deleted = 0
    files_deleted = 0
    for root, dirs, files in os.walk(PROJECT_ROOT):
        # 原地剔除，阻止 os.walk 下钻排除目录
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for d in list(dirs):
            if d == "__pycache__":
                target = Path(root) / d
                shutil.rmtree(target, ignore_errors=True)
                dirs.remove(d)  # 已删，不再下钻
                dirs_deleted += 1
        for f in files:
            if f.endswith(".pyc"):
                (Path(root) / f).unlink(missing_ok=True)
                files_deleted += 1
    return (dirs_deleted, files_deleted)


def _newest_source_mtime() -> datetime | None:
    """app/ 下最新源码 mtime：覆盖 app/**/*.py 与 app/profiles/*.json。

    profile 改动同样需要重启 worker（常驻进程缓存了 profile），故一并纳入"旧代码"检测。
    """
    latest: float | None = None
    candidates: list[Path] = []
    if APP_DIR.is_dir():
        candidates.extend(APP_DIR.rglob("*.py"))
    if PROFILES_DIR.is_dir():
        candidates.extend(PROFILES_DIR.glob("*.json"))
    for p in candidates:
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if latest is None or m > latest:
            latest = m
    return None if latest is None else datetime.fromtimestamp(latest)


def _process_start_time(pid: int) -> datetime | None:
    """``ps -o lstart=`` 解析进程启动时间；失败返回 None。"""
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except FileNotFoundError:
        return None
    if not out:
        return None
    try:
        return datetime.strptime(out, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_start() -> int:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    # 先校验两个 PID 文件，避免重复启动
    web_pid, web_running = _validate_or_cleanup(WEB_PID, WEB_MODULE)
    worker_pid, worker_running = _validate_or_cleanup(WORKER_PID, WORKER_MODULE)
    if web_running:
        print(f"Web 已在运行 (pid={web_pid})，请先执行 stop/restart")
        return 1
    if worker_running:
        print(f"Worker 已在运行 (pid={worker_pid})，请先执行 stop/restart")
        return 1
    if not VENV_PYTHON.is_file():
        print(f".venv/bin/python 不存在: {VENV_PYTHON}")
        return 1

    # 扫描 PID 文件之外的游离 app 进程（手动启动 / 上次未正常停止遗留）。
    # 这类进程 pgid≠pid，dev.py stop 用 killpg(pid) 无法可靠停止它，且 web 会占住端口 →
    # start 出来的新进程会因 bind 冲突秒退。提前拒绝并给出明确指引，避免"误报启动成功"。
    stray_web = [p for p in _scan_app_pids(WEB_MODULE) if p != web_pid]
    stray_worker = [p for p in _scan_app_pids(WORKER_MODULE) if p != worker_pid]
    if stray_web or stray_worker:
        if stray_web:
            print(f"检测到游离 Web 进程 (pid={stray_web})：非 dev.py 管理（无 PID 文件/pgid≠pid）")
        if stray_worker:
            print(f"检测到游离 Worker 进程 (pid={stray_worker})：非 dev.py 管理（无 PID 文件/pgid≠pid）")
        print("这些进程可能占住端口或跑旧代码。请先手动停止：")
        print(f"  kill {' '.join(map(str, stray_web + stray_worker))}")
        print("然后再执行 python scripts/dev.py start。")
        return 1

    # 记录 worker.log 当前大小，用于只看本次启动后的新日志
    try:
        worker_log_offset = (LOGS_DIR / "worker.log").stat().st_size
    except FileNotFoundError:
        worker_log_offset = 0

    new_web_proc = _launch(WEB_MODULE, WEB_PID, WEB_OUT)
    new_worker_proc = _launch(WORKER_MODULE, WORKER_PID, WORKER_OUT)
    new_web = new_web_proc.pid
    new_worker = new_worker_proc.pid

    # 确认 web 健康
    if _wait_health(HEALTH_URL, HEALTH_TIMEOUT_SEC):
        # 健康端点可达但本进程已退出 → 端口被其它（陈旧/手动）进程占用，避免误报成功
        if new_web_proc.poll() is not None:
            print(
                f"Web 启动失败 (pid={new_web}, exit={new_web_proc.returncode})："
                f"端口被其它进程占用，请先执行 python scripts/dev.py stop，"
                f"或手动停掉占用 {HEALTH_URL} 的进程；详情见 {WEB_OUT.name}"
            )
            return 1
        print(f"Web 已启动 (pid={new_web}, health=ok)")
    else:
        # 进程是否已死（如端口被占 / import 失败）
        if new_web_proc.poll() is not None:
            print(f"Web 启动失败 (pid={new_web}, exit={new_web_proc.returncode})，请查看 {WEB_OUT.name}")
            return 1
        print(f"Web 启动超时 (pid={new_web})，请查看 {WEB_OUT.name} / {WEB_PID.name}")

    # 确认 worker 就绪
    if _wait_log_marker(LOGS_DIR / "worker.log", WORKER_READY_MARKER, WORKER_READY_TIMEOUT_SEC, worker_log_offset):
        print(f"Worker 已启动 (pid={new_worker})")
    else:
        if new_worker_proc.poll() is not None:
            print(f"Worker 启动失败 (pid={new_worker}, exit={new_worker_proc.returncode})，请查看 {WORKER_OUT.name}")
            return 1
        print(f"Worker 启动超时 (pid={new_worker})，请查看 logs/worker.log / {WORKER_OUT.name}")

    return 0


def _stray_notice(managed_pids: dict[str, int | None]) -> None:
    """stop/status 结束时若发现 PID 文件之外的游离 app 进程，打印提示（仅提示，不杀）。

    managed_pids: {"web": pid|None, "worker": pid|None} —— 已被 dev.py 管理的 PID，扫描时排除。
    """
    strays: list[tuple[str, int]] = []
    for module, label in ((WEB_MODULE, "web"), (WORKER_MODULE, "worker")):
        managed = managed_pids.get(label)
        for p in _scan_app_pids(module):
            if p != managed:
                strays.append((label, p))
    if strays:
        print()
        print("注意：检测到游离 app 进程（非 dev.py 管理，stop 不会动它们）：")
        for label, p in strays:
            print(f"  {label}: pid={p}")
        print(f"如需一并停止：  kill {' '.join(str(p) for _, p in strays)}")
        print("之后用 python scripts/dev.py start 即可由 dev.py 接管。")


def cmd_stop() -> int:
    _stop_component(WORKER_PID, WORKER_MODULE, use_group=True, grace=STOP_GRACE_SEC, label="Worker")
    _stop_component(WEB_PID, WEB_MODULE, use_group=True, grace=WEB_STOP_GRACE_SEC, label="Web")
    _stray_notice({"web": _read_pid(WEB_PID), "worker": _read_pid(WORKER_PID)})
    return 0


def _stop_component(pid_file: Path, module: str, *, use_group: bool, grace: float, label: str) -> None:
    pid, running = _validate_or_cleanup(pid_file, module)
    if pid is None:
        if _read_pid(pid_file) is None and not pid_file.exists():
            # _validate_or_cleanup 已对陈旧/复用情况打印并删文件；这里仅处理"本就没有 PID 文件"
            if not pid_file.exists():
                print(f"{label} 未找到 PID 文件，跳过")
        return
    if not running:
        print(f"{label} 未运行（PID 文件已清理）")
        return
    stopped = _stop_pid(pid, use_group=use_group, grace=grace)
    print(f"{label} 已停止 (pid={pid})" + ("" if stopped else "（仍存活，请手动检查）"))
    pid_file.unlink(missing_ok=True)


def cmd_restart() -> int:
    cmd_stop()
    dirs, files = _clean_pycache()
    print(f"已清理 {dirs} 个 __pycache__ 目录, {files} 个 *.pyc 文件")
    return cmd_start()


def cmd_status() -> int:
    newest = _newest_source_mtime()
    newest_str = newest.strftime("%Y-%m-%d %H:%M:%S") if newest else "-"
    print(f"{'组件':<8} {'PID':<8} {'状态':<14} {'启动时间':<22} {'app/ 最新源码 mtime':<22} {'代码新旧'}")
    old_code = False
    managed: dict[str, int | None] = {"web": None, "worker": None}
    for label, pid_file, module in (
        ("web", WEB_PID, WEB_MODULE),
        ("worker", WORKER_PID, WORKER_MODULE),
    ):
        pid, running = _validate_or_cleanup(pid_file, module)
        if running and pid is not None:
            managed[label] = pid
        if pid is None or not running:
            # _validate_or_cleanup 已对陈旧/复用打印并删文件
            state = "未运行" if (pid is None and not pid_file.exists()) else "已清理"
            print(f"{label:<8} {'<none>':<8} {state:<14} {'-':<22} {newest_str:<22} {'-'}")
            continue
        start = _process_start_time(pid)
        start_str = start.strftime("%Y-%m-%d %H:%M:%S") if start else "?"
        verdict = "新"
        if newest is not None and start is not None and newest > start:
            verdict = "⚠ 运行旧代码，请 restart"
            old_code = True
        print(f"{label:<8} {str(pid):<8} {'运行中':<14} {start_str:<22} {newest_str:<22} {verdict}")
    print()
    if old_code:
        print("提示：检测到运行旧代码，请执行  python scripts/dev.py restart")
    else:
        print("状态正常")
    _stray_notice(managed)
    return 0


def cmd_clean() -> int:
    dirs, files = _clean_pycache()
    print(f"已清理 {dirs} 个 __pycache__ 目录, {files} 个 *.pyc 文件")
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/dev.py",
        description="开发环境重启助手 (web + worker 子进程管理)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="启动 web 与 worker 子进程")
    sub.add_parser("stop", help="停止 web 与 worker 子进程（含 Node 桥接子进程）")
    sub.add_parser("restart", help="停止 → 清理 __pycache__/*.pyc → 重新启动（加载新代码）")
    sub.add_parser("status", help="查看运行状态与代码新旧")
    sub.add_parser("clean", help="仅清理 __pycache__ 与 *.pyc（不重启进程）")
    args = parser.parse_args(argv)
    handlers = {
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "clean": cmd_clean,
    }
    return handlers[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
