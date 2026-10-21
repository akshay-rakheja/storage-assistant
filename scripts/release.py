"""Cut a release: verify, build the .mcpb, tag, and publish a GitHub release.

    python3 scripts/release.py --dry-run   # checks and build only
    python3 scripts/release.py             # also tag, push and publish

Bump the version first in plugins/storage-assistant/plugin.json,
plugins/storage-assistant/.claude-plugin/plugin.json, packaging/mcpb/manifest.json
and plugins/storage-assistant/storage_assistant/__init__.py, then commit and push main.
The release attaches storage-assistant.mcpb (for the stable "latest" download link)
and storage-assistant-<version>.mcpb. Never force-pushes.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugins" / "storage-assistant"
REPO = "akshay-rakheja/storage-assistant"


def run(*cmd, capture=False):
    print("$ " + " ".join(cmd), flush=True)
    result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=capture)
    if result.returncode:
        if capture:
            print(result.stdout + result.stderr)
        sys.exit(f"release stopped: {cmd[0]} failed")
    return result.stdout.strip() if capture else ""


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="verify and build only; no tag, push or release")
    args = parser.parse_args()

    version = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())["version"]
    tag = f"v{version}"
    if run("git", "status", "--porcelain", capture=True):
        sys.exit("release stopped: commit or stash local changes first")
    if run("git", "tag", "--list", tag, capture=True):
        sys.exit(f"release stopped: tag {tag} already exists; bump the version first (see the docstring)")
    if not args.dry_run:
        if run("git", "rev-parse", "--abbrev-ref", "HEAD", capture=True) != "main":
            sys.exit("release stopped: release from main")
        run("git", "fetch", "origin", "main")
        if run("git", "rev-parse", "HEAD", capture=True) != run("git", "rev-parse", "origin/main", capture=True):
            sys.exit("release stopped: push main first so the release matches what users install")

    run(sys.executable, "scripts/check_build.py")
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests")
    if shutil.which("claude"):
        run("claude", "plugin", "validate", ".")
        run("claude", "plugin", "validate", str(PLUGIN.relative_to(ROOT)))
    sys.path.insert(0, str(ROOT))
    from scripts.build_mcpb import build
    stable = build()
    versioned = stable.with_name(f"storage-assistant-{version}.mcpb")
    shutil.copyfile(stable, versioned)
    print(f"Built {stable.relative_to(ROOT)} and {versioned.relative_to(ROOT)}")
    if args.dry_run:
        print(f"Dry run complete for {tag}; nothing was tagged, pushed or released.")
        return

    run("git", "tag", "-a", tag, "-m", f"Storage Assistant {version}")
    run("git", "push", "origin", tag)
    notes = ("**Claude Desktop:** download `storage-assistant.mcpb` below and double-click it.\n\n"
             f"**Claude Code:** `claude plugin marketplace add https://github.com/{REPO}.git` then "
             "`claude plugin install storage-assistant@storage-assistant` (existing users: "
             "`claude plugin update storage-assistant@storage-assistant`).\n\n"
             f"**ChatGPT desktop (Codex):** `codex plugin marketplace add {REPO}`, restart, install from the Plugins Directory.\n\n"
             f"Full instructions and privacy details: https://github.com/{REPO}#readme")
    run("gh", "release", "create", tag, str(stable), str(versioned), "--repo", REPO,
        "--title", f"Storage Assistant {version}", "--notes", notes, "--latest")
    print(f"Released {tag}: https://github.com/{REPO}/releases/tag/{tag}")


if __name__ == "__main__":
    main()
