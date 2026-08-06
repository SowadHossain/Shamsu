import type { GameState } from "../game/state";

// Binds the HUD element to the live score. Extend as the PRD needs.

export function renderHud(el: HTMLElement, state: GameState): void {
  const score = `Left ${state.scoreLeft}  |  Right ${state.scoreRight}`;
  el.textContent = state.over ? `${score}  -  Game Over` : score;
}
