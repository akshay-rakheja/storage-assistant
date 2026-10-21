.PHONY: test build mcpb release release-check
test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
build:
	python3 scripts/check_build.py
mcpb:
	python3 scripts/build_mcpb.py
release-check:
	python3 scripts/release.py --dry-run
release:
	python3 scripts/release.py
