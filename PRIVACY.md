# Storage Assistant privacy policy

Effective 11 November 2026. Applies to Storage Assistant 0.6.10 and later.

Storage Assistant is a plugin that runs on your Mac inside Claude Desktop, Claude Code or the ChatGPT desktop app (Codex). It has no servers, no accounts, no analytics and no telemetry.

## What it reads

- **One folder at a time,** the one you name or pick. It never scans your whole home folder, system folders, `~/Library`, other disks, or credential stores (`.ssh`, `.aws`, `.gnupg`, `.env` files and small key files).
- **Metadata only:** file and folder names, paths inside the chosen folder, sizes, dates and whether a file is stored only in iCloud.
- **File contents are never read,** except by the two optional checks, and only while you have them switched on: the exact-duplicate check compares file contents, and the git check runs `git status` on repositories inside the folder (locally; no remote is contacted). Both run on your Mac; their results are labels, never the contents themselves.

## What it shares, and with whom

- **The assistant you are chatting with** (Claude or ChatGPT) receives the names, relative paths, sizes and dates it needs to answer you. They become part of your conversation, and the privacy policy of that assistant's provider applies to them. The full location of the folder on your Mac is given only to the treemap view, not to the assistant.
- **GitHub,** once each time the plugin starts: it asks GitHub's public releases page for the latest version number, so the treemap can offer an update. The request contains nothing about your files or folders. Like any web request, it reveals your IP address and the plugin's version to GitHub, whose [privacy statement](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement) applies. Set `STORAGE_ASSISTANT_UPDATE_CHECK=0` in the plugin's environment to turn it off.
- **Nobody else.** No other network requests are made. The optional local browser view runs only on your Mac (`127.0.0.1`).

## What it stores

Nothing. Scan results live in memory and are gone when the plugin stops. No files, caches or logs are written by Storage Assistant, apart from the one-line diagnostics (tool name, outcome and timing, never names or paths) that your assistant app may keep in its own log.

## What it changes

Nothing, unless you click Move to Trash, Delete permanently or Remove Download in the treemap or local view. The assistant cannot perform these actions. Every item is checked again just before anything happens, and Trash is always the default.

## Contact

Questions or concerns: open an issue at https://github.com/akshay-rakheja/storage-assistant/issues.
