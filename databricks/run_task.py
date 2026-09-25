"""Databricks entry wrapper for the pipeline scripts.

Runs one or more of the pure-Python ``scripts/*.py`` unchanged inside a
Databricks job task, in order, stopping at the first failure. The scripts and
``src/`` don't know about Databricks (the same files ran on GitHub Actions
until 2026-09); this wrapper is the only Databricks-specific glue:

1. Sets the run-mode env vars the code reads (``STAGE`` selects the
   ocha-stratus data plane, plus any ``--env`` extras such as
   ``GITHUB_REPOSITORY``). DB/blob credentials are NOT set here: the Job
   Compute policy injects ``DSCI_AZ_*`` from the ``dsci`` secret scope. Other
   secrets (GitHub token, Claude OAuth token) are resolved with ``--secret``.
2. Copies ``src`` + ``scripts`` + ``prompts`` from the wsfs git checkout onto
   local disk and runs from there (importing straight off the workspace FUSE
   mount is unreliable), creating the ``claude_work/`` and ``site/`` scratch
   dirs the scripts write into. All scripts in one task share that directory,
   so ``prepare -> run_claude -> apply`` hand files to each other exactly as
   the old workflow jobs did on a runner.
3. Shells out to each script with ``PYTHONPATH`` at the copied repo root so
   ``from src ...`` and ``import scripts.check_storm_sids`` resolve without
   ``pip install -e .``.

Usage (as the ``spark_python_task`` parameters); a script entry may carry its
own arguments as one quoted string:

    run_task.py scripts/refresh_mirror.py scripts/refresh_projects.py --stage dev
    run_task.py "scripts/check_storm_sids.py --write" scripts/prepare_claude_input.py \
        "scripts/run_claude.py prompts/match_storms.md" scripts/apply_claude_matches.py \
        --stage dev --secret CERF_SUPPLEMENT_GH_TOKEN=GITHUB_TOKEN --secret CLAUDE_CODE_OAUTH_TOKEN
"""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile

_COPY_DIRS = ("src", "scripts", "prompts")
_SCRATCH_DIRS = ("claude_work", "site")


def _find_script_dir() -> str:
    """spark_python_task's exec context doesn't always define __file__."""
    try:
        return os.path.dirname(os.path.abspath(__file__))  # noqa: F821
    except NameError:
        pass
    if sys.argv and sys.argv[0]:
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    return os.getcwd()


def _parse(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "scripts",
        nargs="+",
        help="pipeline scripts, relative to repo root, run in order; quote a "
        "script together with its own arguments ('scripts/x.py --write')",
    )
    ap.add_argument(
        "--stage",
        required=True,
        choices=["dev", "prod"],
        help="ocha-stratus data plane (DEV vs PROD DB + blob)",
    )
    ap.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="extra env var for the scripts (repeatable)",
    )
    ap.add_argument(
        "--secret",
        action="append",
        default=[],
        metavar="KEY[=ENV]",
        help="dsci secret KEY to expose as env var ENV (default: same name; "
        "repeatable). Resolved with dbutils at run time and tolerated if "
        "missing — unlike a spark_env_vars {{secrets/...}} reference, which "
        "stops the cluster from launching at all when the key does not exist.",
    )
    return ap.parse_args(argv)


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)
    repo_root = os.path.abspath(os.path.join(_find_script_dir(), ".."))
    local_root = os.path.join(
        "/local_disk0" if os.path.isdir("/local_disk0") else tempfile.gettempdir(),
        "cerf_supplement_run",
    )
    for sub in _COPY_DIRS:
        shutil.copytree(
            os.path.join(repo_root, sub),
            os.path.join(local_root, sub),
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for sub in _SCRATCH_DIRS:
        os.makedirs(os.path.join(local_root, sub), exist_ok=True)

    env = dict(os.environ)
    env["STAGE"] = args.stage
    for kv in args.env:
        key, _, value = kv.partition("=")
        if not key:
            raise ValueError(f"bad --env {kv!r}; expected KEY=VALUE")
        env[key] = value
    resolved = []
    for spec in args.secret:
        key, _, name = spec.partition("=")
        name = name or key
        try:
            from databricks.sdk.runtime import dbutils

            env[name] = dbutils.secrets.get("dsci", key)
            resolved.append(name)
        except Exception as exc:  # noqa: BLE001
            print(f"[run_task] WARNING: dsci/{key} unavailable ({exc})")
    env["PYTHONPATH"] = local_root + os.pathsep + env.get("PYTHONPATH", "")
    # Unbuffered so the scripts' prints interleave correctly in the run log.
    env["PYTHONUNBUFFERED"] = "1"
    env["MPLCONFIGDIR"] = "/tmp/mplconfig"

    shown = {k: env[k] for k in ["STAGE", *[kv.partition("=")[0] for kv in args.env]]}
    print(f"[run_task] env={shown} secrets={resolved} scripts={args.scripts}")
    for entry in args.scripts:
        script, *script_args = shlex.split(entry)
        cmd = [sys.executable, os.path.join(local_root, script), *script_args]
        print(f"[run_task] >>> {script} {' '.join(script_args)}".rstrip())
        rc = subprocess.run(cmd, cwd=local_root, env=env, check=False).returncode
        # Databricks treats a top-level SystemExit (even code 0) as a task
        # failure; raise only on non-zero and let success return naturally.
        if rc != 0:
            raise RuntimeError(f"{script} exited with code {rc}")
        print(f"[run_task] <<< {script} OK")
    print("[run_task] OK")


if __name__ == "__main__":
    main()
