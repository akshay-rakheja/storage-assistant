"""Write dist/server.json for the official MCP Registry, pointing at an already-published release.

The registry stores only metadata; clients download the .mcpb from the GitHub release and check it
against fileSha256, so this hashes the exact file attached to that release.

    python3 scripts/registry.py            # current version; then: mcp-publisher login github && mcp-publisher publish dist/server.json
"""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
REPO = "akshay-rakheja/storage-assistant"
SCHEMA = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"


def main():
    manifest = json.loads((ROOT / "packaging/mcpb/manifest.json").read_text())
    version = manifest["version"]
    asset = f"storage-assistant-{version}.mcpb"
    with tempfile.TemporaryDirectory() as tmp:
        # The released file, not a local rebuild: zip timestamps make rebuilds differ byte for byte.
        subprocess.run(["gh", "release", "download", f"v{version}", "--repo", REPO, "--pattern", asset, "--dir", tmp],
                       check=True)
        sha = hashlib.sha256((Path(tmp) / asset).read_bytes()).hexdigest()
    server = {
        "$schema": SCHEMA,
        "name": "io.github.akshay-rakheja/storage-assistant",
        "title": manifest["display_name"],
        "description": "See what fills a folder on your Mac as a treemap in the chat, and clear it with a click.",
        "repository": {"url": f"https://github.com/{REPO}", "source": "github"},
        "websiteUrl": f"https://github.com/{REPO}",
        "version": version,
        "packages": [{
            "registryType": "mcpb",
            "identifier": f"https://github.com/{REPO}/releases/download/v{version}/{asset}",
            "fileSha256": sha,
            "transport": {"type": "stdio"},
        }],
    }
    out = ROOT / "dist" / "server.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(server, indent=2) + "\n")
    print(f"Wrote {out.relative_to(ROOT)} for v{version} ({sha[:12]}…)")


if __name__ == "__main__":
    main()
