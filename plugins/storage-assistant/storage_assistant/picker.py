"""Native human folder choice. Fixed argv only; no model text reaches a command."""
import subprocess
import sys
from .policy import StorageError

PROMPT = ("Storage Assistant: choose ONE folder to measure. It reads file and folder names, sizes and dates "
          "and shares them with your assistant. File contents are never opened. Nothing changes unless you click "
          "a cleanup action in the view.")
# Activate first so the dialog opens in front of the host app instead of behind it.
SCRIPT = f'tell current application to activate\nPOSIX path of (choose folder with prompt "{PROMPT}")'
TIMEOUT = 300


def choose_folder():
    """Return the folder the human picked, or None if they cancelled."""
    if sys.platform != "darwin":
        raise StorageError("UNSUPPORTED", "The native folder picker is macOS-only. Launch with run.py serve --root instead.")
    try:
        completed = subprocess.run(["/usr/bin/osascript", "-e", SCRIPT], capture_output=True, text=True,
                                   timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise StorageError("NOT_SELECTED", "No folder was chosen before the picker timed out.") from None
    except OSError:
        raise StorageError("UNAVAILABLE", "The native folder picker could not be opened.") from None
    if completed.returncode:
        # -128 is the user pressing Cancel; anything else is still "no choice".
        return None
    path = completed.stdout.strip()
    return path or None
