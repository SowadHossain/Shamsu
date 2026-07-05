import type { GameState, InputState, Player, Vec3, EndState } from "./entities";

const MOVE_SPEED = 4;
const FINISH_SCORE = 10;

export function createInitialState(): GameState {
  const players: Player[] = [
    {
      id: "local-player",
      name: "You",
      position: [-1.25, 0, 0],
      score: 0,
      color: "#38bdf8",
      ready: true,
    },
    {
      id: "remote-player",
      name: "Remote",
      position: [1.25, 0, 0],
      score: 0,
      color: "#f97316",
      ready: true,
    },
  ];

  return {
    phase: "lobby",
    players,
    localPlayerId: "local-player",
    obstacles: [{ id: "center", position: [0, 0, -3], radius: 0.5 }],
    elapsed: 0,
    endState: { ended: false, reason: "" },
  };
}

// HOLE:rule.update
export function updateGameState(state: GameState, input: InputState, dt: number): GameState {
  if (state.phase !== "playing") {
    return state;
  }

  const players = state.players.map((player) => {
    if (player.id !== state.localPlayerId) {
      return {
        ...player,
        position: [player.position[0], 0, Math.sin(state.elapsed + dt) * 1.8] as Vec3,
      };
    }

    const x = player.position[0] + (Number(input.right) - Number(input.left)) * MOVE_SPEED * dt;
    const z = player.position[2] + (Number(input.back) - Number(input.forward)) * MOVE_SPEED * dt;
    return {
      ...player,
      position: [Math.max(-5, Math.min(5, x)), 0, Math.max(-6, Math.min(3, z))] as Vec3,
      score: player.score + dt,
    };
  });

  const nextState = {
    ...state,
    players,
    elapsed: state.elapsed + dt,
  };

  return {
    ...nextState,
    endState: checkEndState(nextState),
    phase: checkEndState(nextState).ended ? "ended" : nextState.phase,
  };
}

// HOLE:rule.end_condition
export function checkEndState(state: GameState): EndState {
  const winner = state.players.find((player) => player.score >= FINISH_SCORE);
  if (winner) {
    return { ended: true, reason: "score-limit", winnerId: winner.id };
  }
  return { ended: false, reason: "" };
}

export function createInputState(): InputState {
  return { left: false, right: false, forward: false, back: false };
}
