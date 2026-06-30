## What this PR does
<!-- One sentence summary -->

## Issue(s) closed
Closes #

## Changes
<!-- List the files changed and why -->

## How to test
<!-- Steps to verify this works -->

## Checklist
- [ ] Tests added or updated (`pytest tests/`)
- [ ] `ruff check shamsu/` passes
- [ ] No `print()` statements (use `Logger`)
- [ ] All file operations go through `Sandbox.validate()`
- [ ] Secrets are redacted before any log write
- [ ] Types imported from `shamsu/types.py` (not redefined)
- [ ] If this changes `types.py` or `interfaces.py`: the other two devs were tagged before merging
