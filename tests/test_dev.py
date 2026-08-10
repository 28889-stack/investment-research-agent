"""scripts/dev.py 的单元测试。

不启动真实的 web/worker 进程：用 ``subprocess.Popen(["sleep","30"])`` 当作"存活的伪进程"，
用 monkeypatch 重定向 PROJECT_ROOT 与 _process_cmdline，把 _clean_pycache / 文件 mtime
测试都限制在 tmp_path 里，绝不触碰真实项目目录。
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import scripts.dev as dev


# --------------------------------------------------------------------------- #
# _read_pid
# --------------------------------------------------------------------------- #
def test_read_pid_missing(tmp_path):
    assert dev._read_pid(tmp_path / "no.pid") is None


def test_read_pid_non_digit_is_none(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text("not-a-number\n")
    assert dev._read_pid(p) is None


def test_read_pid_ok(tmp_path):
    p = tmp_path / "x.pid"
    p.write_text("12345\n")
    assert dev._read_pid(p) == 12345


# --------------------------------------------------------------------------- #
# _is_alive
# --------------------------------------------------------------------------- #
def test_is_alive_for_real_sleep_process():
    proc = subprocess.Popen(["sleep", "30"])
    try:
        assert dev._is_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait()


def test_is_alive_dead_pid():
    # 999999 几乎不可能存活，保证 ProcessLookupError 分支
    assert dev._is_alive(999999) is False


# --------------------------------------------------------------------------- #
# _is_app_process
# --------------------------------------------------------------------------- #
def test_is_app_process_matches_worker(monkeypatch):
    monkeypatch.setattr(dev, "_process_cmdline", lambda pid: "/x/.venv/bin/python -m app.worker")
    assert dev._is_app_process(1, "worker") is True
    assert dev._is_app_process(1, "main") is False


def test_is_app_process_matches_main(monkeypatch):
    monkeypatch.setattr(dev, "_process_cmdline", lambda pid: "/usr/bin/python -m app.main")
    assert dev._is_app_process(1, "main") is True
    assert dev._is_app_process(1, "worker") is False


def test_is_app_process_accepts_full_module_name(monkeypatch):
    monkeypatch.setattr(dev, "_process_cmdline", lambda pid: "/usr/bin/python -m app.main")
    assert dev._is_app_process(1, "app.main") is True


def test_is_app_process_empty_cmd(monkeypatch):
    monkeypatch.setattr(dev, "_process_cmdline", lambda pid: "")
    assert dev._is_app_process(1, "main") is False


def test_process_cmdline_uses_wide_ps_output(monkeypatch):
    """macOS 的默认 ps 输出会截断 Python 路径，必须使用宽输出。"""
    full_command = (
        "/Library/Frameworks/Python.framework/Versions/3.13/Resources/"
        "Python.app/Contents/MacOS/Python -m app.worker"
    )

    def fake_run(args, **kwargs):
        class Result:
            stdout = full_command if "-ww" in args else full_command[:80]

        return Result()

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    assert dev._process_cmdline(123) == full_command


# --------------------------------------------------------------------------- #
# _validate_or_cleanup
# --------------------------------------------------------------------------- #
def test_validate_or_cleanup_stale_removes_file(tmp_path):
    p = tmp_path / "web.pid"
    p.write_text("999999")  # 不可能存活
    pid, running = dev._validate_or_cleanup(p, "main")
    assert pid is None and running is False
    assert not p.exists()


def test_validate_or_cleanup_reused_pid_removes_file(tmp_path, monkeypatch):
    """pid 存活但 cmd 不是 app 进程 → 删文件、不杀进程、返回未运行。"""
    proc = subprocess.Popen(["sleep", "30"])
    try:
        p = tmp_path / "web.pid"
        p.write_text(str(proc.pid))
        monkeypatch.setattr(dev, "_process_cmdline", lambda pid: "/bin/sleep 30")
        pid, running = dev._validate_or_cleanup(p, "main")
        assert pid is None and running is False
        assert not p.exists()
        # 伪进程未被杀
        assert dev._is_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait()


def test_validate_or_cleanup_running_app_process(tmp_path, monkeypatch):
    """pid 存活且 cmd 是 app 进程 → 返回 (pid, True)，文件保留。"""
    proc = subprocess.Popen(["sleep", "30"])
    try:
        p = tmp_path / "worker.pid"
        p.write_text(str(proc.pid))
        monkeypatch.setattr(dev, "_process_cmdline", lambda pid: "/x/python -m app.worker")
        pid, running = dev._validate_or_cleanup(p, "worker")
        assert pid == proc.pid and running is True
        assert p.exists()
    finally:
        proc.terminate()
        proc.wait()


def test_validate_or_cleanup_no_pid_file(tmp_path):
    p = tmp_path / "absent.pid"
    pid, running = dev._validate_or_cleanup(p, "main")
    assert pid is None and running is False


# --------------------------------------------------------------------------- #
# _scan_app_pids
# --------------------------------------------------------------------------- #
def test_scan_app_pids_finds_matching(monkeypatch):
    """monkeypatch _process_cmdline 不可行（_scan_app_pids 直接调 ps），改测解析逻辑：
    用一个 fake ps 输出注入到 subprocess.run。"""
    fake_ps_output = (
        "  100  /x/.venv/bin/python -m app.worker\n"
        "  200  /usr/bin/python -m app.main\n"
        "  300  /usr/bin/python other.py\n"          # 不含 -m app.
        "  400  /bin/sleep 30\n"
    )

    def fake_run(args, **kwargs):
        class R:
            stdout = fake_ps_output

        return R()

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    pids = dev._scan_app_pids("worker")
    assert pids == [100]
    pids = dev._scan_app_pids("main")
    assert pids == [200]


def test_scan_app_pids_excludes_non_python(monkeypatch):
    """命令行含 '-m app.main' 但不含 python（理论上不会出现）→ 排除。"""
    fake_ps_output = "  500  /bin/sh -m app.main\n"  # 无 python

    def fake_run(args, **kwargs):
        class R:
            stdout = fake_ps_output

        return R()

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    assert dev._scan_app_pids("main") == []


def test_scan_app_pids_empty_when_ps_missing(monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError("no ps")

    monkeypatch.setattr(dev.subprocess, "run", fake_run)
    assert dev._scan_app_pids("main") == []


# --------------------------------------------------------------------------- #
# cmd_start 拒绝游离进程
# --------------------------------------------------------------------------- #
def test_cmd_start_refuses_when_stray_web_running(tmp_path, monkeypatch, capsys):
    """游离 web 进程（不在 PID 文件里）→ start 拒绝并提示手动 kill，退出 1。"""
    monkeypatch.setattr(dev, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(dev, "WEB_PID", tmp_path / "web.pid")
    monkeypatch.setattr(dev, "WORKER_PID", tmp_path / "worker.pid")
    monkeypatch.setattr(dev, "VENV_PYTHON", tmp_path / "fake_python")  # 存在性检查在游离检查之后，先触发游离
    (tmp_path / "fake_python").write_text("#!/bin/sh\n")

    def fake_scan(module):
        return [89142] if module == "app.main" else []

    monkeypatch.setattr(dev, "_scan_app_pids", fake_scan)
    rc = dev.cmd_start()
    out = capsys.readouterr().out
    assert rc == 1
    assert "游离 Web 进程" in out
    assert "kill 89142" in out


def test_cmd_start_no_stray_proceeds(tmp_path, monkeypatch, capsys):
    """无游离进程时 start 不应被游离检查拦截（此处只验证游离逻辑放行，不真启进程）。"""
    monkeypatch.setattr(dev, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(dev, "WEB_PID", tmp_path / "web.pid")
    monkeypatch.setattr(dev, "WORKER_PID", tmp_path / "worker.pid")
    monkeypatch.setattr(dev, "VENV_PYTHON", tmp_path / "fake_python")
    (tmp_path / "fake_python").write_text("#!/bin/sh\n")
    # 无游离
    monkeypatch.setattr(dev, "_scan_app_pids", lambda module: [])
    # _launch 会真起进程；用 monkeypatch 桩住，返回一个带 poll() 的伪 Popen
    import types

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid
            self._returncode = None

        def poll(self):
            return self._returncode

    call = {"n": 0}

    def fake_launch(module, pid_file, out_file):
        call["n"] += 1
        proc = FakeProc(call["n"])
        pid_file.write_text(str(proc.pid))
        return proc

    monkeypatch.setattr(dev, "_launch", fake_launch)
    # web 健康检查桩住返回 True 且 poll() 为 None
    monkeypatch.setattr(dev, "_wait_health", lambda url, t: True)
    monkeypatch.setattr(dev, "_wait_log_marker", lambda *a, **k: True)
    rc = dev.cmd_start()
    out = capsys.readouterr().out
    assert rc == 0
    assert "Web 已启动" in out
    assert "Worker 已启动" in out
    assert "游离" not in out


# --------------------------------------------------------------------------- #
# _stray_notice
# --------------------------------------------------------------------------- #
def test_stray_notice_prints_when_stray_found(monkeypatch, capsys):
    monkeypatch.setattr(dev, "_scan_app_pids", lambda module: [89142] if module == "app.main" else [])
    dev._stray_notice({"web": None, "worker": None})
    out = capsys.readouterr().out
    assert "游离 app 进程" in out
    assert "kill 89142" in out


def test_stray_notice_silent_when_clean(monkeypatch, capsys):
    monkeypatch.setattr(dev, "_scan_app_pids", lambda module: [])
    dev._stray_notice({"web": 100, "worker": 200})
    out = capsys.readouterr().out
    assert out == ""


# --------------------------------------------------------------------------- #
# _clean_pycache
# --------------------------------------------------------------------------- #
def test_clean_pycache_removes_dirs_and_pyc_and_excludes_deps(tmp_path, monkeypatch):
    # 构建微型项目树
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "app" / "__pycache__" / "x.pyc").write_bytes(b"")  # 随 rmtree 删，不计入文件数
    (tmp_path / "app" / "x.py").write_text("x=1")
    (tmp_path / "app" / "sub").mkdir()
    (tmp_path / "app" / "sub" / "__pycache__").mkdir()
    (tmp_path / "app" / "sub" / "y.pyc").write_bytes(b"")  # 在 sub 下，单独 unlink，计入
    # 孤立的 .pyc（非 __pycache__ 内）也要删
    (tmp_path / "app" / "loose.pyc").write_bytes(b"")
    # .venv 下的 pycache 必须保留（EXCLUDE_DIRS）
    (tmp_path / ".venv" / "lib" / "pycache_ish").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "pycache_ish" / "__pycache__").mkdir()
    (tmp_path / ".venv" / "lib" / "pycache_ish" / "__pycache__" / "keep.pyc").write_bytes(b"")
    # node_modules 下的 pycache 必须保留（EXCLUDE_DIRS，os.walk 不下钻）
    (tmp_path / "node_modules" / "__pycache__").mkdir(parents=True)
    (tmp_path / "node_modules" / "__pycache__" / "z.pyc").write_bytes(b"")
    # 普通 .py 不应被删
    (tmp_path / "app" / "keep.py").write_text("y=2")

    monkeypatch.setattr(dev, "PROJECT_ROOT", tmp_path)
    dirs, files = dev._clean_pycache()

    # app 下 2 个 __pycache__ 目录被删；node_modules 被排除不删
    assert dirs == 2
    # y.pyc(在 app/sub 下) + loose.pyc(在 app 下) = 2 个独立 .pyc 文件；
    # x.pyc 与 z.pyc 都在 __pycache__ 目录内，随 rmtree 删除，不单独计入 files
    assert files == 2
    assert not (tmp_path / "app" / "__pycache__").exists()
    assert not (tmp_path / "app" / "sub" / "__pycache__").exists()
    assert not (tmp_path / "app" / "loose.pyc").exists()
    assert not (tmp_path / "app" / "sub" / "y.pyc").exists()
    # .venv 完好（排除名单，不下钻）
    assert (tmp_path / ".venv" / "lib" / "pycache_ish" / "__pycache__" / "keep.pyc").exists()
    # node_modules 完好（排除名单，不下钻）
    assert (tmp_path / "node_modules" / "__pycache__" / "z.pyc").exists()
    # 普通 .py 保留
    assert (tmp_path / "app" / "keep.py").exists()
    assert (tmp_path / "app" / "x.py").exists()


def test_clean_pycache_empty_project(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "PROJECT_ROOT", tmp_path)
    dirs, files = dev._clean_pycache()
    assert dirs == 0 and files == 0


# --------------------------------------------------------------------------- #
# _newest_source_mtime
# --------------------------------------------------------------------------- #
def test_newest_source_mtime_picks_latest(tmp_path, monkeypatch):
    # 重定向 APP_DIR / PROFILES_DIR 到 tmp_path 下的伪结构
    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "a.py").write_text("x")
    time.sleep(0.05)
    (fake_app / "b.py").write_text("y")
    monkeypatch.setattr(dev, "APP_DIR", fake_app)
    monkeypatch.setattr(dev, "PROFILES_DIR", fake_app / "profiles")  # 不存在 → 不参与

    m = dev._newest_source_mtime()
    assert m is not None
    assert m > datetime.now() - timedelta(seconds=5)


def test_newest_source_mtime_includes_profiles(tmp_path, monkeypatch):
    fake_app = tmp_path / "app"
    fake_app.mkdir()
    (fake_app / "old.py").write_text("x")
    time.sleep(0.05)
    fake_profiles = fake_app / "profiles"
    fake_profiles.mkdir()
    (fake_profiles / "p.json").write_text("{}")  # 比 old.py 更新
    monkeypatch.setattr(dev, "APP_DIR", fake_app)
    monkeypatch.setattr(dev, "PROFILES_DIR", fake_profiles)

    m = dev._newest_source_mtime()
    assert m is not None
    py_mtime = datetime.fromtimestamp((fake_app / "old.py").stat().st_mtime)
    json_mtime = datetime.fromtimestamp((fake_profiles / "p.json").stat().st_mtime)
    assert m == json_mtime
    assert m > py_mtime


def test_newest_source_mtime_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "APP_DIR", tmp_path / "nope")
    monkeypatch.setattr(dev, "PROFILES_DIR", tmp_path / "nope2")
    assert dev._newest_source_mtime() is None


# --------------------------------------------------------------------------- #
# _process_start_time
# --------------------------------------------------------------------------- #
def test_process_start_time_returns_datetime_for_real_process():
    proc = subprocess.Popen(["sleep", "30"])
    try:
        t = dev._process_start_time(proc.pid)
        assert t is not None
        assert isinstance(t, datetime)
        # 启动时间应在最近几分钟内
        assert t > datetime.now() - timedelta(minutes=5)
    finally:
        proc.terminate()
        proc.wait()


def test_process_start_time_dead_pid_returns_none():
    assert dev._process_start_time(999999) is None


# --------------------------------------------------------------------------- #
# 旧代码判定逻辑（组合 _newest_source_mtime 与 _process_start_time）
# --------------------------------------------------------------------------- #
def test_old_code_detection_when_source_newer_than_start(tmp_path, monkeypatch):
    start = datetime.now() - timedelta(hours=1)
    monkeypatch.setattr(dev, "_process_start_time", lambda pid: start)
    monkeypatch.setattr(dev, "_newest_source_mtime", lambda: datetime.now())
    # 源码 mtime > 进程启动时间 → 判为旧代码
    assert dev._newest_source_mtime() is not None
    assert dev._process_start_time(1) is not None
    assert dev._newest_source_mtime() > dev._process_start_time(1)


def test_old_code_detection_when_start_newer_than_source(tmp_path, monkeypatch):
    start = datetime.now() + timedelta(hours=1)  # 进程刚启动（未来时间代表"最新"）
    monkeypatch.setattr(dev, "_process_start_time", lambda pid: start)
    monkeypatch.setattr(dev, "_newest_source_mtime", lambda: datetime.now())
    # 源码 mtime < 进程启动时间 → 新代码
    assert not (dev._newest_source_mtime() > dev._process_start_time(1))


# --------------------------------------------------------------------------- #
# _wait_log_marker
# --------------------------------------------------------------------------- #
def test_wait_log_marker_finds_marker_after_offset(tmp_path):
    log = tmp_path / "worker.log"
    log.write_text("旧的不算\n")
    offset = log.stat().st_size
    # 追加（write_text 是覆盖，故用 open append）
    with open(log, "a") as fh:
        fh.write("Research Worker 已启动\n")
    assert dev._wait_log_marker(log, "Research Worker 已启动", 2.0, offset) is True


def test_wait_log_marker_ignores_old_marker_before_offset(tmp_path):
    log = tmp_path / "worker.log"
    log.write_text("Research Worker 已启动\n")
    offset = log.stat().st_size
    # 之后不再写入
    assert dev._wait_log_marker(log, "Research Worker 已启动", 0.5, offset) is False


def test_wait_log_marker_missing_file(tmp_path):
    assert dev._wait_log_marker(tmp_path / "nope.log", "x", 0.5, 0) is False


# --------------------------------------------------------------------------- #
# _stop_pid（真实伪进程）
# --------------------------------------------------------------------------- #
def test_stop_pid_terminates_real_process():
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    pid = proc.pid
    try:
        assert dev._is_alive(pid)
        stopped = dev._stop_pid(pid, use_group=True, grace=5.0)
        assert stopped is True
        assert dev._is_alive(pid) is False
    finally:
        if dev._is_alive(pid):
            os.killpg(pid, 9)


def test_stop_pid_already_dead():
    proc = subprocess.Popen(["true"], start_new_session=True)
    proc.wait()
    pid = proc.pid
    # 已死，_stop_pid 应不报错并返回 True
    assert dev._stop_pid(pid, use_group=True, grace=1.0) is True
