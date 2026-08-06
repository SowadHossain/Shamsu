// Relay room. The server does not simulate the game; it stores shared state,
// relays player transforms, runs the room lifecycle, and writes match results
// to SQLite so the leaderboard persists.
import { Room, type Client } from "@colyseus/core";
import { randomUUID } from "node:crypto";
import { GameState, Player } from "./schema.js";
import { upsertPlayer, startMatch, endMatch, recordScore } from "./db.js";

function makeRoomCode(): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  let code = "";
  for (let i = 0; i < 5; i += 1) {
    code += alphabet[Math.floor(Math.random() * alphabet.length)];
  }
  return code;
}

interface JoinOptions {
  name?: string;
  roomCode?: string;
  winScore?: number;
}

interface MoveMessage {
  x: number;
  y: number;
  z: number;
  ry: number;
  bag?: string;
}

export class GameRoom extends Room<GameState> {
  maxClients = 8;

  onCreate(options: JoinOptions): void {
    const state = new GameState();
    state.roomCode = (options?.roomCode || makeRoomCode()).toUpperCase();
    state.winScore = Number(options?.winScore) > 0 ? Number(options.winScore) : 10;
    this.setState(state);
    this.setMetadata({ roomCode: state.roomCode });

    this.onMessage("move", (client: Client, message: MoveMessage) => {
      const player = this.state.players.get(client.sessionId);
      if (!player || this.state.phase !== "playing") return;
      player.x = message.x;
      player.y = message.y;
      player.z = message.z;
      player.ry = message.ry;
      if (typeof message.bag === "string") player.bag = message.bag;
    });

    this.onMessage("ready", (client: Client, ready: boolean) => {
      const player = this.state.players.get(client.sessionId);
      if (player) player.ready = Boolean(ready);
    });

    this.onMessage("start", (client: Client) => {
      if (client.sessionId !== this.state.hostId) return;
      if (this.state.phase !== "lobby") return;
      const players = [...this.state.players.values()];
      if (players.length === 0 || !players.every((p) => p.ready)) return;
      this.startGame();
    });

    this.onMessage("score", (client: Client, delta: number) => {
      const player = this.state.players.get(client.sessionId);
      if (!player || this.state.phase !== "playing") return;
      player.score += Number(delta) || 0;
      if (player.score >= this.state.winScore) this.endGame(player.id);
    });

    this.onMessage("backToLobby", (client: Client) => {
      if (client.sessionId !== this.state.hostId) return;
      this.state.phase = "lobby";
      this.state.winnerId = "";
      for (const player of this.state.players.values()) {
        player.ready = false;
        player.score = 0;
      }
    });
  }

  onJoin(client: Client, options: JoinOptions): void {
    const player = new Player();
    player.id = client.sessionId;
    player.name = (options?.name || "Player").slice(0, 24);
    this.state.players.set(client.sessionId, player);
    upsertPlayer(player.id, player.name);
    if (!this.state.hostId) this.state.hostId = client.sessionId;
  }

  async onLeave(client: Client, consented: boolean): Promise<void> {
    const player = this.state.players.get(client.sessionId);
    if (player) player.connected = false;
    if (!consented) {
      try {
        await this.allowReconnection(client, 20);
        const back = this.state.players.get(client.sessionId);
        if (back) back.connected = true;
        return;
      } catch {
        // reconnection window elapsed; fall through and remove the player
      }
    }
    this.state.players.delete(client.sessionId);
    if (this.state.hostId === client.sessionId) {
      const next = this.state.players.keys().next().value;
      this.state.hostId = next ?? "";
    }
  }

  private startGame(): void {
    this.state.phase = "playing";
    this.state.matchId = randomUUID();
    startMatch(this.state.matchId, this.state.roomCode);
    for (const player of this.state.players.values()) player.score = 0;
  }

  private endGame(winnerId: string): void {
    if (this.state.phase === "ended") return;
    this.state.phase = "ended";
    this.state.winnerId = winnerId;
    const ranked = [...this.state.players.values()].sort((a, b) => b.score - a.score);
    ranked.forEach((player, index) => {
      recordScore(this.state.matchId, player.id, player.score, index + 1);
    });
    endMatch(this.state.matchId, winnerId);
  }
}
