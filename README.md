# Storage Assistant

See what's taking up space on your Mac by asking Claude (or ChatGPT) in plain language, and explore the answer as an interactive treemap.

> "What's taking up the most space in my Downloads?"

Storage Assistant measures the folder you name, right on your Mac, and answers with the biggest files and folders. In Claude Desktop the answer comes with a treemap you can click into. It reads names, sizes and dates only: **file contents are never opened, and nothing is ever moved or deleted.**

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

- **Name the folder** in your question: "…in ~/Downloads", "…in ~/Desktop/Projects". If you don't, a folder picker opens.
- macOS may ask once whether Claude can access that folder. Allow it.
- **Everything is counted**, including `node_modules`, `.git` and iCloud-only files. Files under 1 MB are grouped per folder (for example "5,021 small code files") so even huge folders are measured completely: a 2.8-million-file Documents folder took under 3 minutes in testing. The assistant tells you while it is still scanning.
- **File size vs Space on disk:** if iCloud keeps your files in the cloud, "File size" can be many times larger than what your Mac actually stores. The treemap says so when that happens.
- In the treemap, click a folder to open it, use the path at the top to go back, and switch between **Items** and **File types**, or **File size** and **Space on disk**.
- Want the full-size map with every name? Ask it to "open the local view".

## Privacy

- **Shared with the assistant:** file and folder names, paths inside the folder you chose, sizes and dates. They become part of your conversation, like anything you paste.
- **Never read:** file contents.
- **Never scanned:** your whole home folder, system folders, `~/Library`, other disks, and credential stores (`.ssh`, `.aws`, `.gnupg`, `.env`, key files).
- **iCloud-only files** are counted at their full size but take no space on your Mac. They are never downloaded.
- **Read only:** there is no delete feature. You decide what to remove, in Finder.

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
