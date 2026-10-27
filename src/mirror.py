"""Helpers shared by the OneGMS mirror scripts."""
from sqlalchemy import text


def kept_blanks(conn, table, key, columns, rows, schema="aa"):
    """{column: rows} where this run's feed has no value but the table has one: the values
    a coalescing upsert (``col = coalesce(excluded.col, t.col)``) keeps. Read it before the
    upsert, in the same transaction. Rows not yet stored are skipped."""
    cols = [c for c in columns if c != key]
    stored = {r[0]: r[1:] for r in conn.execute(text(
        f"select {key}, {', '.join(cols)} from {schema}.{table}"))}
    kept = {}
    for row in rows:
        old = stored.get(row[key])
        if old is None:
            continue
        for col, v in zip(cols, old):
            if v is not None and row[col] is None:
                kept[col] = kept.get(col, 0) + 1
    return kept
