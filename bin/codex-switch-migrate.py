#!/usr/bin/env python3
"""Merge legacy profile-local Codex SQLite state into one shared SQLite home."""

from __future__ import annotations

import argparse
import contextlib
import os
from pathlib import Path
import re
import sqlite3
import sys


DATABASES = (
    "state_5.sqlite",
    "thread_history_1.sqlite",
    "goals_1.sqlite",
    "queue_1.sqlite",
    "memories_1.sqlite",
    "logs_2.sqlite",
)
RESERVED_PROFILES = {
    "add", "auth", "current", "help", "list", "login", "logout", "remove",
    "run", "shell-init", "status", "switch",
}
SKIP_TABLES = {"_sqlx_migrations", "sqlite_sequence", "backfill_state",
               "rollout_migration_state", "rollout_migration_skipped_rollouts"}
TIMESTAMP_COLUMNS = (
    "updated_at_ms", "updated_at", "updated_at_ordinal", "recency_at_ms", "generated_at",
    "source_updated_at", "completed_at", "finished_at", "created_at_ms",
    "created_at", "started_at", "last_success_watermark", "rollout_ordinal",
)


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@contextlib.contextmanager
def migration_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.read(1) == b"":
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("another codex-switch migration is running") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("another codex-switch migration is running") from exc
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            with contextlib.suppress(OSError):
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def tables(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]


def columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({quote(table)})")]


def primary_key(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f"PRAGMA table_info({quote(table)})").fetchall()
    return [row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5]]


def unique_keys(connection: sqlite3.Connection, table: str) -> list[list[str]]:
    result = []
    for index in connection.execute(f"PRAGMA index_list({quote(table)})").fetchall():
        if not index[2] or index[3] == "pk":
            continue
        names = [row[2] for row in connection.execute(
            f"PRAGMA index_info({quote(index[1])})"
        ).fetchall()]
        if names:
            result.append(names)
    return result


def migration_signature(connection: sqlite3.Connection) -> list[tuple]:
    if "_sqlx_migrations" not in tables(connection):
        return []
    return connection.execute(
        "SELECT version, description, success, hex(checksum) "
        "FROM _sqlx_migrations ORDER BY version"
    ).fetchall()


def row_timestamp(row: sqlite3.Row, names: list[str]) -> int:
    for candidate in TIMESTAMP_COLUMNS:
        if candidate in names and row[candidate] is not None:
            try:
                return int(row[candidate])
            except (TypeError, ValueError):
                pass
    return 0


def merge_logs(destination: sqlite3.Connection, source: sqlite3.Connection) -> None:
    if "logs" not in tables(source) or "logs" not in tables(destination):
        return
    names = [name for name in columns(destination, "logs") if name != "id"]
    if any(name not in columns(source, "logs") for name in names):
        raise RuntimeError("logs database schemas are incompatible")
    placeholders = ",".join("?" for _ in names)
    insert = f"INSERT INTO logs ({','.join(map(quote, names))}) VALUES ({placeholders})"
    seen = {tuple(row) for row in destination.execute(
        f"SELECT {','.join(map(quote, names))} FROM logs"
    ).fetchall()}
    for row in source.execute(f"SELECT {','.join(map(quote, names))} FROM logs"):
        if tuple(row) not in seen:
            destination.execute(insert, tuple(row))
            seen.add(tuple(row))


def merge_table(destination: sqlite3.Connection, source: sqlite3.Connection, table: str) -> None:
    destination_columns = columns(destination, table)
    source_columns = columns(source, table)
    if destination_columns != source_columns:
        raise RuntimeError(f"incompatible schema for table {table}")
    keys = primary_key(destination, table)
    if not keys:
        return

    destination.row_factory = sqlite3.Row
    source.row_factory = sqlite3.Row
    names_sql = ",".join(map(quote, destination_columns))
    placeholders = ",".join("?" for _ in destination_columns)
    where = " AND ".join(f"{quote(name)} IS ?" for name in keys)
    insert = f"INSERT INTO {quote(table)} ({names_sql}) VALUES ({placeholders})"
    update_names = [name for name in destination_columns if name not in keys]
    update = (
        f"UPDATE {quote(table)} SET "
        + ",".join(f"{quote(name)}=?" for name in update_names)
        + " WHERE " + where
    )
    secondary_keys = unique_keys(destination, table)

    for source_row in source.execute(f"SELECT {names_sql} FROM {quote(table)}"):
        key_values = [source_row[name] for name in keys]
        current = destination.execute(
            f"SELECT {names_sql} FROM {quote(table)} WHERE {where}", key_values
        ).fetchone()
        if current is None:
            try:
                destination.execute(insert, [source_row[name] for name in destination_columns])
            except sqlite3.IntegrityError:
                # A secondary unique key already represents this logical record.
                for unique_names in secondary_keys:
                    unique_where = " AND ".join(f"{quote(name)} IS ?" for name in unique_names)
                    values = [source_row[name] for name in unique_names]
                    conflicting = destination.execute(
                        f"SELECT {names_sql} FROM {quote(table)} WHERE {unique_where}", values
                    ).fetchone()
                    if conflicting is not None and row_timestamp(source_row, destination_columns) > row_timestamp(conflicting, destination_columns):
                        destination.execute(f"DELETE FROM {quote(table)} WHERE {unique_where}", values)
                        destination.execute(insert, [source_row[name] for name in destination_columns])
                        break
        elif row_timestamp(source_row, destination_columns) > row_timestamp(current, destination_columns):
            try:
                destination.execute(
                    update,
                    [source_row[name] for name in update_names] + key_values,
                )
            except sqlite3.IntegrityError:
                continue


