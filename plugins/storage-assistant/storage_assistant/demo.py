"""Only explicitly invoked synthetic demo writes files. Scanner never does."""
import os
from pathlib import Path


def create_demo(path):
    path = Path(path)
    path.mkdir(mode=0o700)
    for folder in ("Films", "Sound", "Documents", "Pictures", "Empty"):
        (path / folder).mkdir()
    for name, size in {"Films/launch.mov": 6_000_000, "Films/interview.mp4": 3_000_000,
                       "Sound/recording.wav": 2_000_000, "Documents/notes.pdf": 240_000,
                       "Pictures/reference.png": 800_000, ".hidden.txt": 1234}.items():
        # Fixture content is disposable zero bytes, not copied private data.
        with (path / name).open("wb") as stream:
            stream.write(b"\0" * size)
    with (path / "Films/sparse.mov").open("wb") as stream:
        stream.truncate(25_000_000)
    os.link(path / "Sound/recording.wav", path / "Sound/alias.wav")
    os.symlink("../Documents", path / "Films/link")
    (path / "cloud.icloud").touch()
    (path / ".git").mkdir()
    return path
