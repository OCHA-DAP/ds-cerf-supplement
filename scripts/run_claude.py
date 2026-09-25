"""
Run one Claude Code research step headlessly — the Databricks stand-in for the
`anthropics/claude-code-base-action` step of the old GitHub workflows.

    python scripts/run_claude.py prompts/match_storms.md [--timeout-minutes 45]

Same contract as the action: Claude is told to read the prompt file and follow
it; it reads `claude_work/<input>.json` and writes `claude_work/<matches>.json`
in the current directory (the repo root — run_task.py sets cwd). Tools are
restricted to Read/Write/Edit/WebSearch/WebFetch, and every `DSCI_*` variable
is stripped from Claude's environment, so exactly as on GitHub it never holds
DB or blob credentials — the apply scripts do all validated writes.

Needs `CLAUDE_CODE_OAUTH_TOKEN` (dsci secret, resolved by run_task.py
`--secret`). Installs the Claude Code CLI with the official native installer
when `claude` is not on PATH (ephemeral job clusters start clean).
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

MODEL = "claude-sonnet-5"  # must be a *current* model id — Claude API ids drift
ALLOWED_TOOLS = "Read,Write,Edit,WebSearch,WebFetch"
INSTALL_URL = "https://claude.ai/install.sh"


def _claude_bin() -> str:
    if found := shutil.which("claude"):
        return found
    local = Path.home() / ".local" / "bin" / "claude"
    if not local.exists():
        print("[run_claude] installing Claude Code CLI")
        subprocess.run(
            f"curl -fsSL {INSTALL_URL} | bash",
            shell=True,
            check=True,
            stdout=sys.stdout,
            stderr=subprocess.STDOUT,
        )
    if not local.exists():
        raise SystemExit("[run_claude] claude CLI not found after install")
    return str(local)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt_file", help="e.g. prompts/match_storms.md")
    ap.add_argument("--timeout-minutes", type=int, default=20)
    args = ap.parse_args()

    if not os.getenv("CLAUDE_CODE_OAUTH_TOKEN"):
        raise SystemExit("[run_claude] CLAUDE_CODE_OAUTH_TOKEN is not set (dsci secret)")
    if not Path(args.prompt_file).exists():
        raise SystemExit(f"[run_claude] {args.prompt_file} not found (cwd={os.getcwd()})")

    env = {k: v for k, v in os.environ.items() if not k.startswith("DSCI_")}
    env.setdefault("HOME", str(Path.home()))
    cmd = [
        _claude_bin(),
        "-p",
        f"Read the file {args.prompt_file} and follow its instructions exactly.",
        "--allowedTools",
        ALLOWED_TOOLS,
        "--model",
        MODEL,
        "--output-format",
        "text",
    ]
    print(f"[run_claude] {args.prompt_file} model={MODEL} timeout={args.timeout_minutes}m")
    try:
        rc = subprocess.run(
            cmd, env=env, timeout=args.timeout_minutes * 60, check=False
        ).returncode
    except subprocess.TimeoutExpired:
        raise SystemExit(f"[run_claude] timed out after {args.timeout_minutes} minutes")
    if rc != 0:
        raise SystemExit(f"[run_claude] claude exited with code {rc}")
    print("[run_claude] done")


if __name__ == "__main__":
    main()
