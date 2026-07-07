import type { GameState } from "./state";

// Draw the game each frame. The canvas is cleared to black first; fill the hole
// with the PRD's real entities (paddles, ball, ...).

export function render(ctx: CanvasRenderingContext2D, state: GameState): void {
  ctx.clearRect(0, 0, state.width, state.height);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, state.width, state.height);
  ctx.fillStyle = "#fff";
  // HOLE:render
  // Placeholder: draw the bouncing box.
  ctx.fillRect(state.boxX - 10, state.boxY - 10, 20, 20);
  // END:render
}
