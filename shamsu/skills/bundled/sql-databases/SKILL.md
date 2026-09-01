---
name: sql-databases
description: Build against a server SQL database - PostgreSQL, MySQL, MariaDB, SQL Server - with connection config, migrations, indexes, and transactions.
---
# SQL Databases Skill

For a **server** engine, not a local file.

## Connection

Read it from the environment (`DATABASE_URL`, framework settings), never a
literal. Never commit a password. Name the driver in the dependency file.

## Migrations

- Every model change ships its migration. Generate it, read it, then accept.
  Never edit an applied migration - add a new one.
- Run it against a real database. An unapplied migration is not evidence.
- A `NOT NULL` column on a populated table needs a default or a three-step
  migration. Say which you used.

## Schema

- Index every foreign key and every column used in `WHERE` or `ORDER BY`.
  Postgres does not index foreign keys for you.
- Real types: `TIMESTAMPTZ` not naive, `NUMERIC` for money (never float),
  native `UUID`, `JSONB` over text.
- `UNIQUE`, `CHECK` and `ON DELETE` in the database, not only in code.

## Queries

- One transaction around writes that must succeed together.
- Bound parameters or the ORM. Never concatenate user input into SQL.
- Fix N+1 with a join or prefetch. `select_for_update` where two requests can
  update one row.

## Verification

Connect, migrate, insert, read back, and check your constraint rejects a bad
value. If no server is reachable, say **UNVERIFIED** - never fall back to
SQLite and report success. The engines differ exactly where it matters.
