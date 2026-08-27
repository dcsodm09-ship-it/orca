#!/usr/bin/env python3
"""Install Orca Context Bridge into shared Claude Code and Codex skill roots."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path


SKILL_NAME = "orca-context-bridge"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Orca Context Bridge without overwriting existing skills."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Replace an existing installation of this exact skill.",
    )
    parser.add_argument(
        "--allow-delete",
        action="store_true",
        help=(
            "Permit --update to remove files that exist in the deployed "
            "destination but not in this repo's source tree. Without this "
            "flag, --update refuses and lists such files instead of "
            "silently deleting them."
        ),
    )
    parser.add_argument("--shared-root", type=Path, default=Path.home() / ".agents" / "skills")
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "skills")
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".codex" / "skills")
    return parser


def existing_points_to(path: Path, destination: Path) -> bool:
    if not path.is_symlink():
        return False
    try:
        return path.resolve(strict=False) == destination.resolve(strict=False)
    except OSError:
        return False


def files_only_in_destination(destination: Path, incoming: Path) -> list[str]:
    """Relative paths that exist under ``destination`` but not under ``incoming``.

    ``incoming`` is expected to be the fully-populated replacement tree (the
    temporary copy of ``source``, after the same ignore patterns used for the
    real copy have been applied) so the comparison reflects exactly what the
    destructive rename is about to remove.
    """
    missing: list[str] = []
    if not destination.exists():
        return missing
    for path in destination.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(destination)
        if not (incoming / relative).exists():
            missing.append(str(relative))
    return sorted(missing)


def is_owned_installation(path: Path) -> bool:
    skill_file = path / "SKILL.md"
    try:
        header = skill_file.read_text(encoding="utf-8")[:512]
    except OSError:
        return False
    return "\nname: orca-context-bridge\n" in header


def main() -> int:
    args = build_parser().parse_args()
    source = Path(__file__).resolve().parent.parent
    shared_root = args.shared_root.expanduser().resolve(strict=False)
    destination = shared_root / SKILL_NAME
    link_roots = [
        args.claude_root.expanduser().resolve(strict=False),
        args.codex_root.expanduser().resolve(strict=False),
    ]
    link_paths = [root / SKILL_NAME for root in link_roots]

    conflicts: list[str] = []
    destination_exists = destination.exists() or destination.is_symlink()
    if destination_exists and not (args.update and is_owned_installation(destination)):
        conflicts.append(str(destination))
    for link in link_paths:
        if (link.exists() or link.is_symlink()) and not existing_points_to(link, destination):
            conflicts.append(str(link))
    if conflicts:
        print(json.dumps({"ok": False, "conflicts": conflicts}, indent=2))
        return 2

    result = {
        "ok": True,
        "dry_run": args.dry_run,
        "action": "update" if destination_exists else "install",
        "source": str(source),
        "shared_copy": str(destination),
        "links": [str(path) for path in link_paths],
    }
    if args.dry_run:
        print(json.dumps(result, indent=2))
        return 0

    shared_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.", dir=shared_root))
    previous: Path | None = None
    try:
        shutil.copytree(
            source,
            temporary,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        if destination_exists:
            orphaned = files_only_in_destination(destination, temporary)
            if orphaned and not args.allow_delete:
                print(json.dumps({
                    "ok": False,
                    "error": "update_would_delete_files",
                    "destination": str(destination),
                    "files": orphaned,
                    "hint": "Re-run with --allow-delete to proceed and remove these files.",
                }, indent=2))
                shutil.rmtree(temporary, ignore_errors=True)
                return 3
            if orphaned:
                print(json.dumps({
                    "ok": True,
                    "warning": "deleting_files_not_in_source",
                    "destination": str(destination),
                    "files": orphaned,
                }, indent=2))
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination_exists:
            previous = shared_root / f".{SKILL_NAME}.previous-{os.getpid()}"
            destination.rename(previous)
        temporary.rename(destination)
    except BaseException:
        if previous and previous.exists() and not destination.exists():
            previous.rename(destination)
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    if previous:
        shutil.rmtree(previous, ignore_errors=True)

    for root, link in zip(link_roots, link_paths):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if existing_points_to(link, destination):
            continue
        relative_target = os.path.relpath(destination, root)
        link.symlink_to(relative_target, target_is_directory=True)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
