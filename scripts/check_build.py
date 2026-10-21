"""Validate distributable source without installing packages or writing caches."""
import ast
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
plugin = root / "plugins/storage-assistant"
files = [root / "run.py", plugin / "run.py", *plugin.glob("storage_assistant/*.py"), *root.glob("scripts/*.py"), *root.glob("tests/*.py")]
for path in files:
    ast.parse(path.read_text(), filename=str(path.relative_to(root)))
manifests = [plugin / "plugin.json", plugin / "mcp.json", plugin / ".claude-plugin/plugin.json", *root.glob("packaging/*.json"), *root.glob("packaging/mcpb/*.json"),
             *root.glob(".claude-plugin/*.json"), root / ".agents/plugins/marketplace.json"]
for path in manifests:
    json.loads(path.read_text())
assert (plugin / "mcp.json").read_text().count('"storage"') == 1
versions = {json.loads((root / f).read_text())["version"] for f in ("plugins/storage-assistant/plugin.json", "plugins/storage-assistant/.claude-plugin/plugin.json", "packaging/mcpb/manifest.json")}
assert len(versions) == 1, f"Manifest versions differ: {versions}"
from storage_assistant import __version__
assert versions == {__version__}, f"storage_assistant.__version__ {__version__} differs from manifests {versions}"
assert all('"plugin"' in (root / f).read_text() for f in ("plugins/storage-assistant/mcp.json", "plugins/storage-assistant/.claude-plugin/plugin.json", "packaging/mcpb/manifest.json"))
assert (root / "run.py").read_text() == (plugin / "run.py").read_text(), "plugin run.py drifted from root run.py"
from storage_assistant.apps import UI_URI, resource_read
html = resource_read({"uri": UI_URI})["contents"][0]["text"]
assert "BUNDLED_" not in html and len(html.encode()) < 100000
print(f"Build check passed: {len(files)} Python sources, {len(manifests)} manifests, bundled UI; zero third-party runtime dependencies.")
