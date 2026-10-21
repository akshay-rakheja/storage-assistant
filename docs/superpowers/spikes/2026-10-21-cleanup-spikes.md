# Cleanup spikes (2026-10-21)

Run with `scripts/spikes/cleanup_spikes.py` on macOS 26.6 (Apple silicon), `/usr/bin/python3` 3.9.

## putback
Decision: **Put back works without Full Disk Access.**
A temp file (`storage-assistant-spike-*`) was trashed through Foundation
(`trashItemAtURL:resultingItemURL:error:`), which returned the resulting path in `~/.Trash`;
`os.rename(resulting, original)` then succeeded. Caveat: run from the developer's terminal/agent host,
whose own privacy grants apply; Task 7/8 should still handle a rename failure by falling back to Open Trash.
`macos.trash_item` calls Foundation first and returns the Trash path when it succeeds; it falls back to
`/usr/bin/trash` (returning `None`) only if Foundation fails.

## mdls
Batched `mdls -name kMDItemLastUsedDate -name kMDItemWhereFroms` over 300 paths: 0.33 s, exit 0, 600 output
lines. Batching is cheap enough to run per page of results.

## evict
Not run. Needs a downloaded iCloud file; to be run during Task 9 manual acceptance
(`python3 scripts/spikes/cleanup_spikes.py evict PATH`): confirm `st_flags & 0x40000000` (dataless) afterwards
and that no download occurred. `evict_download` on a non-iCloud file was verified to raise `OSError`.

## App-only tool visibility
Checked in Task 9 manual acceptance.
