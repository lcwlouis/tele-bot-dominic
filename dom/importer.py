"""Import a V2 workspace into a new DOM_HOME. The source folder is only ever read."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from dom.config import load_config, save_owner
from dom.home import limit_for
from dom.store import Store

# V2 kept these under identity/. Dom does not import them: the new soul is written fresh.
LEGACY_NAMES = ("SOUL.md", "SCHEDULE.md", "TRAITS.json", "presets")


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)   # destination paths, relative to the home
    unresolved: list[str] = field(default_factory=list)  # handle-only profiles: the numeric id is unknown
    over_limit: list[str] = field(default_factory=list)  # copied as is; Dom prunes them later
    skipped: list[str] = field(default_factory=list)     # source files with an unrecognised shape
    legacy: list[str] = field(default_factory=list)      # reference copies under legacy/


def _dedupe_handle(rest: str) -> str:
    """V2 named handle profiles handle_<h>_<h>.md; recover <h> (it may itself contain underscores)."""
    n = (len(rest) - 1) // 2
    if len(rest) % 2 == 1 and rest[n] == "_" and rest[:n] == rest[n + 1 :]:
        return rest[:n]
    return rest


def _person_destination(filename: str) -> tuple[str, bool] | None:
    """(destination relative to the home, is_handle_only) for a V2 people profile."""
    stem = filename.removesuffix(".md")
    uid = re.fullmatch(r"uid_(\d+)(?:_.*)?", stem)
    if uid:
        return f"memory/people/{uid.group(1)}.md", False
    if stem.startswith("handle_"):
        return f"memory/people/handle_{_dedupe_handle(stem[len('handle_'):])}.md", True
    return None


def _legacy_owner_ids(src: Path) -> set[str]:
    """Who owned the V2 workspace: the chats whose STATE.json says admin_dm, else the uid_<id> profile
    that admin/PRIVATE_MEMORY.md refers to."""
    chats_dir = src / "chats"
    found: set[str] = set()
    for chat_dir in sorted(p for p in chats_dir.iterdir() if p.is_dir()) if chats_dir.is_dir() else []:
        try:
            state = json.loads((chat_dir / "STATE.json").read_text())
        except (OSError, ValueError):
            continue
        if isinstance(state, dict) and state.get("chat_type") == "admin_dm":
            found.add(chat_dir.name)
    if found:
        return found
    memory = src / "admin" / "PRIVATE_MEMORY.md"
    return set(re.findall(r"uid_(\d+)_", memory.read_text())) if memory.is_file() else set()


def _skip_links(src: Path, report: ImportReport):
    """copytree ignore callback: leave symlinks out of a copied folder and report them."""
    def ignore(folder: str, names: list[str]) -> list[str]:
        links = [n for n in names if (Path(folder) / n).is_symlink()]
        report.skipped += [f"{(Path(folder) / n).relative_to(src)} (symlink)" for n in links]
        return links
    return ignore


def _regular(src: Path, path: Path, report: ImportReport) -> bool:
    """True for a regular file reached without any symlink. A symlink is skipped and reported."""
    if not path.exists() and not path.is_symlink():
        return False
    if path.resolve() != src.resolve() / path.relative_to(src):
        report.skipped.append(f"{path.relative_to(src)} (symlink)")
        return False
    return path.is_file()


def import_v2(src: Path, home: Path, owner_id: int) -> ImportReport:
    src, home = Path(src), Path(home)
    if (home / "config.toml").is_file() and load_config(home).owner_id not in (None, owner_id):
        raise ValueError("the home already has a different owner")
    if (home / "state.db").exists():
        raise FileExistsError(f"{home / 'state.db'} already exists; import into a fresh home")

    legacy_owners = _legacy_owner_ids(src)
    if legacy_owners - {str(owner_id)}:
        raise ValueError(
            f"the workspace's legacy owner is {', '.join(sorted(legacy_owners))}, not --owner-id {owner_id}; "
            "importing would put their private notes in the wrong place"
        )

    report = ImportReport()
    home.mkdir(parents=True, exist_ok=True)

    def put(rel: str, text: str) -> None:
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        report.imported.append(rel)

    def copy(source: Path, rel: str) -> bool:
        if not _regular(src, source, report):
            return False
        put(rel, source.read_text())
        return True

    for name in ("MEMORY.md", "JOURNAL.md"):
        if (src / "memory" / name).exists():
            copy(src / "memory" / name, f"memory/{name}")

    # people profiles, and the old profile paths recorded in the database
    path_remap: dict[str, str] = {}
    people_dir = src / "memory" / "people"
    for profile in sorted(people_dir.glob("*.md")) if people_dir.is_dir() else []:
        destination = _person_destination(profile.name)
        if destination is None:
            report.skipped.append(f"memory/people/{profile.name}")
            continue
        rel, handle_only = destination
        if not copy(profile, rel):
            continue
        path_remap[f"memory/people/{profile.name}"] = rel
        if handle_only:
            report.unresolved.append(rel)

    # the owner's private notes: admin memory first, then the owner DM's chat context
    private_notes = []
    if _regular(src, src / "admin" / "PRIVATE_MEMORY.md", report):
        private_notes.append((src / "admin" / "PRIVATE_MEMORY.md").read_text().strip())

    chat_traits: dict[str, dict] = {}
    chats_dir = src / "chats"
    for chat_dir in sorted(p for p in chats_dir.iterdir() if p.is_dir()) if chats_dir.is_dir() else []:
        chat_id = chat_dir.name
        context = chat_dir / "CONTEXT.md"
        if _regular(src, context, report):
            if chat_id == str(owner_id):
                private_notes.append(context.read_text().strip())
            else:
                copy(context, f"memory/chats/{chat_id}.md")
        if (chat_dir / "transcript.jsonl").exists():
            copy(chat_dir / "transcript.jsonl", f"transcripts/{chat_id}.jsonl")
        traits_file = chat_dir / "TRAITS.json"
        if _regular(src, traits_file, report):
            try:
                traits = json.loads(traits_file.read_text())
            except ValueError:
                traits = None
            if isinstance(traits, dict):
                chat_traits[chat_id] = traits
            else:
                report.skipped.append(f"chats/{chat_id}/TRAITS.json")
    if private_notes:
        put("private/NOTES.md", "\n\n".join(t for t in private_notes if t))

    # reference copies of what is deliberately not imported
    for base in (src / "identity", src):
        for name in LEGACY_NAMES:
            source = base / name
            if not source.exists() or f"legacy/{name}" in report.legacy:
                continue
            if source.is_symlink():
                report.skipped.append(f"{source.relative_to(src)} (symlink)")
                continue
            target = home / "legacy" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True, ignore=_skip_links(src, report))
            else:
                shutil.copyfile(source, target)
            report.legacy.append(f"legacy/{name}")

    # database: plain file copies (never open the source), then migrate the copy
    for suffix in ("", "-wal"):
        if _regular(src, src / f"state.db{suffix}", report):
            shutil.copyfile(src / f"state.db{suffix}", home / f"state.db{suffix}")
            if suffix == "":
                report.imported.append("state.db")
    asyncio.run(_finish_database(home, owner_id, chat_traits, path_remap))

    if (home / "config.toml").is_file() and load_config(home).owner_id is None:
        save_owner(home, owner_id)
        (home / ".claim").unlink(missing_ok=True)

    for rel in report.imported:
        limit = limit_for(rel)
        if limit is not None and len((home / rel).read_text()) > limit:
            report.over_limit.append(rel)
    return report


async def _finish_database(
    home: Path, owner_id: int, chat_traits: dict[str, dict], path_remap: dict[str, str]
) -> None:
    store = Store(home, owner_id)  # migrates the copy to the current schema
    try:
        for chat_id in chat_traits:
            await store.get_chat_auth_status(chat_id)  # make sure the chat row exists
    finally:
        store.close()
    with sqlite3.connect(home / "state.db") as conn:
        for chat_id, traits in chat_traits.items():
            conn.execute("UPDATE chats SET traits_json = ? WHERE chat_id = ?", (json.dumps(traits), chat_id))
        for old, new in path_remap.items():
            conn.execute("UPDATE people_identities SET profile_path = ? WHERE profile_path = ?", (new, old))
