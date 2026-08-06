// SQLite persistence for players, matches, and settings.
// The schema auto-migrates on first import (CREATE TABLE IF NOT EXISTS).
import Database from "better-sqlite3";
import path from "path";
import { fileURLToPath } from "url";

const here = path.dirname(fileURLToPath(import.meta.url));
const DB_PATH = process.env.SHAMSU_DB || path.join(here, "..", "data.sqlite");

const SCHEMA = `
CREATE TABLE IF NOT EXISTS players (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  total_score INTEGER DEFAULT 0,
  games_played INTEGER DEFAULT 0,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
  id TEXT PRIMARY KEY,
  room_code TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  winner_id TEXT
);
CREATE TABLE IF NOT EXISTS match_players (
  match_id TEXT NOT NULL,
  player_id TEXT NOT NULL,
  score INTEGER DEFAULT 0,
  placement INTEGER,
  PRIMARY KEY (match_id, player_id)
);
CREATE TABLE IF NOT EXISTS settings (
  player_id TEXT PRIMARY KEY,
  data TEXT NOT NULL
);
`;

const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");
db.exec(SCHEMA);

export interface LeaderboardRow {
  id: string;
  name: string;
  total_score: number;
  games_played: number;
}

export function upsertPlayer(id: string, name: string): void {
  db.prepare(
    `INSERT INTO players (id, name, created_at)
     VALUES (?, ?, ?)
     ON CONFLICT(id) DO UPDATE SET name = excluded.name`,
  ).run(id, name, Date.now());
}

export function startMatch(id: string, roomCode: string): void {
  db.prepare(
    `INSERT OR REPLACE INTO matches (id, room_code, started_at) VALUES (?, ?, ?)`,
  ).run(id, roomCode, Date.now());
}

export function endMatch(matchId: string, winnerId: string | null): void {
  db.prepare(`UPDATE matches SET ended_at = ?, winner_id = ? WHERE id = ?`).run(
    Date.now(),
    winnerId,
    matchId,
  );
}

// Record one player's result for a match and roll it into their running totals.
export function recordScore(
  matchId: string,
  playerId: string,
  score: number,
  placement: number | null,
): void {
  const tx = db.transaction(() => {
    db.prepare(
      `INSERT OR REPLACE INTO match_players (match_id, player_id, score, placement)
       VALUES (?, ?, ?, ?)`,
    ).run(matchId, playerId, score, placement);
    db.prepare(
      `UPDATE players
       SET total_score = total_score + ?, games_played = games_played + 1
       WHERE id = ?`,
    ).run(score, playerId);
  });
  tx();
}

export function getLeaderboard(limit = 10): LeaderboardRow[] {
  return db
    .prepare(
      `SELECT id, name, total_score, games_played
       FROM players
       ORDER BY total_score DESC, games_played DESC
       LIMIT ?`,
    )
    .all(limit) as LeaderboardRow[];
}

export function getSettings(playerId: string): Record<string, unknown> | null {
  const row = db
    .prepare(`SELECT data FROM settings WHERE player_id = ?`)
    .get(playerId) as { data: string } | undefined;
  if (!row) return null;
  try {
    return JSON.parse(row.data);
  } catch {
    return null;
  }
}

export function saveSettings(playerId: string, data: Record<string, unknown>): void {
  db.prepare(
    `INSERT OR REPLACE INTO settings (player_id, data) VALUES (?, ?)`,
  ).run(playerId, JSON.stringify(data));
}

export default db;
