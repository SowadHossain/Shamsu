// Definition of Done runner. Starts the server and client, then drives two
// headless browsers through menu -> lobby -> game -> end, checking each DoD item.
// Persistence is checked through the server API. Exits non-zero if any required
// check fails.
import { chromium } from "playwright";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SMOKE_DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(SMOKE_DIR, "..");
const SERVER_DIR = path.join(ROOT, "server");
// Use uncommon ports so the DoD never collides with other dev servers.
const SERVER_PORT = process.env.DOD_SERVER_PORT || "2591";
const CLIENT_PORT = process.env.DOD_CLIENT_PORT || "5391";
const CLIENT_URL = `http://127.0.0.1:${CLIENT_PORT}`;
const API = `http://127.0.0.1:${SERVER_PORT}`;
const DB_PATH = path.join(SERVER_DIR, "dod.sqlite");

const results = [];
function record(id, ok, detail = "") {
  results.push({ id, ok });
  console.log(`${ok ? "PASS" : "FAIL"} ${id}${detail ? " - " + detail : ""}`);
}
async function guard(id, fn) {
  try {
    const detail = await fn();
    record(id, true, detail || "");
  } catch (err) {
    record(id, false, err.message);
  }
}

async function waitForUrl(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url);
      if (res.ok || res.status === 404) return;
    } catch {
      // not up yet
    }
    await sleep(500);
  }
  throw new Error(`timed out waiting for ${url}`);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const procs = [];
function start(command, cwd, env) {
  const child = spawn(command, { cwd, env: { ...process.env, ...env }, shell: true, stdio: "ignore" });
  procs.push(child);
  return child;
}
function killAll() {
  for (const child of procs) {
    try {
      if (process.platform === "win32") spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { stdio: "ignore" });
      else child.kill("SIGKILL");
    } catch {
      // ignore
    }
  }
}

async function main() {
  // build.succeeds: the production build produced client + server output.
  const built = existsSync(path.join(ROOT, "client", "dist", "index.html")) && existsSync(path.join(SERVER_DIR, "dist", "index.js"));
  record("build.succeeds", built, built ? "" : "run npm run build first");

  start("node --import tsx src/index.ts", SERVER_DIR, { PORT: SERVER_PORT, SHAMSU_DB: DB_PATH });
  start("npm --workspace client run dev", ROOT, {
    VITE_SERVER_PORT: SERVER_PORT,
    VITE_CLIENT_PORT: CLIENT_PORT,
  });
  await waitForUrl(`${API}/api/health`, 20000);
  await waitForUrl(CLIENT_URL, 30000);

  const browser = await chromium.launch();
  const pageA = await (await browser.newContext()).newPage();
  const pageB = await (await browser.newContext()).newPage();

  await guard("menu.renders", async () => {
    await pageA.goto(CLIENT_URL);
    await pageA.waitForSelector("[data-testid=main-menu]", { timeout: 10000 });
  });

  let code = "";
  await guard("net.connects", async () => {
    await pageA.click("[data-testid=play-button]");
    await pageA.click("[data-testid=create-room]");
    await pageA.waitForFunction(
      () => {
        const el = document.querySelector("[data-testid=room-code]");
        return el && (el.textContent || "").trim().length > 0;
      },
      { timeout: 10000 },
    );
    code = (await pageA.textContent("[data-testid=room-code]"))?.trim() || "";
    if (!code) throw new Error("no room code");
    return `code=${code}`;
  });

  await guard("lobby.works", async () => {
    await pageB.goto(CLIENT_URL);
    await pageB.click("[data-testid=play-button]");
    await pageB.fill("[data-testid=code-input]", code);
    await pageB.click("[data-testid=join-room]");
    await pageA.waitForFunction(() => document.querySelectorAll("[data-testid=player-entry]").length >= 2, { timeout: 10000 });
    await pageB.waitForFunction(() => document.querySelectorAll("[data-testid=player-entry]").length >= 2, { timeout: 10000 });
  });

  // start the match: both ready, host starts
  await pageA.click("[data-testid=ready-button]");
  await pageB.click("[data-testid=ready-button]");
  await pageA.waitForSelector("[data-testid=start-button]:not([disabled])", { timeout: 10000 });
  await pageA.click("[data-testid=start-button]");
  await pageA.waitForSelector("[data-testid=game-scene]", { timeout: 10000 });
  await pageB.waitForSelector("[data-testid=game-scene]", { timeout: 10000 });

  await guard("net.two_players_visible", async () => {
    await pageA.waitForFunction(() => document.querySelectorAll("[data-testid=player-entry]").length >= 2, { timeout: 10000 });
    const a = await pageA.$$eval("[data-testid=player-entry]", (els) => els.length);
    const b = await pageB.$$eval("[data-testid=player-entry]", (els) => els.length);
    if (a < 2 || b < 2) throw new Error(`a=${a} b=${b}`);
    return `a=${a} b=${b}`;
  });

  await guard("hud.visible", async () => {
    if (!(await pageA.isVisible("[data-testid=hud]"))) throw new Error("hud not visible");
  });

  await guard("loop.runs", async () => {
    const read = () => pageA.getAttribute("[data-testid=net-debug]", "data-tick");
    const t0 = Number(await read());
    await pageA.keyboard.down("KeyD");
    await sleep(800);
    await pageA.keyboard.up("KeyD");
    const t1 = Number(await read());
    const x = await pageA.getAttribute("[data-testid=net-debug]", "data-x");
    if (t1 - t0 < 30) throw new Error(`tick advanced only ${t1 - t0}`);
    return `ticks ${t0}->${t1}, x=${x}`;
  });

  await guard("end.condition", async () => {
    // Drive the local player's score to the win threshold via the test hook.
    await pageA.evaluate(() => window.__shamsu.net.sendScore(10));
    await pageA.waitForSelector("[data-testid=game-over]", { timeout: 10000 });
    await pageB.waitForSelector("[data-testid=game-over]", { timeout: 10000 });
  });

  await guard("score.persists", async () => {
    const res = await fetch(`${API}/api/leaderboard`);
    const rows = await res.json();
    const scored = rows.filter((row) => row.total_score > 0);
    if (scored.length === 0) throw new Error(`leaderboard has no scores: ${JSON.stringify(rows)}`);
    return `${scored.length} scored player(s)`;
  });

  await browser.close();
}

main()
  .catch((err) => {
    console.error("DoD runner error:", err.message);
    record("runner", false, err.message);
  })
  .finally(async () => {
    killAll();
    const failed = results.filter((r) => !r.ok);
    console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
    await sleep(500);
    process.exit(failed.length === 0 ? 0 : 1);
  });
