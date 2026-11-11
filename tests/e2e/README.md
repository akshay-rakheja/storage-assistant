# End-to-end tests for the inline view

These drive the real bundled view (`storage_assistant/inline`) in headless Chromium against a real `Plugin`,
through a pretend MCP Apps host (`host.py`). Nothing is mocked in the view; fixtures are throwaway folders.

    sh tests/e2e/run.sh                 # everything
    sh tests/e2e/run.sh cleanup.test.js # one file

Needs Node 22+ and Playwright with Chromium. Playwright is found from `PLAYWRIGHT_PATH`, a normal `require`,
or the npx cache. Fixtures go under `E2E_TMP` (default: the system temp folder) and are removed afterwards.

| File | Covers |
| --- | --- |
| `render.test.js` | names, sizes, totals, coverage, notices, paging, long/unicode names, themes, narrow widths |
| `navigate.test.js` | opening folders, breadcrumbs, Refresh, ticks and tray, keyboard, focus, races |
| `cleanup.test.js` | suggestions, Review, Move to Trash, Delete permanently, receipts, refusals |
| `lifecycle.test.js` | running/cancelled scans, reopened views, restarts, timeouts, host capabilities, updates |
| `contract.test.js` | what the assistant sees, tool listing, output size, refusals |

Move to Trash uses a temp folder as the Trash. `E2E_REAL_TRASH=1` also runs one test that moves one uniquely
named fixture file (`sa-e2e-…`) to the real macOS Trash and removes exactly that file from it afterwards.
Open Trash never opens Finder (stubbed in `host.py`).

Tests that fail describe behaviour the suite expects and the view does not have yet.
