# Cleanup: pick, review and free space without typing paths

Date: 2026-10-21. Status: design for approval. Target release: 0.6.0.

## Intent

Storage Assistant today shows what takes space. People then have to switch to Finder, find each item again and decide alone. The hard part of cleanup is not deleting but deciding: "is this still in use?" and "do I still care about it?" This feature makes deciding fast and confident, then lets the person act in one click from the treemap, without typing a path or a name.

Claude does the legwork (finds candidates and explains why each is likely safe). The person makes every decision. Nothing is deleted unless the person clicks a button in the view.

## Goals

1. Candidates with reasons. After a scan, the person can ask "help me free up space" and see likely-safe items labelled with plain reasons and a confidence tier.
2. Selection by pointing. Items are selected by ticking them in the treemap view (list and tiles), never by typing.
3. Three actions, all from the view:
   - **Move to Trash** (default, recoverable).
   - **Delete permanently** (frees space now; separate, clearly destructive, own confirmation; only for items labelled safe or re-downloadable).
   - **Remove Download** for files that are also stored in iCloud (frees local space, keeps the file in iCloud).
4. Honest numbers. "Frees about X on this Mac" uses space on disk, not file size; after acting, show the observed change in free space.
5. Works where the view works: Claude Desktop inline view (MCP Apps) and the local browser view (`storage_open_local_view`), so Claude Code and Codex users can clean up too.

## Non-goals

- No automatic or scheduled cleanup. No cleanup Claude performs on its own.
- No emptying the whole Trash and no deleting items from the Trash (macOS privacy protection blocks `~/.Trash` without Full Disk Access, which we will not ask for). An **Open Trash** button opens it in Finder instead.
- No cleanup outside the folder the person selected (no `~/Library/Caches`, Xcode or package-manager caches elsewhere). A future "cleanup spots" feature may cover known cache locations.
- No content reading by default. Two content-touching checks exist only as opt-ins (below).
- macOS only.

## Anti-goals (failure even if it "works")

- Claude, or anything other than the person's click in the view, can trigger a deletion.
- Deleting iCloud-only files (they free nothing here and delete the file on every device).
- Suggesting files changed in the last 7 days or inside an active project.
- A Trash failure silently turning into permanent deletion.
- "Frees X GB" numbers that turn out to be file size, or iCloud space, rather than space on this Mac.

## Decisions made with the user

- **Approval: one click in the chat view (option A).** No extra macOS dialog for Move to Trash. The safeguards that make one click acceptable are mandatory (see "Execution boundary").
- **Delete permanently** replaces "Empty these from Trash" (Trash is privacy-protected). It is a separate secondary action with its own confirmation and is offered only for items tiered "Safe to clear".
- **Remove Download** for iCloud items is in scope for 0.6.0, built efficiently.
- **Opt-in checks** (off until the person turns them on in the view): "is this git repo pushed with nothing unsaved?" and "are these files exact duplicates?" (hashing).
- Labels like "Untouched since 2022" are wanted; smarter signals such as "can be rebuilt" and "can be downloaded again" are wanted.

## User experience

### Flow

1. The person asks, e.g. "help me free up space in Downloads". If no scan exists, Claude scans first (existing flow).
2. Claude calls `storage_suggest_cleanup`. The view enters **cleanup mode**: candidates are marked on tiles and listed with a reason chip and tier. "Safe to clear" items are pre-ticked; "Worth a glance" and "Your call" items are not.
3. The person ticks or unticks items in the list or on tiles. A sticky tray shows "N items, frees about X on this Mac" and **Review…**. Each candidate row offers **Quick Look** and **Show in Finder** (local actions, nothing returned to Claude).
4. **Review** lists exactly what will happen: name, path inside the folder, space on disk, the evidence, and warnings. The person can untick here. Buttons: **Move N items to Trash** (primary), **Delete permanently, free X now** (secondary, destructive), and, when relevant, **Remove N downloads (keep in iCloud)**.
5. **Delete permanently** shows its own confirmation in the view: "Delete N items permanently? This can't be undone." with the list and total. (Remove Download needs no extra confirmation: nothing is lost.)
6. A receipt shows per-item results (done, or skipped with a reason), the observed change in free space, **Open Trash**, and **Put back** where feasible (see Spikes). The treemap updates to reflect the change.

The mockup reviewed with the user is in `.superpowers/brainstorm/` (not committed): treemap with badges, checklist with chips, tray, review panel, receipt.

### Tiers and reasons

