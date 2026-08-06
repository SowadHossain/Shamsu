// Game rules. The model fills the marked holes with real game logic. The
// placeholder is a trivial "move a cube, touch a pickup to score, first to the
// win score wins" so every system runs before any model edits.
//
// Networking model: this is a relay game. Each client moves its own player and
// grabs its own pickups locally, then reports transform and score to the server.
// The server sums scores across players and decides the match end. So the win
// check here is a local mirror; the server is authoritative for persistence.
import { Entity, makePickup, makePlayer } from "./entities";
import { InputState } from "./controller";

export interface World {
  entities: Map<string, Entity>;
  localId: string;
  score: number;
  winScore: number;
  phase: "playing" | "ended";
  nextPickupId: number;
}

export interface TickContext {
  input: InputState;
  dt: number;
  // Report a scoring event to the network layer (server keeps the real total).
  onScore: (delta: number) => void;
}

const MOVE_SPEED = 5;
const ARENA = 6;
const PICKUP_COUNT = 4;

export function createWorld(localId: string, winScore: number): World {
  const world: World = {
    entities: new Map(),
    localId,
    score: 0,
    winScore,
    phase: "playing",
    nextPickupId: 0,
  };
  world.entities.set(localId, makePlayer(localId, "#38bdf8"));
  for (let i = 0; i < PICKUP_COUNT; i += 1) spawnPickup(world);
  return world;
}

// HOLE:rule.update
// Advance the game one fixed tick: move the local player from input, move or
// spawn world entities, and handle collisions. Call ctx.onScore(n) when the
// player earns points. Keep it deterministic for a given input and dt.
export function updateWorld(world: World, ctx: TickContext): void {
  const player = world.entities.get(world.localId);
  if (!player || world.phase !== "playing") return;

  const dx = (Number(ctx.input.right) - Number(ctx.input.left)) * MOVE_SPEED * ctx.dt;
  const dz = (Number(ctx.input.back) - Number(ctx.input.forward)) * MOVE_SPEED * ctx.dt;
  player.x = clamp(player.x + dx, -ARENA, ARENA);
  player.z = clamp(player.z + dz, -ARENA, ARENA);
  if (dx !== 0 || dz !== 0) player.ry = Math.atan2(dx, dz);

  for (const entity of world.entities.values()) {
    if (entity.type !== "pickup") continue;
    if (distanceSquared(player, entity) < 1.0) {
      const points = scorePoints();
      world.score += points;
      ctx.onScore(points);
      recycle(world, entity);
    }
  }

  if (checkWin(world)) world.phase = "ended";
}

// HOLE:rule.win
// The end condition. Return true when the local player should see the match end.
export function checkWin(world: World): boolean {
  return world.score >= world.winScore;
}

// HOLE:rule.score
// How many points one scoring event is worth.
export function scorePoints(): number {
  return 1;
}

// Placeholder helpers below are not holes. The model may replace them.

function spawnPickup(world: World): void {
  const id = `pickup-${world.nextPickupId}`;
  world.nextPickupId += 1;
  const x = randomInArena();
  const z = randomInArena();
  world.entities.set(id, makePickup(id, x, z));
}

function recycle(world: World, entity: Entity): void {
  entity.x = randomInArena();
  entity.z = randomInArena();
}

function randomInArena(): number {
  return (Math.random() * 2 - 1) * (ARENA - 1);
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function distanceSquared(a: Entity, b: Entity): number {
  const dx = a.x - b.x;
  const dz = a.z - b.z;
  return dx * dx + dz * dz;
}
