"""Read-only archive of PAPER backtest inputs."""

from __future__ import annotations

import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from psycopg2 import sql
from starlette.background import BackgroundTask

from . import db


_PAPER_TABLE = re.compile(
    r"(?:paper_decisions|decisions|fusion_decisions|paper_history|fusion_history|"
    r"round_history|rounds_history|fusion_rounds|"
    r"fusion_snapshots(?:_v[0-9]+)?|"
    r"fusion222_v[0-9a-z_]+_(?:snapshots|decisions))\Z"
)


def _tables_available(cur) -> list[str]:
    cur.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    return sorted(
        name for (name,) in cur.fetchall()
        if _PAPER_TABLE.fullmatch(name) and not name.startswith("fusion222_real_")
    )


def _chunks(file):
    while True:
        chunk = file.read(64 * 1024)
        if not chunk:
            return
        yield chunk


def archive():
    """Create a consistent database snapshot without materializing rows in RAM."""
    tmp = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with db.conn() as connection:
            with connection.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                tables = _tables_available(cur)
                if not tables:
                    raise HTTPException(status_code=404, detail="No PAPER history tables available")
                counts: dict[str, int] = {}
                with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                    for name in tables:
                        cur.execute(
                            sql.SQL("SELECT COUNT(*) FROM public.{}").format(sql.Identifier(name))
                        )
                        counts[name] = int(cur.fetchone()[0])
                        query = sql.SQL("COPY (SELECT * FROM public.{}) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)").format(
                            sql.Identifier(name)
                        )
                        with bundle.open(name + ".csv", "w") as destination:
                            cur.copy_expert(query.as_string(connection), destination)
                    bundle.writestr("manifest.json", json.dumps({
                        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                        "tables": tables,
                        "row_counts": counts,
                        "scope": "PAPER historical decisions, rounds and snapshots; REAL excluded",
                    }, indent=2))
            connection.rollback()
        tmp.seek(0)
        return StreamingResponse(
            _chunks(tmp),
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="fusion-paper-backtest-inputs.zip"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
            background=BackgroundTask(tmp.close),
        )
    except Exception:
        tmp.close()
        raise
