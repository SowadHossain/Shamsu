// Entry point: one HTTP server hosts both the Colyseus websocket (gameplay) and
// the REST API (leaderboard + settings), so the client only needs one origin.
import { createServer } from "node:http";
import express from "express";
import cors from "cors";
import { Server } from "@colyseus/core";
import { WebSocketTransport } from "@colyseus/ws-transport";
import { GameRoom } from "./room.js";
import { getLeaderboard, getSettings, saveSettings } from "./db.js";

const PORT = Number(process.env.PORT || 2567);

const app = express();
app.use(cors());
app.use(express.json());

app.get("/api/health", (_req, res) => {
  res.json({ ok: true });
});

app.get("/api/leaderboard", (_req, res) => {
  res.json(getLeaderboard(10));
});

app.get("/api/settings/:playerId", (req, res) => {
  res.json(getSettings(req.params.playerId) ?? {});
});

app.post("/api/settings/:playerId", (req, res) => {
  saveSettings(req.params.playerId, req.body ?? {});
  res.json({ ok: true });
});

const httpServer = createServer(app);
const gameServer = new Server({
  transport: new WebSocketTransport({ server: httpServer }),
});
// filterBy roomCode lets clients join a specific room by its short code.
gameServer.define("game", GameRoom).filterBy(["roomCode"]);

httpServer.listen(PORT, () => {
  console.log(`SHAMSU multiplayer server listening on http://127.0.0.1:${PORT}`);
});
