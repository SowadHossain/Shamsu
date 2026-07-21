// Colyseus client wrapper: create or join a room by code, send/receive, and
// reconnect if the socket drops. The rest of the app reads a plain snapshot of
// the synced state each frame instead of wiring per-field callbacks.
import { Client, Room } from "colyseus.js";

export interface NetPlayer {
  id: string;
  name: string;
  ready: boolean;
  connected: boolean;
  score: number;
  x: number;
  y: number;
  z: number;
  ry: number;
  bag: string;
}

export interface NetSnapshot {
  phase: string;
  roomCode: string;
  hostId: string;
  winnerId: string;
  matchId: string;
  winScore: number;
  players: NetPlayer[];
}

interface MoveTransform {
  x: number;
  y: number;
  z: number;
  ry: number;
  bag?: string;
}

function serverUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  // The dev server proxies /api to the game server; the websocket goes straight
  // to the game server port (override with VITE_SERVER_PORT).
  const port = (import.meta.env.VITE_SERVER_PORT as string) || "2567";
  return `${proto}://${window.location.hostname}:${port}`;
}

export class NetClient {
  private client: Client;
  private room: Room | null = null;
  private token: string | null = null;
  private reconnecting = false;

  constructor(url: string = serverUrl()) {
    this.client = new Client(url);
  }

  get sessionId(): string {
    return this.room?.sessionId ?? "";
  }

  get connected(): boolean {
    return this.room !== null;
  }

  async createRoom(name: string, winScore: number): Promise<string> {
    const code = makeRoomCode();
    this.room = await this.client.create("game", { name, winScore, roomCode: code });
    this.bind();
    return this.roomCode() || code;
  }

  async joinByCode(code: string, name: string): Promise<void> {
    // The room is defined with filterBy(["roomCode"]) on the server, so joining
    // "game" with a roomCode matches the room that was created with that code.
    this.room = await this.client.join("game", { name, roomCode: code.trim().toUpperCase() });
    this.bind();
  }

  roomCode(): string {
    const state = this.room?.state as { roomCode?: string } | undefined;
    return state?.roomCode ?? "";
  }

  snapshot(): NetSnapshot | null {
    // state (and its fields) can be briefly undefined between joining and the
    // first sync, so read everything defensively.
    const state = this.room?.state as unknown as RawState | undefined;
    if (!state) return null;
    const players: NetPlayer[] = [];
    if (state.players && typeof state.players.forEach === "function") {
      state.players.forEach((player) => {
        players.push({
          id: player.id,
          name: player.name,
          ready: player.ready,
          connected: player.connected,
          score: player.score,
          x: player.x,
          y: player.y,
          z: player.z,
          ry: player.ry,
          bag: player.bag,
        });
      });
    }
    return {
      phase: state.phase ?? "lobby",
      roomCode: state.roomCode ?? "",
      hostId: state.hostId ?? "",
      winnerId: state.winnerId ?? "",
      matchId: state.matchId ?? "",
      winScore: state.winScore ?? 10,
      players,
    };
  }

  sendMove(transform: MoveTransform): void {
    this.room?.send("move", transform);
  }

  sendScore(delta: number): void {
    this.room?.send("score", delta);
  }

  setReady(ready: boolean): void {
    this.room?.send("ready", ready);
  }

  startMatch(): void {
    this.room?.send("start");
  }

  backToLobby(): void {
    this.room?.send("backToLobby");
  }

  leave(): void {
    this.room?.leave();
    this.room = null;
    this.token = null;
  }

  private bind(): void {
    if (!this.room) return;
    this.token = this.room.reconnectionToken;
    this.room.onLeave((code) => {
      // Codes above 1000 are abnormal closes; try to get back into the room.
      if (code > 1000 && this.token) void this.tryReconnect();
    });
  }

  private async tryReconnect(): Promise<void> {
    if (this.reconnecting || !this.token) return;
    this.reconnecting = true;
    for (let attempt = 0; attempt < 5; attempt += 1) {
      try {
        this.room = await this.client.reconnect(this.token);
        this.bind();
        this.reconnecting = false;
        return;
      } catch {
        await delay(1000);
      }
    }
    this.reconnecting = false;
  }
}

interface RawPlayer {
  id: string;
  name: string;
  ready: boolean;
  connected: boolean;
  score: number;
  x: number;
  y: number;
  z: number;
  ry: number;
  bag: string;
}

interface RawState {
  phase: string;
  roomCode: string;
  hostId: string;
  winnerId: string;
  matchId: string;
  winScore: number;
  players: { forEach: (cb: (player: RawPlayer) => void) => void };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function makeRoomCode(): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let code = "";
  for (let i = 0; i < 5; i += 1) {
    code += alphabet[Math.floor(Math.random() * alphabet.length)];
  }
  return code;
}
