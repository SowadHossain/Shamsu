// Colyseus synced state. This is the authoritative shared record every client
// sees: the player list, ready state, scores, transforms, and match phase.
// Movement is relayed (clients send their own transform), not simulated here.
//
// We use defineTypes instead of @type decorators so the schema works the same
// under tsc, tsx, and esbuild without depending on decorator transform settings.
import { Schema, MapSchema, defineTypes } from "@colyseus/schema";

export class Player extends Schema {
  id = "";
  name = "";
  ready = false;
  connected = true;
  score = 0;
  x = 0;
  y = 0;
  z = 0;
  ry = 0;
  // Generic per-player state bag as JSON, so game-specific fields can flow
  // without changing the schema.
  bag = "{}";
}

defineTypes(Player, {
  id: "string",
  name: "string",
  ready: "boolean",
  connected: "boolean",
  score: "number",
  x: "number",
  y: "number",
  z: "number",
  ry: "number",
  bag: "string",
});

export class GameState extends Schema {
  phase = "lobby"; // lobby | playing | ended
  roomCode = "";
  hostId = "";
  winnerId = "";
  matchId = "";
  winScore = 10;
  players = new MapSchema<Player>();
}

defineTypes(GameState, {
  phase: "string",
  roomCode: "string",
  hostId: "string",
  winnerId: "string",
  matchId: "string",
  winScore: "number",
  players: { map: Player },
});
