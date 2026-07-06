# Multiplayer game: feature contract for the model

This template is a working multiplayer 3D browser game. Menus, lobby, networking,
the game loop, physics, HUD, and SQLite persistence are already built and pass
the Definition of Done with a placeholder game (move a cube, touch a pickup to
score, first to the win score wins).

Your job is to turn the placeholder into the game described by the PRD by filling
the marked holes only. Do not rewrite the plumbing.

## Rules

- Read the file around each `// HOLE:` marker, and the files that import it,
  before writing.
- Fill only the holes listed in `manifest.yaml`. Keep every export the rest of
  the app imports (App.tsx, world.tsx, loop.ts, GameScreen.tsx read these).
- The game is a Colyseus relay: each client moves its own player and reports
  transform and score to the server. The server sums scores and ends the match.
- Keep it compiling. Run `npm run build` (client + server typecheck) before you
  are done.
- No em dashes in code comments.

## Where game logic lives

- `client/src/game/entities.ts` - entity fields (player, world).
- `client/src/game/rules.ts` - per-tick update, scoring, win condition.
- `client/src/ui/Hud.tsx` - bind HUD slots to game state.

## What is already done (do not rebuild)

Menus and screens, lobby with room codes and ready/start, Colyseus client and
room, remote player interpolation, the fixed-timestep loop, Rapier physics,
the follow camera and renderer, the HUD frame, and the SQLite leaderboard.