| Tier | Meaning | Pre-ticked | Delete permanently offered |
| --- | --- | --- | --- |
| Safe to clear | Nothing is lost: rebuildable, re-downloadable, redundant, partial download | yes | yes |
| Worth a glance | Probably unused: old and untouched, possible duplicate, archive next to its extracted folder | no | no |
| Your call | Personal media/documents with no strong signal | no (only shown on request) | no |
| Never suggested | Changed in last 7 days, active project files, credentials (already skipped), iCloud-only items, system/app data | not selectable for deletion | no |

### Signals (metadata only unless marked opt-in)

| Signal | How (read-only) | Tier / action | Example label |
| --- | --- | --- | --- |
| Rebuildable folder | Name in a known list (`node_modules`, `.venv`, `venv`, `target`, `.next`, `.nuxt`, `dist`/`build` only with a project marker, `Pods`, `DerivedData`, `__pycache__`, `.gradle`, `.turbo`, `.parcel-cache`) **and** a sibling project marker exists (`package.json`, `pyproject.toml`/`requirements.txt`, `Cargo.toml`, `Podfile`, …) checked by stat | Safe | "Rebuilds with npm install" |
| Stale project's dependencies | Rebuildable folder whose project has no changes for 90+ days | Safe, offered as "delete only `node_modules`, keep your code" | |
| Installer for an installed app | `.dmg`/`.pkg`/app `.zip` whose app name exists in `/Applications` or `~/Applications` (stat) | Safe | "Ollama is already installed" |
| App bundle outside Applications | `.app` in the folder with the same app in `/Applications` | Safe | "Copy in Applications" |
| Re-downloadable | Spotlight `kMDItemWhereFroms` (download source host) on installers, archives and model files (`.gguf`, `.safetensors`, …) | Safe | "Downloaded from github.com, can be fetched again" |
| Partial download | `.crdownload`, `.part`, `.download` | Safe | "Unfinished download" |
| Old and untouched | Spotlight `kMDItemLastUsedDate`, falling back to modification time; 1+ year | Worth a glance | "Untouched since 2022" |
| Archive beside its contents | `foo.zip` next to folder `foo` | Worth a glance | "Already unzipped" |
| Possible duplicate | Same name and size elsewhere in the scan | Worth a glance | "Possible duplicate of …" |
| Exact duplicate (opt-in) | Content hash of same-size files | Safe for the extra copies | "Exact duplicate of …" |
| Repo pushed and clean (opt-in) | `git -C <repo> status --porcelain=v2 --branch` with `GIT_OPTIONAL_LOCKS=0`, fixed argv, no shell; remote host only, never URL credentials | Safe ("re-clonable") / Never (unsaved work) | "Pushed to github.com, nothing unsaved" |
| iCloud copy downloaded locally | File in an iCloud-synced location and not dataless | Remove Download | "Also in iCloud" |
| iCloud-only | Dataless | Never (frees nothing here) | |
| Recently changed | Modified in last 7 days | Never | |

Spotlight lookups run for candidates only (bounded, for example the 300 largest), batched (`mdls` accepts many paths), on the local executor; the model never runs commands.

## Execution boundary (mandatory)

