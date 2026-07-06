// Headless two-client check of the netcode + persistence path (no browser).
// Verifies: create room, second client joins by code, both appear, start,
// score to the win threshold, match ends with a winner.
import { Client } from "colyseus.js";

const URL = process.env.GAME_URL || "ws://127.0.0.1:2567";
const CODE = "SMOKE";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const c1 = new Client(URL);
  const c2 = new Client(URL);

  const host = await c1.create("game", { name: "Alice", winScore: 3, roomCode: CODE });
  const guest = await c2.join("game", { name: "Bob", roomCode: CODE });
  await sleep(400);

  const seenPlayers = host.state.players.size;
  if (seenPlayers !== 2) throw new Error(`host sees ${seenPlayers} players, expected 2`);
  if (guest.state.players.size !== 2) throw new Error(`guest sees ${guest.state.players.size} players`);

  host.send("ready", true);
  guest.send("ready", true);
  await sleep(300);
  host.send("start");
  await sleep(300);
  if (host.state.phase !== "playing") throw new Error(`phase is ${host.state.phase}, expected playing`);

  for (let i = 0; i < 3; i += 1) {
    host.send("score", 1);
    await sleep(120);
  }
  await sleep(500);

  const players = [];
  host.state.players.forEach((p) => players.push({ name: p.name, score: p.score }));
  console.log("phase:", host.state.phase, "winner:", host.state.winnerId);
  console.log("players:", JSON.stringify(players));
  console.log("roomCode:", host.state.roomCode);

  host.leave();
  guest.leave();
  await sleep(200);

  if (host.state.phase !== "ended") throw new Error("match did not end");
  if (!host.state.winnerId) throw new Error("no winner recorded");
  console.log("NETCHECK OK");
}

main().catch((err) => {
  console.error("NETCHECK FAIL:", err.message);
  process.exit(1);
});
