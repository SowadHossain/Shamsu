// Game state. GameState is a numeric bag: add any fields you need and TypeScript
// will not complain, so filling entities never breaks the build. Booleans are
// represented as 0 or 1 (e.g. `over`).

export interface GameState {
  width: number;
  height: number;
  scoreLeft: number;
  scoreRight: number;
  over: number;
  [key: string]: number;
}

export function createState(width: number, height: number): GameState {
  const state: GameState = {
    width,
    height,
    scoreLeft: 0,
    scoreRight: 0,
    over: 0,
  };
  // HOLE:entity
  // Placeholder: a single box bouncing around the canvas.
  state.boxX = width / 2;
  state.boxY = height / 2;
  state.vx = 120;
  state.vy = 90;
  // END:entity
  return state;
}
