#!/usr/bin/env python3
"""Install Orca Context Bridge into shared Claude Code and Codex skill roots."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
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
            "Permit --update to destroy deployed content: files that exist in "
            "the deployed destination but not in this repo's source tree, and "
            "paths whose node type differs between the two trees (a deployed "
            "file that is a directory in source, a deployed symlink replaced "
            "by a real file, ...). Without this flag, --update refuses and "
            "lists them instead of silently destroying them."
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


def node_kind(path: Path) -> str:
    """The kind of filesystem node at ``path`` itself, never its symlink target.

    ``os.lstat`` is deliberate: ``Path.exists``/``Path.is_dir``/``Path.is_file``
    all follow symlinks, so they cannot tell a deployed symlink apart from the
    plain file or directory it happens to point at, and they answer "yes, that
    path exists" for a file and a directory alike. Both blind spots let a
    destructive replace slip past the guard below.

    Returns one of ``file``, ``directory``, ``symlink``, ``other`` (fifo,
    socket, device, ...), or ``absent`` (nothing there, or unreadable).
    """
    try:
        info = os.lstat(path)
    except (OSError, ValueError):
        return "absent"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "file"
    return "other"


def destination_changes(destination: Path, incoming: Path) -> list[dict[str, str]]:
    """Everything under ``destination`` the replace would destroy, not overwrite.

    ``incoming`` is expected to be the fully-populated replacement tree (the
    temporary copy of ``source``, after the same ignore patterns used for the
    real copy have been applied) so the comparison reflects exactly what the
    destructive rename is about to remove.

    A path counts as destroyed when the node *type* at that path differs
    between the two trees, which covers both the plain case (absent from the
    incoming tree entirely) and the type-change cases (a live file whose path
    is a directory in the incoming tree, a live directory that becomes a file,
    a live symlink replaced by a real file or directory). Type changes matter
    because the deployed content disappears just as completely as an outright
    deletion does, while ``exists()`` reports "present on both sides" and lets
    the update through. Matching types are left alone: replacing a file with a
    file, or a symlink with a symlink, is the ordinary overwrite that
    ``--update`` exists to perform.

    Each entry is ``{"path", "reason", "deployed", "source"}`` where ``reason``
    is ``missing_from_source`` or ``type_change``.
    """
    changes: list[dict[str, str]] = []
    destination_kind = node_kind(destination)
    if destination_kind == "absent":
        return changes
    incoming_kind = node_kind(incoming)
    if destination_kind != incoming_kind:
        # The deployed root itself is not what the incoming root is — most
        # plausibly a symlinked installation about to become a real directory.
        return [{
            "path": ".",
            "reason": "missing_from_source" if incoming_kind == "absent" else "type_change",
            "deployed": destination_kind,
            "source": incoming_kind,
        }]
    if destination_kind != "directory":
        return changes
    _collect_changes(destination, incoming, None, changes)
    return sorted(changes, key=lambda change: change["path"])


def _collect_changes(
    destination: Path,
    incoming: Path,
    relative: Path | None,
    changes: list[dict[str, str]],
) -> None:
    current = destination if relative is None else destination / relative
    try:
        names = sorted(entry.name for entry in current.iterdir())
    except OSError:
        return
    for name in names:
        child = Path(name) if relative is None else relative / name
        deployed_kind = node_kind(current / name)
        source_kind = node_kind(incoming / child)
        if deployed_kind != source_kind:
            changes.append({
                "path": str(child),
                "reason": "missing_from_source" if source_kind == "absent" else "type_change",
                "deployed": deployed_kind,
                "source": source_kind,
            })
            # The whole subtree goes with it; reporting the root is enough.
            continue
        if deployed_kind == "directory":
            _collect_changes(destination, incoming, child, changes)


def files_only_in_destination(destination: Path, incoming: Path) -> list[str]:
    """Relative paths of everything :func:`destination_changes` would destroy."""
    return [change["path"] for change in destination_changes(destination, incoming)]


def describe_changes(changes: list[dict[str, str]]) -> list[str]:
    """One human-readable line per change, so the refusal says what kind it is."""
    lines = []
    for change in changes:
        if change["reason"] == "missing_from_source":
            lines.append(f"{change['path']}: deployed {change['deployed']} is absent from source")
        else:
            lines.append(
                f"{change['path']}: deployed {change['deployed']} becomes "
                f"{change['source']} in source"
            )
    return lines


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
            destroyed = destination_changes(destination, temporary)
            if destroyed and not args.allow_delete:
                print(json.dumps({
                    "ok": False,
                    "error": "update_would_delete_files",
                    "destination": str(destination),
                    "files": [change["path"] for change in destroyed],
                    "changes": destroyed,
                    "details": describe_changes(destroyed),
                    "hint": "Re-run with --allow-delete to proceed and remove these files.",
                }, indent=2))
                shutil.rmtree(temporary, ignore_errors=True)
                return 3
            if destroyed:
                print(json.dumps({
                    "ok": True,
                    "warning": "deleting_files_not_in_source",
                    "destination": str(destination),
                    "files": [change["path"] for change in destroyed],
                    "changes": destroyed,
                    "details": describe_changes(destroyed),
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
