# Working on Storage Assistant

Notes for coding agents (Claude Code reads this through `CLAUDE.md`). The README covers what the plugin does and how people install it.

## Ground rules

- Development happens in this public repository. Never commit private data: real folder listings, scan output, local paths, logs with file names, credentials.
- Commit as `60154613+akshay-rakheja@users.noreply.github.com` (the GitHub noreply address), never a personal email.
- Tests use throwaway synthetic folders only (temp or scratch directories), never real folders such as Downloads, Documents or Desktop.
- Push, release and publish only when the owner asks.
- The model must never be able to change files. Tools that act on files stay view-only (`_meta.ui.visibility: ["app"]`), and only `storage_cleanup_execute` is labelled `destructiveHint: true`.

## Checks

```sh
make test                      # unit tests (python3 -m unittest)
python3 -m pytest -q           # same tests through pytest
sh tests/e2e/run.sh            # end-to-end tests of the inline view in headless Chromium (see tests/e2e/README.md)
```

Run both suites before a release; everything should pass, apart from the opt-in real-Trash test, which skips.

## Releasing a new version

1. Work on a branch, get the tests green, then merge to `main` (fast-forward) and push.
2. Bump the version in all four files, never touching the tests:
   ```sh
   git grep -l 'OLD' -- '*.json' '*.py' ':!tests' | xargs sed -i '' 's/OLD/NEW/g'
   ```
   The files are `packaging/mcpb/manifest.json`, `plugins/storage-assistant/plugin.json`, `plugins/storage-assistant/.claude-plugin/plugin.json` and `plugins/storage-assistant/storage_assistant/__init__.py`. Escape the dots in OLD (`0\.6\.11`). Commit and push `main`.
3. `python3 scripts/release.py`. It refuses unless `main` matches `origin/main`, runs the checks, builds the `.mcpb`, tags `vX.Y.Z` and creates the GitHub release marked latest. Installed copies then show the update notice.
4. Publish the new version to the official MCP Registry (it does not pick up releases by itself):
   ```sh
   python3 scripts/registry.py                  # downloads the released .mcpb, hashes it, writes dist/server.json
   mcp-publisher validate dist/server.json
   mcp-publisher publish dist/server.json
   ```
   `mcp-publisher` comes from Homebrew (`brew install mcp-publisher`). If it says you are not signed in, run `mcp-publisher login github` and enter the code it prints at https://github.com/login/device. The server name is `io.github.akshay-rakheja/storage-assistant`, so only that GitHub account can publish it. Check the listing:
   ```sh
   curl -s "https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.akshay-rakheja/storage-assistant"
   ```
5. Claude Code users get the new version from `main` through the marketplace (`claude plugin update storage-assistant@storage-assistant`). Claude Desktop directory listings are updated through Anthropic's own process, not from here.
