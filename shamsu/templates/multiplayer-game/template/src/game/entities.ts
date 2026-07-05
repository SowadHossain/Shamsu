export type Vec3 = [number, number, number];

// HOLE:entity.player
export type Player = {
  id: string;
  name: string;
  position: Vec3;
  score: number;
  color: string;
  ready: boolean;
};

// HOLE:entity.world
export type Obstacle = {
  id: string;
  position: Vec3;
  radius: number;
};

export type EndState = {
  ended: boolean;
  reason: string;
  winnerId?: string;
};

export type GamePhase = "menu" | "lobby" | "playing" | "ended";

export type GameState = {
  phase: GamePhase;
  players: Player[];
  localPlayerId: string;
  obstacles: Obstacle[];
  elapsed: number;
  endState: EndState;
};

export type InputState = {
  left: boolean;
  right: boolean;
  forward: boolean;
  back: boolean;
};
