# Storage Assistant

See what's taking up space on your Mac by asking Claude (or ChatGPT) in plain language, and explore the answer as an interactive treemap.

> "What's taking up the most space in my Downloads?"

Storage Assistant measures the folder you name, right on your Mac, and answers with the biggest files and folders. In Claude Desktop the answer comes with a treemap you can click into. It reads names, sizes and dates only: **file contents are never opened, and nothing is moved or deleted unless you click to do it in the view.**

## Install

You need a Mac with Python 3 (type `python3 --version` in Terminal; if macOS offers to install developer tools, accept).

### Claude Desktop (recommended: includes the treemap)

1. Download **[storage-assistant.mcpb](https://github.com/akshay-rakheja/storage-assistant/releases/latest/download/storage-assistant.mcpb)**.
2. Double-click it and choose **Install** (or Settings → Extensions → Advanced settings → Install Extension).
3. Start a new chat and ask about a folder. Click **Allow** when Claude asks to use Storage Assistant.

### Claude Code

```sh
claude plugin marketplace add https://github.com/akshay-rakheja/storage-assistant.git
claude plugin install storage-assistant@storage-assistant
```

Start a new session and ask. Update later with `claude plugin update storage-assistant@storage-assistant`.

### ChatGPT desktop app (Codex)

```sh
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex plugin marketplace add akshay-rakheja/storage-assistant
```

Restart the ChatGPT app, open the Plugins Directory, install **Storage Assistant**, and ask in a Codex conversation. (Not yet tested as thoroughly as Claude; if no treemap appears, ask it to "open the local view".)

## Using it

- **Name the folder** in your question: "…in ~/Downloads", "…in ~/Desktop/Projects". It measures one folder at a time (not your whole Mac or home folder); if you don't name one, a folder picker opens.
- Big folders show a live "Scanning…" view that turns into the treemap when the scan finishes.
- macOS may ask once whether Claude can access that folder. Allow it.
- **Everything is counted**, including `node_modules`, `.git` and iCloud-only files. Files under 1 MB are grouped per folder (for example "5,021 small code files") so even huge folders are measured completely: a 2.8-million-file Documents folder took under 3 minutes in testing. The assistant tells you while it is still scanning.
- **File size vs Space on disk:** if iCloud keeps your files in the cloud, "File size" can be many times larger than what your Mac actually stores. The treemap says so when that happens.
- In the treemap, click a folder to open it, use the path at the top to go back, and switch between **Items** and **File types**, or **File size** and **Space on disk**.
- Want the full-size map with every name? Ask it to "open the local view".

## Freeing space

Ask "help me free up space in ~/Downloads". Claude suggests items worth a look, each with the reason (an unfinished download, an installer you already installed, a `node_modules` folder that rebuilds with `npm install`…), and the treemap switches to clean-up mode. In the local browser view, click **Free up space** for the same list.

- Suggestions are sorted into **Safe to clear** (ticked for you) and **Worth a glance** (not ticked). Labels describe what was found; they are not a promise that something is unneeded, so check anything you are unsure about. In the local view, **Quick Look** and **Show in Finder** open an item before you decide.
- Tick what you want, then **Review…**. Each item is checked again on disk, and anything that changed since the scan, was modified in the last 7 days, or lives only in iCloud is left out with the reason.
- Then pick one action:
  - **Move N items to Trash** — the same as dragging them to the Trash. **Undo:** Finder's **Put Back** (the receipt has an **Open Trash** button). Space is freed when you empty the Trash.
  - **Delete permanently, free X now** — only offered for items marked Safe to clear (like `node_modules`, unfinished downloads or an installer copy of an app you have), and only after a second "This can't be undone" confirmation. **No undo.**
  - **Remove N downloads (keep in iCloud)** — for iCloud Drive files: removes the copy on this Mac and keeps the file in iCloud, where it downloads again when you open it. Nothing is lost.
- **iCloud Drive, Desktop and Documents:** for items synced with iCloud, **Move to Trash** and **Delete permanently** remove them from iCloud and from every device signed in to it. Only **Remove Download** keeps the file in iCloud.
- A receipt lists what was done, what was left alone and anything that didn't finish, with your free space before and after. The map then rescans.

Two **optional checks** are off until you switch them on in the view, and read a little file data on your Mac when on:

- **Check git repositories** runs `git status` in repositories inside the folder (and reads the remote's host name) so a project can be marked "Pushed, nothing unsaved". It does not fetch or change anything, and it skips (never marks as pushed) any repository whose own settings could make git run a command, such as fsmonitor or filter drivers.
- **Find exact duplicates** compares the contents of same-size files (1 MB or more) by computing a checksum on your Mac. Contents and checksums are never sent anywhere.

## Privacy

- **Shared with the assistant:** file and folder names, paths inside the folder you chose, sizes and dates. They become part of your conversation, like anything you paste.
- **Never read:** file contents are never read by Storage Assistant, except by the two optional clean-up checks above, and only while you have them switched on. **Quick Look** in the local view is macOS showing the file to you when you click it; nothing from it goes to the assistant.
- **Never scanned:** your whole home folder, system folders, `~/Library`, other disks, and credential stores (`.ssh`, `.aws`, `.gnupg`, `.env`, key files).
- **iCloud-only files** are counted at their full size but take no space on your Mac. They are never downloaded.
- **Only your click changes anything:** moving to the Trash, deleting or removing downloads happens only when you click the button in the treemap or local view. Claude can suggest items but cannot call the actions that change files, and every item is checked again just before anything happens.

Nothing is sent anywhere by Storage Assistant itself: it runs on your Mac and only talks to the Claude or ChatGPT app that started it.

"File size" and "Space on disk" can differ a lot (for sparse and cloud files), and neither tells you exactly how much deleting something will free, because macOS can share space between files.

## Feedback

Found a bug or have an idea? [Open an issue](https://github.com/akshay-rakheja/storage-assistant/issues). A screenshot helps. If the treemap doesn't appear in Claude Desktop, `~/Library/Logs/Claude/mcp-server-Storage Assistant.log` shows which tools ran (tool names and timings only).

## Uninstall

- Claude Desktop: Settings → Extensions → Storage Assistant → Uninstall.
- Claude Code: `claude plugin uninstall storage-assistant@storage-assistant`
- ChatGPT: remove it in the Plugins Directory.

## Development

Everything that gets installed lives in [`plugins/storage-assistant/`](plugins/storage-assistant): a small Python package with no third-party dependencies.

```sh
make test           # unit tests on throwaway synthetic folders
make build          # syntax and manifest checks
make mcpb           # build dist/storage-assistant.mcpb for Claude Desktop
make release        # tag, build and publish a GitHub release (bump versions first)
```

Inspired by [DiskTree](https://github.com/tobi/disktree) by Tobi Lütke. No DiskTree code is included.

MIT licensed. See [LICENSE](LICENSE).
