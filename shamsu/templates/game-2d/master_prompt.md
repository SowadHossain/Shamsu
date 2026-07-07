# 2D game: feature contract for the model

This template is a working single/local 2D canvas game. The fixed-timestep loop,
keyboard input plumbing, renderer, and HUD are already built and compile with a
placeholder "bouncing box". Your job is to turn the placeholder into the game
described by the PRD by filling the marked holes only. Do not rewrite the loop.

## Rules

- Fill only the holes in `manifest.yaml`. Each hole is the region between
  `// HOLE:<id>` and `// END:<id>`; keep both marker lines.
- `GameState` is a numeric bag: add any fields you need (paddleY, ballX, ...).
  Everything is a number (use 0/1 for booleans like `over`), so new state never
  breaks the build.
- Keep every export the rest of the app imports (`createState`, `readActions`,
  `update`, `scorePoints`, `checkWin`, `render`).
- Keep it compiling. `npm run build` runs `tsc` then `vite build`.
- No em dashes in code comments.

## Where game logic lives

- `src/game/state.ts` - initial entities/state (`// HOLE:entity`).
- `src/game/input.ts` - map keys to named actions (`// HOLE:input`).
- `src/game/update.ts` - per-tick logic, scoring, win (`// HOLE:update`, `score`, `win`).
- `src/game/render.ts` - draw the entities (`// HOLE:render`).

## What is already done (do not rebuild)

The canvas bootstrap, the `requestAnimationFrame` loop with a clamped `dt`, the
keydown/keyup input map, and the HUD binding.
