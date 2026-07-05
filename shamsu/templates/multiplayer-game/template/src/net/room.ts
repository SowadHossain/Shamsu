import { Client, Room } from "colyseus.js";
import type { GameState } from "../game/entities";

export type RoomEvent =
  | { type: "connected"; playerId: string }
  | { type: "state"; state: GameState }
  | { type: "closed" };

export class RoomClient {
  private client: Client | null = null;
  private room: Room | null = null;
  private listeners = new Set<(event: RoomEvent) => void>();

  connect(roomId: string): void {
    const protocol = window.location.protocol === "https:" ? "wss" : "ws";
    this.client = new Client(`${protocol}://${window.location.hostname}:8787`);
    void this.client.joinOrCreate(roomId).then((room) => {
      this.room = room;
      this.emit({ type: "connected", playerId: room.sessionId });
      room.onMessage("state", (state: GameState) => {
        this.emit({ type: "state", state });
      });
      room.onLeave(() => {
        this.emit({ type: "closed" });
      });
    }).catch(() => {
      this.emit({ type: "connected", playerId: "local-player" });
    });
  }

  publish(state: GameState): void {
    if (this.room) {
      this.room.send("state", state);
    }
  }

  onEvent(listener: (event: RoomEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private emit(event: RoomEvent): void {
    for (const listener of this.listeners) {
      listener(event);
    }
  }
}