The trusted local executor (this plugin's Python process) enforces everything below; the model, the view and host approvals are not trusted to.

- **Who can act.** New tools `storage_cleanup_preview` and `storage_cleanup_execute` (and the opt-in toggle) are view-only: declared with MCP Apps tool visibility for the app, not the model, and described as such. The model-visible tool is only `storage_suggest_cleanup`, which is read-only. The local browser view calls the executor through its existing paired local API (human-only by construction).
- **Bound approval.** Preview returns the exact plan and a one-time token bound to grant, scan version, action, the exact node identities (device, inode, type, birth time), item count, total bytes and a short expiry (5 minutes). Execute accepts only that token, once. Any mismatch refuses the whole request with a clear message; the person reviews again.
- **Revalidate at execution.** Each item is reopened relative to the granted root without following links, must still be inside the grant, and must match its scanned identity, size and modification time. Directories for permanent deletion are walked (bounded) and must contain no new or changed entries since the scan; otherwise that item is skipped and reported. Never delete through a path string built from model text.
- **No silent escalation.** Trash failure never falls back to permanent deletion. Partial results are reported per item.
- **Protected items stay protected.** Items labelled protected by the scanner, items changed in the last 7 days, iCloud-only items and anything outside the grant cannot be previewed for deletion.
- **Bounds.** At most 500 items and a total byte cap per action (builder picks a sensible cap); permanent deletion limited to Safe-tier items.
- **Receipts.** Every action returns per-item outcomes, the on-disk estimate and the observed `statvfs` free-space change. The space freed by Move to Trash appears only when the Trash is emptied; the receipt says so.

## Mechanisms

- **Move to Trash:** `/usr/bin/trash <paths…>` (present on macOS 15+, verified on 26.6) with explicit argv; on older macOS, `NSFileManager trashItemAtURL:resultingItemURL:error:` via `ctypes` (no third-party dependencies). Record the resulting Trash location when available.
- **Delete permanently:** descriptor-based, no-follow recursive removal of the revalidated item only.
- **Remove Download:** `NSFileManager evictUbiquitousItemAtURL:error:` via `ctypes` (no CLI exists on this macOS: `brctl` has no evict, `fileproviderctl` has none). Confirm afterwards that the file is dataless.
- **Open Trash:** `open` with the Trash location (Finder has access; we do not).
- **After acting:** the snapshot is marked changed and the affected folder is rescanned (fast with small-file grouping) so the treemap and totals update.

## Spikes before building

1. Remove Download via `ctypes`/Objective-C runtime on Apple silicon and Intel; confirm it never downloads and works on an iCloud Desktop & Documents file.
2. Put back: can the plugin move a trashed item back out of `~/.Trash` without Full Disk Access? If not, "Put back" becomes "Open Trash" with instructions to use Finder's Put Back.
3. MCP Apps tool visibility in Claude Desktop: confirm app-only tools are hidden from Claude and callable from the view. If not hidden, do not ship one-click execution; report back.
4. Spotlight metadata speed for ~300 candidates via batched `mdls`.

## Clarifications after builder review (designer's calls, 2026-10-21)

1. **Protection vs cleanup signals.** The scanner's `protected` label means "never suggest by default"; a cleanup signal overrides it **for that item only** (a `node_modules` with a project marker becomes Safe). A folder is never deletable as a whole if anything below it was skipped by the scan (credential files such as `.env` or keys, credential folders, links, other volumes) or if it contains a `.git` whose opt-in check is not "pushed, nothing unsaved". So whole-repo deletion requires the git opt-in to pass **and** no skipped descendants; rebuildable folders inside a repo remain deletable on their own.
2. **Active project.** A project is a folder with a marker file (`package.json`, `pyproject.toml`, `Cargo.toml`, `.git`, …). It is *active* if any file outside its rebuildable folders changed in the last 30 days. Rebuildable folders of an active project are **Worth a glance** (not pre-ticked: rebuilding costs time); of an inactive project, **Safe**. Nothing inside a project other than its rebuildable folders is suggested unless the whole-repo rule in (1) is met. The 7-day rule still applies to individual items.
3. **If spike 3 fails** (app-only tools are visible to Claude): execution ships only in the local browser view (human-only via pairing). The Claude Desktop inline view still shows suggestions, ticks and the tray, and its Review button opens the local view with the same selection. Everything else in 0.6.0 is unchanged.
4. **Trash and paths.** Permanent deletion is descriptor-based end to end. For Move to Trash and Remove Download, which only accept paths, the executor keeps the parent directory open by descriptor, revalidates the item with a no-follow `fstatat` immediately before the call, passes the absolute path of that revalidated item, and afterwards confirms the original name is gone (Trash) or the file is dataless (Remove Download). The remaining window is a documented residual risk, acceptable because Trash is recoverable and Remove Download loses nothing.
5. **Grouped small files.** Grouped entries ("1,234 small image files") are not selectable. The scanner does not group files that match a cleanup signal (partial downloads, installers, archives, app bundles, model files), so those stay individually selectable. Identity binding does not need the scan to keep identities: preview re-reads identity, size and modification time of each selected item (no-follow, relative to the grant), checks size and modification time against the scan, and binds the token to the identities it just read; execute compares against those.
6. **Where.** Built in the public repository `akshay-rakheja/storage-assistant` on `main` (the plugin's home since 2026-10-20), released as 0.6.0. Destructive actions are in scope by the user's request in this design conversation; building starts only after the user approves this document.

## Scope boundaries for the builder

- Builder decides: exact visual design within the existing treemap style; how tiles show a tick affordance (selection must be possible from both list and tiles, and clicking a folder tile still opens it); caps and thresholds not stated above; whether suggestions are computed eagerly after a scan or on request; how selection is shared with Claude as context.
- Keep: zero third-party dependencies; Python 3.9+; existing read-only tools unchanged in behaviour; all tests on disposable synthetic fixtures (never real user files); trash/evict/delete behind an injectable executor so tests never touch the real Trash or iCloud.
- Document: README privacy section (what deletes, what can be undone, opt-in checks), and a short safety note for testers.
