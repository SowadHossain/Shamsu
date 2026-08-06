---
name: react-vite
description: Build and repair React, TypeScript, Vite, and Vitest applications.
---
# React/Vite Skill

Use this skill for React, TypeScript, Vite, SPA, dashboard, and browser UI
projects.

- Keep `package.json` dependencies valid and minimal.
- Use `vite`, `typescript`, `react`, `react-dom`, and `vitest` for local UI apps.
- Prefer `npm test -- --run` compatibility by making the test script target the harness-owned test file.
- Put browser entry files at `index.html`, `src/index.tsx`, `src/App.tsx`, and `src/styles.css`.
- Keep domain data and pure functions in `src/data.ts` so Vitest can verify behavior without a browser.
- Use deterministic local data when external services are unavailable.
- Verify with `npm install`, `npm test -- --run`, and `npm run build` when required.
