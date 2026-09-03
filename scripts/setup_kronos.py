from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = PROJECT_ROOT / "config" / "kronos-source.json"
SOURCE_DIR = PROJECT_ROOT / "vendor" / "kronos"


def _run(*args: str) -> None:
    subprocess.run(args, cwd=PROJECT_ROOT, check=True)


def install_source(repository: str, commit: str) -> None:
    if SOURCE_DIR.exists() and not (SOURCE_DIR / ".git").is_dir():
        raise RuntimeError(f"{SOURCE_DIR} 已存在但不是 Git 仓库")
    if not SOURCE_DIR.exists():
        SOURCE_DIR.parent.mkdir(parents=True, exist_ok=True)
        _run("git", "clone", "--filter=blob:none", repository, str(SOURCE_DIR))
    remote = subprocess.run(
        ["git", "-C", str(SOURCE_DIR), "remote", "get-url", "origin"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if remote.rstrip("/").removesuffix(".git") != repository.rstrip("/").removesuffix(".git"):
        raise RuntimeError("现有 Kronos 仓库 origin 与锁定的官方地址不一致")
    _run("git", "-C", str(SOURCE_DIR), "fetch", "origin", commit, "--depth=1")
    _run("git", "-C", str(SOURCE_DIR), "checkout", "--detach", commit)


def install_dependencies() -> None:
    _run(sys.executable, "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements-kronos.txt"))


def download_models(model: str, tokenizer: str) -> None:
    from huggingface_hub import snapshot_download

    for repository in (model, tokenizer):
        location = snapshot_download(repo_id=repository)
        print(f"downloaded {repository} -> {location}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install pinned official Kronos mini runtime")
    parser.add_argument("--skip-dependencies", action="store_true")
    parser.add_argument("--skip-models", action="store_true")
    args = parser.parse_args(argv)
    lock = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    install_source(lock["repository"], lock["commit"])
    if not args.skip_dependencies:
        install_dependencies()
    if not args.skip_models:
        download_models(lock["model"], lock["tokenizer"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
