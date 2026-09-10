"""
Apply db/migrations/*.sql in filename order, once each.

Schema changes used to need someone with the database password in a psql or
Supabase SQL editor session, so they piled up (the platform CHECK that broke
the Shiji/Gladstone rollout, the shared-nines de-duplication in
search_tee_times). This runs in the scheduled job instead, where DATABASE_URL
already lives, so a migration ships like any other commit.

Each file runs in its own transaction and is recorded in schema_migrations;
an already-applied file is skipped. A failing migration stops the run, loudly
— it must not be papered over the way the config sync was.

Usage:
    python apply_migrations.py            # needs DATABASE_URL
    python apply_migrations.py --dry-run  # list what would be applied
"""

import argparse
import logging
import sys
from pathlib import Path

import golf_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("golf")

MIGRATIONS_DIR = Path(__file__).parent / "db" / "migrations"


def pending(cur) -> list[Path]:
    cur.execute("""
        create table if not exists schema_migrations (
            filename    text primary key,
            applied_at  timestamptz not null default now()
        )
    """)
    cur.execute("select filename from schema_migrations")
    done = {r[0] for r in cur.fetchall()}
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.name not in done]


def main() -> None:
    ap = argparse.ArgumentParser(description="Apply pending SQL migrations")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not MIGRATIONS_DIR.is_dir():
        log.info("No db/migrations directory — nothing to do")
        return

    with golf_db.get_connection() as conn:
        with conn.cursor() as cur:
            todo = pending(cur)
        conn.commit()

        if not todo:
            log.info("Schema is up to date — no pending migrations")
            return
        log.info(f"{len(todo)} pending migration(s): {', '.join(p.name for p in todo)}")
        if a.dry_run:
            return

        for path in todo:
            sql = path.read_text(encoding="utf-8")
            try:
                with conn.transaction(), conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute("insert into schema_migrations (filename) values (%s)", (path.name,))
            except Exception as e:
                log.error(f"Migration {path.name} FAILED (nothing from it was applied): {e}")
                sys.exit(1)
            log.info(f"Applied {path.name}")


if __name__ == "__main__":
    main()
