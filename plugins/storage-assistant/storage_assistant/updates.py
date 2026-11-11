"""Once per start, ask GitHub for the latest release number, so the view can offer an update.

Only a version request goes out: no file names, paths, sizes or identifiers. It runs in the background,
gives up quietly on any error, and is off when STORAGE_ASSISTANT_UPDATE_CHECK=0.
"""
import json
import re
import threading
import urllib.request
from . import __version__

REPO = "akshay-rakheja/storage-assistant"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD = f"https://github.com/{REPO}/releases/latest/download/storage-assistant.mcpb"
VERSION = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")


def newer(tag, current=__version__):
    a, b = VERSION.match(str(tag)), VERSION.match(str(current))
    return bool(a and b) and tuple(map(int, a.groups())) > tuple(map(int, b.groups()))


class Updates:
    def __init__(self, enabled=False, fetch=None):
        self.available = None
        self.fetch = fetch or self._fetch
        if enabled:
            threading.Thread(target=self.check, daemon=True).start()

    @staticmethod
    def _fetch():
        request = urllib.request.Request(LATEST, headers={
            "Accept": "application/vnd.github+json", "User-Agent": f"storage-assistant/{__version__}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read(65536))["tag_name"]

    def check(self):
        try:
            tag = self.fetch()
        except Exception:
            return
        if newer(tag):
            self.available = {"version": str(tag).lstrip("v"), "url": DOWNLOAD}
