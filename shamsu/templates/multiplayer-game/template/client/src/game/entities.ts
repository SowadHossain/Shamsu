// Generic entity system. Every entity has an id, a type tag, a transform, and a
// free-form `bag` for game-specific fields. Keeping game data in `bag` means the
// model can add fields in the holes below without changing the base type.

export interface Entity {
  id: string;
  type: string;
  x: number;
  y: number;
  z: number;
  ry: number;
  bag: Record<string, unknown>;
}

export function bagNumber(entity: Entity, key: string, fallback: number): number {
  const value = entity.bag[key];
  return typeof value === "number" ? value : fallback;
}

export function bagString(entity: Entity, key: string, fallback: string): string {
  const value = entity.bag[key];
  return typeof value === "string" ? value : fallback;
}

// HOLE:entity.player
// Player-specific fields (health, size, color, abilities). The base Entity
// already carries the transform; put game fields in the bag so the renderer and
// rules can read them. Keep makePlayer returning a valid Entity.
export function makePlayer(id: string, color: string): Entity {
  return {
    id,
    type: "player",
    x: 0,
    y: 0.5,
    z: 0,
    ry: 0,
    bag: { color, size: 0.8 },
  };
}

// HOLE:entity.world
// Non-player entities such as obstacles and pickups, with their fields. The
// placeholder uses a single pickup type. Add more entity makers here.
export function makePickup(id: string, x: number, z: number): Entity {
  return {
    id,
    type: "pickup",
    x,
    y: 0.5,
    z,
    ry: 0,
    bag: { color: "#fbbf24", radius: 0.45 },
  };
}
