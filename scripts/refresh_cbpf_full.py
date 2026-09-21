"""Refresh the complete raw mirror of the public CBPF API into dev schema ``cbpf``.

One table per public surface of cbpfapi.unocha.org (vo3 + surviving vo1 entity
sets, the 33 public GlobalGenericDataExtract stored queries) plus the public
Beneficiary Data Tool routes — every table is declared in ``src/cbpf_registry.py``
and loaded by ``src/cbpf_mirror.py`` (typed columns, snake_case names, full-replace
per table in its own transaction, a ``cbpf.mirror_run`` log row per table).

The five AA-facing normalized tables in schema ``aa`` (``aa.cbpf_allocation``,
``aa.cbpf_fund``, ``aa.cbpf_project*``) are NOT touched — they keep their own
scripts (``refresh_cbpf.py`` / ``refresh_cbpf_projects.py``) and consumers.

~1,000 API requests (34 funds × the per-fund tables); 30–60 min end to end.

Auth: ocha-stratus get_engine(write=True); DSCI_AZ_DB_DEV_*(_WRITE) env vars.

Run:
    python scripts/refresh_cbpf_full.py                    # everything
    python scripts/refresh_cbpf_full.py --only pf_proj_summary_v4,pf_org_summary
    python scripts/refresh_cbpf_full.py --group master     # one ERD group
    python scripts/refresh_cbpf_full.py --dry-run --funds SUD15,AFG10   # fetch only, 2 funds
    python scripts/refresh_cbpf_full.py --list             # print the registry
"""
import argparse
import os
import sys
import time

os.environ.setdefault("PGSSLMODE", "require")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cbpf_mirror, cbpf_registry  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="comma-separated table names")
    ap.add_argument("--group", help="only tables in this ERD group (fund|allocation|project|partner|people|reporting|finance|master|bdt)")
    ap.add_argument("--skip", help="comma-separated table names to skip")
    ap.add_argument("--funds", help="comma-separated PFAbbrv subset for per-fund tables (testing)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + report; no DB writes")
    ap.add_argument("--list", action="store_true", help="print the registry and exit")
    ap.add_argument("--views", action="store_true", help="(re)create the derived views only")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.views:
        import ocha_stratus as stratus
        cbpf_mirror.create_views(stratus.get_engine(stage="dev", write=True), cbpf_registry.VIEWS)
        return

    specs = cbpf_registry.ALL
    if args.list:
        for s in specs:
            print(f"{s.name:45s} {s.kind:3s} {s.version if s.kind == 'es' else '':4s} {s.source:40s} "
                  f"{s.fanout or '':9s} {s.group:10s} key={','.join(s.key) or '-'}")
        print(f"{len(specs)} tables")
        return
    if args.only:
        specs = cbpf_registry.by_name(args.only.split(","))
    if args.group:
        specs = [s for s in specs if s.group == args.group]
    if args.skip:
        skip = set(args.skip.split(","))
        specs = [s for s in specs if s.name not in skip]
    funds = args.funds.split(",") if args.funds else None

    engine = None
    if not args.dry_run:
        import ocha_stratus as stratus
        engine = stratus.get_engine(stage="dev", write=True)

    t0 = time.time()
    results = cbpf_mirror.refresh(engine, specs, funds=funds, dry_run=args.dry_run,
                                  verbose=not args.quiet)
    ok = [r for r in results if "error" not in r]
    bad = [r for r in results if "error" in r]
    if not args.dry_run and not args.only and not args.group:
        # derived cuts (views over the tables above) — recreated on every full run
        cbpf_mirror.create_views(engine, cbpf_registry.VIEWS)
    print(f"\n{len(ok)}/{len(results)} tables {'fetched' if args.dry_run else 'loaded'} "
          f"({sum(r.get('rows', 0) for r in ok):,} rows) in {(time.time() - t0) / 60:.1f} min")
    for r in bad:
        print(f"  FAILED {r['table']}: {r['error'][:200]}", file=sys.stderr)
    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()
