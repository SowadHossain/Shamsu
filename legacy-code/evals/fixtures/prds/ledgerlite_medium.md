# LedgerLite Expense CLI PRD

## Product Summary

LedgerLite is a local-first command line expense tracker for freelancers who
need a tiny project ledger without signing into a web service. The application
must run with the Python standard library only.

## Users

- Freelancers tracking reimbursable expenses.
- Solo operators exporting a monthly CSV summary.
- Test reviewers who need deterministic seeded data.

## Functional Requirements

1. Provide a script named `ledgerlite.py` at the project root.
2. Store data in a JSON file selected by `--db`; default to `ledgerlite.json`.
3. Support `seed`, `add`, `list`, `summary`, and `export` commands.
4. The `seed` command overwrites the selected database with four realistic
   expenses:
   - supplies 18.25 notebooks
   - meals 27.75 client lunch
   - software 99.00 design tool
   - travel 5.00 metro
5. The `add` command accepts `--category`, `--amount`, and `--note`.
6. The `list` command prints one line per expense in insertion order.
7. The `summary` command prints the grand total with two decimal places.
8. The `export` command writes a CSV file with headers:
   `id,category,amount,note`.
9. Invalid commands or missing required arguments print usage text and exit
   non-zero without a Python traceback.

## Non-Functional Requirements

- No third-party dependencies.
- Use deterministic IDs starting at `exp-001`.
- Keep the code readable, with pure helper functions for load/save/formatting.
- Never write outside the current project folder.

## Acceptance

- `python ledgerlite.py seed --db data.json` prints `seeded 4 expenses`.
- `python ledgerlite.py add --db data.json --category travel --amount 42.50 --note taxi` prints `added expense travel 42.50`.
- `python ledgerlite.py summary --db data.json` prints `total 192.50`.
- `python ledgerlite.py export --db data.json --out report.csv` prints `exported report.csv`.
- `python ledgerlite.py list --db data.json` prints lines containing `exp-001 supplies 18.25 notebooks` and `exp-005 travel 42.50 taxi`.
- `report.csv` exists after export and contains the CSV header.
