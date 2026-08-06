# AtlasDesk Service Console PRD

## 1. Product Overview

AtlasDesk is a local service-console application for small operations teams.
It combines a browser dashboard, a lightweight Node persistence layer, and
scriptable maintenance commands. The first version must be runnable entirely on
a developer laptop with seeded demo data.

The product is not a marketing page. The first screen should be the operational
console: queues, incidents, escalations, assignees, and current service health.

## 2. Target Users

- Operations leads triaging high-priority support work.
- Support agents who need a compact view of assigned incidents.
- Engineering reviewers validating that a generated app actually works.

## 3. Core Workflows

1. See queue health at a glance.
2. Filter incidents by status, severity, owner, and overdue state.
3. Open an incident detail panel with timeline, customer, SLA, and next action.
4. Add a note to an incident.
5. Mark an incident as acknowledged or resolved.
6. Seed realistic demo data from the command line.
7. Print a deterministic status summary from the command line.

## 4. Required Stack

- TypeScript
- React
- Vite
- Node.js scripts
- SQLite or a deterministic JSON persistence fallback when SQLite is not
  available in the environment
- Vitest for unit or integration tests

## 5. Data Model

### Incident

- `id`: stable string such as `inc-001`
- `title`: short description
- `customer`: customer or account name
- `severity`: one of `low`, `medium`, `high`, `critical`
- `status`: one of `open`, `acknowledged`, `resolved`
- `owner`: teammate name
- `slaMinutes`: integer SLA target
- `ageMinutes`: integer current age
- `tags`: string list
- `notes`: note list

### Note

- `id`
- `incidentId`
- `author`
- `body`
- `createdAt`

### Health Metric

- `id`
- `label`
- `value`
- `trend`: `up`, `flat`, or `down`

## 6. Demo Data

The seed command must create exactly six incidents:

1. `inc-001` critical open, owner Priya, overdue
2. `inc-002` high open, owner Mateo
3. `inc-003` high acknowledged, owner Lina, overdue
4. `inc-004` medium resolved, owner Omar
5. `inc-005` low resolved, owner Priya
6. `inc-006` medium open, owner Lina

It must also create at least three health metrics and at least one note per
incident.

## 7. Browser UI Requirements

- Use the operational console as the first screen.
- Display counts for open, high-or-critical, overdue, and resolved incidents.
- Provide visible filter controls for status, severity, and owner.
- Show a dense incident table or list with title, customer, severity, status,
  owner, and SLA.
- Selecting an incident shows a detail region with its notes and next action.
- Use a calm operational palette with strong contrast and no marketing hero.
- The UI must remain usable at a mobile width.

## 8. Script Requirements

- `scripts/seed.mjs` seeds demo data and prints `seeded 6 records`.
- `scripts/status.mjs` reads the same data and prints
  `open 3 high 2 overdue 1`.
- The scripts must work after a fresh install/build from the project root.
- The status script must not rely on hardcoded output only; it should compute
  counts from the seeded persistence file or database.

## 9. Test Requirements

- Include tests for status-count computation.
- Include tests proving seed data contains six incidents.
- Include at least one test for filtering by owner or status.

## 10. Acceptance

- `npm test -- --run` exits 0.
- `npm run build` exits 0.
- `node scripts/seed.mjs` prints `seeded 6 records`.
- `node scripts/status.mjs` prints `open 3 high 2 overdue 1`.
- `src` contains a React application with incident filtering.
- `scripts/seed.mjs` and `scripts/status.mjs` exist.
- Seeded data survives a separate status command after the seed command exits.
