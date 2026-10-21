"""Pack the Claude Desktop extension (.mcpb is a zip with manifest.json at its root).

Writes only dist/storage-assistant.mcpb. Installs nothing.
"""
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "dist" / "storage-assistant.mcpb"
PLUGIN = ROOT / "plugins" / "storage-assistant"
INCLUDE = ["run.py", "storage_assistant", "skills", "README.md", "LICENSE"]


def files():
    for entry in INCLUDE:
        path = PLUGIN / entry
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for candidate in candidates:
            if "__pycache__" not in candidate.parts and candidate.suffix != ".pyc":
                yield candidate


def build():
    manifest = json.loads((ROOT / "packaging/mcpb/manifest.json").read_text())
    plugin = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert manifest["version"] == plugin["version"], "Desktop and Claude Code versions differ"
    OUTPUT.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for path in files():
            archive.write(path, path.relative_to(PLUGIN))
    return OUTPUT


if __name__ == "__main__":
    print(f"Built {build().relative_to(ROOT)}")
