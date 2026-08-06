// REST calls to the game server (proxied through Vite at /api).
export interface LeaderboardRow {
  id: string;
  name: string;
  total_score: number;
  games_played: number;
}

export async function fetchLeaderboard(): Promise<LeaderboardRow[]> {
  const response = await fetch("/api/leaderboard");
  if (!response.ok) return [];
  return (await response.json()) as LeaderboardRow[];
}

export async function fetchSettings(playerId: string): Promise<Record<string, unknown>> {
  const response = await fetch(`/api/settings/${encodeURIComponent(playerId)}`);
  if (!response.ok) return {};
  return (await response.json()) as Record<string, unknown>;
}

export async function saveSettingsRemote(
  playerId: string,
  data: Record<string, unknown>,
): Promise<void> {
  await fetch(`/api/settings/${encodeURIComponent(playerId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
}
