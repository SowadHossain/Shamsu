---
name: sql-databases
description: Build against a server SQL database - PostgreSQL, MySQL, MariaDB, SQL Server - with connection config, migrations, indexes, and transactions.
---
# SQL Databases Skill

Use this skill when the target database is a **server** engine (PostgreSQL,
MySQL/MariaDB, SQL Server, CockroachDB) rather than a local file. It covers
connection configuration, migrations, indexing, and transactional writes.

## Connection

- Read the connection from the environment (`DATABASE_URL`, `PGHOST`, or the
  framework's settings), never a hardcoded literal. Provide a documented
  default for local development only.
- Never commit a password. Put real values in `.env` and commit `.env.example`.
- Name the driver explicitly in the project's dependency file:
  `psycopg[binary]` for PostgreSQL, `mysqlclient` or `PyMySQL` for MySQL.
- Django: `django.db.backends.postgresql` / `.mysql`, configured through
  `DATABASES["default"]`. SQLAlchemy: a `postgresql+psycopg://` URL.

## Migrations

- Every model change ships with a migration in the same change. Generate it,
  do not hand-write it, then read it before accepting.
- Run the migration against a real database before claiming success; a
  migration that has never been applied is not evidence.
- Adding a `NOT NULL` column to a populated table needs a default or a
  three-step migration. State which one you used.
- Never edit an applied migration - add a new one.

## Schema

- Index every foreign key and every column used in a `WHERE` or `ORDER BY` on
  a large table. Postgres does not index foreign keys automatically.
- Use the engine's real types: `TIMESTAMPTZ` over a naive timestamp, `NUMERIC`
  for money (never float), native `UUID`, `JSONB` over text on Postgres.
- Put a `UNIQUE` constraint on anything the product treats as unique - ISBN,
  email, membership number - rather than relying on application checks.
- Prefer database-level `CHECK` constraints and `ON DELETE` behaviour to
  application-only rules.

## Queries and transactions

- Wrap multi-row writes that must succeed together in one transaction.
- Never build SQL by string concatenation with user input; use bound
  parameters or the ORM.
- Fix N+1 queries with a join or prefetch when a list view reads a relation.
- Add `select_for_update` where two requests can update the same row.

## Verification

- Verify against a running database, not a mock: connect, migrate, insert a
  row, read it back, and check the constraint you claimed to add actually
  rejects a bad value.
- If no server is reachable, say the change is UNVERIFIED - do not silently
  fall back to SQLite and report success, because the engines differ in
  exactly the places that matter (types, constraints, transactional DDL).
