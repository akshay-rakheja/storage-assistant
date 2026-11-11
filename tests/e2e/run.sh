#!/bin/sh
# Run the end-to-end suite: sh tests/e2e/run.sh [file.test.js ...]
cd "$(dirname "$0")" || exit 1
[ $# -eq 0 ] && set -- *.test.js
exec node --test --test-concurrency=3 "$@"
