import type { GameState } from "./state";
import type { Actions } from "./input";

// Per-tick game logic. Fill the holes with the PRD's rules. `dt` is seconds
// since the last frame (clamped). Everything on `state` is a number.

export function update(state: GameState, actions: Actions, dt: number): void {
  if (state.over) return;
  // HOLE:update
  // Placeholder: bounce a box, ignoring input.
  void actions;
  state.boxX += state.vx * dt;
  state.boxY += state.vy * dt;
  if (state.boxX < 0 || state.boxX > state.width) state.vx = -state.vx;
  if (state.boxY < 0 || state.boxY > state.height) state.vy = -state.vy;
  // END:update
  if (checkWin(state)) state.over = 1;
}

export function scorePoints(): number {
  // HOLE:score
  return 1;
  // END:score
}

export function checkWin(state: GameState): boolean {
  // HOLE:win
  return state.scoreLeft >= 11 || state.scoreRight >= 11;
  // END:win
}
