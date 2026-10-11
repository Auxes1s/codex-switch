#!/usr/bin/env python3

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def create_state(path: Path, rows: list[tuple[str, str, int, str]]) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE _sqlx_migrations (
            version INTEGER PRIMARY KEY, description TEXT NOT NULL,
            installed_on TEXT NOT NULL, success INTEGER NOT NULL,
            checksum BLOB NOT NULL, execution_time INTEGER NOT NULL
        );
        INSERT INTO _sqlx_migrations VALUES (1, 'initial', 'now', 1, X'01', 0);
        CREATE TABLE threads (
            id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
            updated_at_ms INTEGER NOT NULL, title TEXT NOT NULL
        );
        CREATE TABLE backfill_state (
            id INTEGER PRIMARY KEY, status TEXT, last_watermark TEXT,
            last_success_at INTEGER, updated_at INTEGER
        );
        INSERT INTO backfill_state VALUES (1, 'complete', NULL, 1, 1);
        """
    )
    connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?)", rows)
    connection.commit()
    connection.close()


def main() -> int:
    helper = Path(__file__).resolve().parents[1] / "bin" / "codex-switch-migrate.py"
    with tempfile.TemporaryDirectory(prefix="codex-switch-migration-") as root_text:
        root = Path(root_text)
        central = root / ".codex"
        profile = root / ".codex-work"
        central.mkdir()
        profile.mkdir()
        (central / "auth.json").write_text("central-auth\n", encoding="utf-8")
        (profile / "auth.json").write_text("profile-auth\n", encoding="utf-8")
        create_state(central / "state_5.sqlite", [
            ("shared", str(central / "sessions" / "shared.jsonl"), 100, "central title"),
        ])
        connection = sqlite3.connect(central / "state_5.sqlite")
        connection.execute("ALTER TABLE threads ADD COLUMN preview TEXT NOT NULL DEFAULT ''")
        connection.commit()
        connection.close()
        create_state(profile / "state_5.sqlite", [
            ("shared", str(profile / "sessions" / "shared.jsonl"), 200, "new title"),
            ("profile-only", str(profile / "sessions" / "only.jsonl"), 150, "profile title"),
        ])

        for _ in range(2):
            subprocess.run(
                [sys.executable, str(helper), "--active-home", str(central)],
                check=True,
            )

        connection = sqlite3.connect(central / "state_5.sqlite")
        rows = connection.execute(
            "SELECT id, rollout_path, title FROM threads ORDER BY id"
        ).fetchall()
        backfill_count = connection.execute("SELECT count(*) FROM backfill_state").fetchone()[0]
        connection.close()
        assert rows == [
            ("profile-only", str(central / "sessions" / "only.jsonl"), "profile title"),
            ("shared", str(central / "sessions" / "shared.jsonl"), "new title"),
        ], rows
        assert backfill_count == 0
        assert (central / ".codex-switch-legacy-state" / "work" / "sqlite" / "state_5.sqlite").is_file()
        assert (central / ".codex-switch-legacy-state" / "central" / "sqlite" / "state_5.sqlite").is_file()
        assert (central / ".codex-switch-legacy-state" / "work" / "sqlite" / ".merged-v1").is_file()
        assert (central / "auth.json").read_text(encoding="utf-8") == "central-auth\n"
        assert (profile / "auth.json").read_text(encoding="utf-8") == "profile-auth\n"

    # Codex 0.162 added agent_message_board_1.sqlite; legacy copies must merge too.
    with tempfile.TemporaryDirectory(prefix="codex-switch-board-") as root_text:
        root = Path(root_text)
        central = root / ".codex"
        profile = root / ".codex-work"
        central.mkdir()
        profile.mkdir()
        connection = sqlite3.connect(profile / "agent_message_board_1.sqlite")
        connection.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, body TEXT NOT NULL, created_at INTEGER NOT NULL)")
        connection.execute("INSERT INTO messages VALUES ('m1', 'hello', 1)")
        connection.commit()
        connection.close()
        subprocess.run([sys.executable, str(helper), "--active-home", str(central)], check=True)
        connection = sqlite3.connect(central / "agent_message_board_1.sqlite")
        assert connection.execute("SELECT id, body FROM messages").fetchall() == [("m1", "hello")]
        connection.close()
        assert (central / ".codex-switch-legacy-state" / "work" / "sqlite" / "agent_message_board_1.sqlite").is_file()

    if sys.platform != "win32":
        import fcntl
        with tempfile.TemporaryDirectory(prefix="codex-switch-locked-migration-") as root_text:
            root = Path(root_text)
            central = root / ".codex"
            profile = root / ".codex-work"
            lock_directory = profile / "thread-writer-locks"
            central.mkdir()
            lock_directory.mkdir(parents=True)
            create_state(profile / "state_5.sqlite", [
                ("locked", str(profile / "sessions" / "locked.jsonl"), 1, "locked"),
            ])
            lock_handle = (lock_directory / "locked.lock").open("w")
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = subprocess.run(
                    [sys.executable, str(helper), "--active-home", str(central)],
                    capture_output=True,
                    text=True,
                )
                assert result.returncode != 0
                assert "close all Codex sessions" in result.stderr
                assert not (central / "state_5.sqlite").exists()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    for conflict in ("unkeyed", "unique", "equal_timestamp", "missing_table"):
        with tempfile.TemporaryDirectory(prefix="codex-switch-conflict-") as root_text:
            root = Path(root_text)
            central = root / ".codex"
            profile = root / ".codex-work"
            central.mkdir()
            profile.mkdir()
            create_state(central / "state_5.sqlite", [
                ("central", str(central / "sessions" / "central.jsonl"), 100, "same title"),
            ])
            create_state(profile / "state_5.sqlite", [
                (("central" if conflict == "equal_timestamp" else "profile"),
                 str(profile / "sessions" / "profile.jsonl"),
                 (100 if conflict == "equal_timestamp" else 200), "same title"),
            ])
            if conflict == "unkeyed":
                for path in (central / "state_5.sqlite", profile / "state_5.sqlite"):
                    connection = sqlite3.connect(path)
                    connection.execute("CREATE TABLE unkeyed (value TEXT NOT NULL)")
                    connection.execute("INSERT INTO unkeyed VALUES ('must not vanish')")
                    connection.commit()
                    connection.close()
            elif conflict == "missing_table":
                connection = sqlite3.connect(profile / "state_5.sqlite")
                connection.execute("CREATE TABLE old_records (id TEXT PRIMARY KEY)")
                connection.execute("INSERT INTO old_records VALUES ('must not vanish')")
                connection.commit()
                connection.close()
            elif conflict == "unique":
                connection = sqlite3.connect(central / "state_5.sqlite")
                connection.execute("CREATE UNIQUE INDEX threads_title ON threads(title)")
                connection.commit()
                connection.close()
            result = subprocess.run(
                [sys.executable, str(helper), "--active-home", str(central)],
                capture_output=True, text=True,
            )
            assert result.returncode != 0, conflict
            assert "could not merge" in result.stderr, result.stderr
            assert not (central / ".codex-switch-legacy-state" / "work" / "sqlite" / ".merged-v1").exists()
            assert (profile / "state_5.sqlite").exists()
    print("ok - SQLite profile migration passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
