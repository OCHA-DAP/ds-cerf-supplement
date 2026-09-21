"""Export the CBPF mirror's structure for the ERD page → site/mirror/meta.json.

Combines the registry (source, fan-out, group, declared keys, join-by-convention
edges, descriptions) with live dev-DB introspection (columns + types, row counts,
last refresh from cbpf.mirror_run) for schema ``cbpf`` AND the five AA-facing
normalized tables in schema ``aa`` (``aa.cbpf_*``), so the page shows the whole
CBPF mirror. Read-only; run by deploy-site.yml. If the DB is unreachable the
registry-only structure is still written (row counts blank).

Run:  python scripts/export_cbpf_erd.py [--out site/mirror/meta.json]
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cbpf_api, cbpf_registry  # noqa: E402

# The normalized AA-facing tables (schema aa) — hand-described; their scripts own them.
AA_TABLES = [
    dict(name="cbpf_fund", schema="aa", group="fund", kind="norm", source="MstPooledFund → refresh_cbpf.py",
         key=["pf_id"], joins=[["parent_pf_id", "aa.cbpf_fund.pf_id"], ["pf_id", "cbpf.mst_pooled_fund.PFId"]],
         desc="Normalized fund registry (46) incl. regional envelopes and children."),
    dict(name="cbpf_allocation", schema="aa", group="allocation", kind="norm", source="AllocationTypes → refresh_cbpf.py",
         key=["pooled_fund_id", "allocation_type_id"],
         joins=[["pooled_fund_id", "aa.cbpf_fund.pf_id"], ["allocation_type_id", "cbpf.allocation_types.AllocationTypeId"]],
         desc="CBPF/RhPF allocation envelopes (CERF rows excluded) + deterministic aa_keyword flag; feeds aa.v_allocation."),
    dict(name="cbpf_project", schema="aa", group="project", kind="norm", source="ProjectSummary (vo2) → refresh_cbpf_projects.py",
         key=["chf_project_code"],
         joins=[["pooled_fund_id", "aa.cbpf_fund.pf_id"], ["allocation_type_id", "aa.cbpf_allocation.allocation_type_id"],
                ["chf_project_code", "cbpf.pf_proj_summary_v4.ChfProjectCode"]],
         desc="One row per project = one grant to one implementing partner (16.3k): org, type, budget, dates, status, M/W/B/G."),
    dict(name="cbpf_project_cluster", schema="aa", group="project", kind="norm", source="ProjectSummary (vo2) → refresh_cbpf_projects.py",
         key=["chf_project_code", "cluster"], joins=[["chf_project_code", "aa.cbpf_project.chf_project_code"]],
         desc="Project × cluster budget split."),
    dict(name="cbpf_project_subip", schema="aa", group="partner", kind="norm", source="ProjectSummary (vo2) → refresh_cbpf_projects.py",
         key=["chf_project_code", "subip_name", "subip_amount"], joins=[["chf_project_code", "aa.cbpf_project.chf_project_code"]],
         desc="Sub-implementing partners (##-delimited feed cells exploded) — the CBPF sub-grant layer."),
]


def registry_tables():
    out = []
    for s in cbpf_registry.ALL:
        src = s.source if s.kind != "es" else f"{s.version}/{s.source}"
        if s.kind == "sp":
            src = f"SPCode={s.source}"
        elif s.kind == "bdt":
            src = f"BDT /{s.source}/"
        fan = s.fanout or ("param" if s.param_fanout else None)
        out.append(dict(
            name=s.name, schema="cbpf", group=s.group, kind=s.kind, source=src, fanout=fan,
            params={k: ("" if v is None else v) for k, v in s.params.items()},
            key=[cbpf_api.snake(k) for k in s.key],
            joins=[[cbpf_api.snake(a), _qualify(b)] for a, b in s.joins],
            desc=s.desc,
        ))
    for name, group, desc, sql in cbpf_registry.VIEWS:
        # a view's "source" is the tables it reads; the ERD joins it to them
        reads = sorted(set(re.findall(r"cbpf\.([a-z0-9_]+)", sql)))
        out.append(dict(
            name=name, schema="cbpf", group=group, kind="view",
            source="view over " + ", ".join(reads), fanout=None, params={}, key=[],
            joins=[[ "template_name", f"cbpf.{t}.template_name"] for t in reads if t != "bdt_template"]
                  + [["group_name", "cbpf.bdt_template.group_names"]],
            desc=desc,
        ))
    return out


def _qualify(ref: str) -> str:
    tbl, col = ref.split(".", 1)
    return f"cbpf.{tbl}.{cbpf_api.snake(col)}"


def introspect(tables):
    import ocha_stratus as stratus
    from sqlalchemy import text
    eng = stratus.get_engine(stage="dev")
    with eng.connect() as c:
        cols = {}
        for sch, tname, cname, dtype in c.execute(text("""
            select table_schema, table_name, column_name, data_type
            from information_schema.columns
            where table_schema = 'cbpf' or (table_schema = 'aa' and table_name like 'cbpf\\_%')
            order by table_schema, table_name, ordinal_position""")):
            cols.setdefault((sch, tname), []).append([cname, _short(dtype)])
        runs = {r[0]: dict(fetched_at=r[1].isoformat(), rows=r[2], requests=r[3], seconds=float(r[4] or 0),
                           key_unique=r[5], api_last_modified=r[6].isoformat() if r[6] else None)
                for r in c.execute(text("""
                    select distinct on (table_name) table_name, fetched_at, n_rows, n_requests, seconds,
                           key_unique, api_last_modified
                    from cbpf.mirror_run order by table_name, fetched_at desc"""))}
        for t in tables:
            t["columns"] = cols.get((t["schema"], t["name"]), [])
            if t.get("kind") == "view" and t["columns"]:
                t["rows"] = c.execute(text(f"select count(*) from cbpf.{t['name']}")).scalar()
            elif t["schema"] == "cbpf":
                t.update(runs.get(t["name"], {}))
            elif t["columns"]:
                t["rows"] = c.execute(text(f"select count(*) from aa.{t['name']}")).scalar()
    return tables


def _short(dtype: str) -> str:
    return {"character varying": "text", "timestamp with time zone": "timestamptz",
            "timestamp without time zone": "timestamp", "double precision": "float",
            "bigint": "bigint", "integer": "int", "numeric": "numeric", "boolean": "bool",
            "text": "text", "json": "json", "date": "date"}.get(dtype, dtype)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "site", "mirror", "meta.json"))
    ap.add_argument("--no-db", action="store_true")
    args = ap.parse_args()
    tables = registry_tables() + AA_TABLES
    db_ok = False
    if not args.no_db:
        try:
            introspect(tables)
            db_ok = True
        except Exception as e:  # noqa: BLE001
            print(f"DB introspection failed ({e}); writing registry-only meta", file=sys.stderr)
    api_lm = None
    try:
        api_lm = cbpf_api.last_modified()
    except Exception:  # noqa: BLE001
        pass
    meta = dict(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        db_introspected=db_ok, api_last_modified=api_lm,
        groups=dict(fund="Funds", allocation="Allocations", project="Projects", partner="Partners",
                    people="People", reporting="Reporting", finance="Finance", master="Masters", bdt="BDT (deduplicated)"),
        tables=tables,
    )
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(meta, f, indent=1, default=str)
    print(f"wrote {args.out}: {len(tables)} tables ({sum(1 for t in tables if t.get('columns'))} introspected)")


if __name__ == "__main__":
    main()
