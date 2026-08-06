import { useEffect, useState } from "react";
import { useStore } from "../store";
import { fetchLeaderboard, LeaderboardRow } from "../api";

export function Leaderboard() {
  const setScreen = useStore((state) => state.setScreen);
  const [rows, setRows] = useState<LeaderboardRow[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    fetchLeaderboard().then((data) => {
      setRows(data);
      setLoaded(true);
    });
  }, []);

  return (
    <div className="panel" data-testid="leaderboard">
      <h2>Leaderboard</h2>
      {loaded && rows.length === 0 ? (
        <p>No scores yet. Play a match.</p>
      ) : (
        <ol className="leaderboard-list">
          {rows.map((row) => (
            <li key={row.id} data-testid="leaderboard-entry">
              <span>{row.name}</span>
              <span>{row.total_score}</span>
            </li>
          ))}
        </ol>
      )}
      <button onClick={() => setScreen("menu")}>Back</button>
    </div>
  );
}
