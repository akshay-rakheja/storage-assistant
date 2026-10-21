import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from .demo import create_demo
from .engine import Engine
from .mcp import attach
from .policy import StorageError, opaque
from .server import Companion


def main():
    parser = argparse.ArgumentParser(description="Read-only metadata companion. No background installation.")
    parser.add_argument("mode", choices=["demo", "serve", "attach", "chatgpt-attach", "plugin"])
    parser.add_argument("--root", help="Folder deliberately chosen by the local human; never a model argument")
    # Next to the launcher that started us (repo root run.py in development).
    parser.add_argument("--session-file", default=str(Path(sys.argv[0]).resolve().parent / ".runtime/session.json"))
    args = parser.parse_args()
    if args.mode == "plugin":
        if args.root:
            parser.error("Plugin mode uses the native folder picker; it cannot take a folder argument.")
        from .plugin import serve
        serve()
        return
    if args.mode in ("attach", "chatgpt-attach"):
        if args.root:
            parser.error("Adapter modes cannot choose a folder.")
        attach(args.session_file, apps=args.mode == "chatgpt-attach")
        return
    if args.mode == "demo" and args.root:
        parser.error("Demo uses only a generated fixture; do not supply a live root.")
    runtime = Path(args.session_file).absolute().parent
    if runtime.is_symlink():
        parser.error("Runtime directory may not be a link.")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    if runtime.stat().st_uid != os.getuid() or runtime.stat().st_mode & 0o077:
        parser.error("Runtime directory must be owned by this user with mode 0700.")
    descriptor = Path(args.session_file)
    # Refuse replacing another session, secret, or symbolic link.
    try:
        descriptor_fd = os.open(descriptor, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        parser.error("Session descriptor already exists. Stop the previous companion; inspect a stale descriptor locally before removing it.")
    fixture = None
    server = None
    engine = Engine()
    try:
        if args.mode == "demo":
            fixture = runtime / ("demo-" + opaque())
            create_demo(fixture)
            engine.select(str(fixture))
        elif args.root:
            engine.select(args.root)
        server = Companion(engine, synthetic_grant_id=engine.grant.grant_id if fixture else None)
        with os.fdopen(descriptor_fd, "w", encoding="utf-8") as stream:
            descriptor_fd = None
            json.dump(server.descriptor(), stream)
        if fixture:
            engine.start(engine.grant.grant_id, opaque())
        print(f"Local companion: {server.url}", flush=True)
        print(f"Browser pairing code (keep local): {server.pairing}", flush=True)
        print("Names stay in this local browser view. Connected assistants receive aliases, aggregates and coverage.", flush=True)
        print("Stop with Ctrl-C; target folders are never written. Demo fixtures are disposable.", flush=True)
        server.serve_forever(poll_interval=0.1)
    except StorageError as exc:
        print(exc.message)
    except KeyboardInterrupt:
        pass
    finally:
        engine.close()
        if server:
            server.server_close()
        if descriptor_fd is not None:
            os.close(descriptor_fd)
        descriptor.unlink(missing_ok=True)
        if fixture:
            shutil.rmtree(fixture)

if __name__ == "__main__":
    main()
