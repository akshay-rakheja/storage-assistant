# Storage Assistant plugin

Ask what's taking up space in a folder on your Mac, explore it as a treemap, and clear what you don't need. File contents are never opened, and nothing changes unless you click a cleanup action.

## What it does

- **Measures one folder you name** (or pick in a macOS folder picker): names, sizes and dates of everything inside it, including `.git`, `node_modules` and iCloud-only files, without opening any file.
- **Answers with real names and sizes.** Claude can list the largest files, open subfolders and group by file type (`storage_query`, `storage_render_view`).
- **Shows a treemap.** In Claude Desktop it appears in the chat; in Claude Code, `storage_open_local_view` opens it in your browser at a private `127.0.0.1` address.
- **Suggests what could be cleared**, with the reason for each item: unfinished downloads, folders that rebuild from a lockfile (`node_modules`), installers for apps already installed, files also stored in iCloud, and things untouched for years.
- **Clears only on your click.** In the treemap you tick items, review them, and click Move to Trash, Delete permanently or Remove Download. Claude cannot call these actions, and each item is checked again just before anything happens. Items changed in the last 7 days are held back.

Install in Claude Code:

```
/plugin marketplace add akshay-rakheja/storage-assistant
/plugin install storage-assistant@storage-assistant
```

Requirements: macOS and `python3` (the one that comes with Apple's developer tools). No other dependencies, accounts or API keys.

## Data it reads and sends

- **To the assistant:** file and folder names, paths inside the chosen folder, sizes and dates. They become part of your conversation.
- **Never read:** file contents, except by two optional checks that are off until you switch them on in the view (`git status` in repositories, and checksums to find exact duplicates); their results stay on your Mac.
- **Never scanned:** your whole home folder, system folders, `~/Library`, other disks, and credential stores (`.ssh`, `.aws`, `.gnupg`, `.env`, key files).
- **Network:** one request per start to GitHub's releases API for the latest version number, so the view can offer an update. It carries nothing about your files. Set `STORAGE_ASSISTANT_UPDATE_CHECK=0` to turn it off. Nothing else leaves your Mac: no telemetry, no other servers.
- **Local connections only:** the browser view and its helper talk over `127.0.0.1` using a random secret the plugin creates for that session. It is not a credential of yours and never leaves your Mac.

Full details: [privacy policy](https://github.com/akshay-rakheja/storage-assistant/blob/main/PRIVACY.md). Install instructions and screenshots: [main README](https://github.com/akshay-rakheja/storage-assistant#readme). Issues: [GitHub](https://github.com/akshay-rakheja/storage-assistant/issues).

MIT licensed.