def backup_database(source_path: Path, backup_path: Path) -> None:
    if backup_path.exists():
        return
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = backup_path.with_name(backup_path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=10)
    destination = sqlite3.connect(temporary, timeout=10)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.replace(temporary, backup_path)
    with contextlib.suppress(OSError):
        os.chmod(backup_path, 0o600)


def merge_database(source_path: Path, destination_path: Path, active_home: Path,
                   profile_home: Path) -> None:
    if not destination_path.exists():
        backup_database(source_path, destination_path)

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(destination_path, timeout=30)
    try:
        destination.execute("PRAGMA foreign_keys=OFF")
        destination.execute("PRAGMA busy_timeout=30000")
        if migration_signature(source) != migration_signature(destination):
            raise RuntimeError(f"incompatible Codex migrations in {source_path.name}")
        with destination:
            destination_tables = set(tables(destination))
            source_tables = set(tables(source))
            for table in sorted(destination_tables & source_tables):
                if table in SKIP_TABLES:
                    continue
                if table == "logs":
                    merge_logs(destination, source)
                else:
                    merge_table(destination, source, table)

            if source_path.name == "state_5.sqlite" and "threads" in destination_tables:
                source_prefix = str(profile_home)
                destination_prefix = str(active_home)
                destination.execute(
                    "UPDATE threads SET rollout_path = ? || substr(rollout_path, ?) "
                    "WHERE rollout_path = ? OR rollout_path LIKE ?",
                    (destination_prefix, len(source_prefix) + 1,
                     source_prefix, source_prefix + os.sep + "%"),
                )
                if "backfill_state" in destination_tables:
                    destination.execute("DELETE FROM backfill_state")
                if "rollout_migration_state" in destination_tables:
                    destination.execute("DELETE FROM rollout_migration_state")
                if "rollout_migration_skipped_rollouts" in destination_tables:
                    destination.execute("DELETE FROM rollout_migration_skipped_rollouts")
    finally:
        destination.close()
        source.close()


def profile_directories(active_home: Path) -> list[Path]:
    prefix = active_home.name + "-"
    return sorted(
        path for path in active_home.parent.iterdir()
        if path.is_dir() and not path.is_symlink() and path.name.startswith(prefix)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.name[len(prefix):])
        and path.name[len(prefix):].lower() not in RESERVED_PROFILES
    )


def assert_no_active_thread_writers(homes: list[Path]) -> None:
    if os.name == "nt":
        return
    import fcntl
    handles = []
    try:
        seen = set()
        for home in homes:
            lock_directory = home / "thread-writer-locks"
            if not lock_directory.is_dir():
                continue
            for lock_path in lock_directory.glob("*.lock"):
                resolved = lock_path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                handle = lock_path.open("rb")
                handles.append(handle)
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeError(
                        "a Codex session is still writing; close all Codex sessions and retry"
                    ) from exc
    finally:
        for handle in handles:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def migrate_profile(active_home: Path, profile_home: Path) -> None:
    profile_name = profile_home.name[len(active_home.name) + 1:]
    backup_root = active_home / ".codex-switch-legacy-state" / profile_name / "sqlite"
    marker = backup_root / ".merged-v1"
    sources = [name for name in DATABASES if (profile_home / name).is_file()]
    if marker.exists() or not sources:
        return

    central_backup_root = active_home / ".codex-switch-legacy-state" / "central" / "sqlite"
    for name in sources:
        if (active_home / name).is_file():
            backup_database(active_home / name, central_backup_root / name)
        backup_database(profile_home / name, backup_root / name)
    for name in sources:
        merge_database(profile_home / name, active_home / name, active_home, profile_home)
    marker.write_text("merged into shared CODEX_SQLITE_HOME\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(marker, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--active-home", required=True)
    arguments = parser.parse_args()
    active_home = Path(os.path.abspath(os.path.expanduser(arguments.active_home)))
    active_home.mkdir(parents=True, exist_ok=True)
    try:
        with migration_lock(active_home / ".codex-switch-sqlite-migration.lock"):
            profiles = profile_directories(active_home)
            assert_no_active_thread_writers([active_home] + profiles)
            for profile_home in profiles:
                migrate_profile(active_home, profile_home)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"codex-switch: could not merge legacy SQLite state: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
