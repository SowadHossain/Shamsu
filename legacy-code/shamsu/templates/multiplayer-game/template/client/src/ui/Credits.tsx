import { useStore } from "../store";

export function Credits() {
  const setScreen = useStore((state) => state.setScreen);
  return (
    <div className="panel" data-testid="credits">
      <h2>Credits</h2>
      <p>Built on the SHAMSU multiplayer game template.</p>
      <p>Stack: React, React-Three-Fiber, Three.js, Colyseus, Rapier, SQLite.</p>
      <button onClick={() => setScreen("menu")}>Back</button>
    </div>
  );
}
