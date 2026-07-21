import { useEffect } from "react";
import { useStore } from "../store";
import { fetchSettings, saveSettingsRemote } from "../api";

export function Settings() {
  const settings = useStore((state) => state.settings);
  const updateSettings = useStore((state) => state.updateSettings);
  const setScreen = useStore((state) => state.setScreen);
  const playerId = useStore((state) => state.playerId);

  useEffect(() => {
    fetchSettings(playerId).then((data) => {
      const patch: { name?: string; volume?: number } = {};
      if (typeof data.name === "string") patch.name = data.name;
      if (typeof data.volume === "number") patch.volume = data.volume;
      if (Object.keys(patch).length > 0) updateSettings(patch);
    });
  }, [playerId, updateSettings]);

  const save = () => {
    saveSettingsRemote(playerId, { name: settings.name, volume: settings.volume });
    setScreen("menu");
  };

  return (
    <div className="panel" data-testid="settings">
      <h2>Settings</h2>
      <label className="field">
        Name
        <input
          data-testid="name-input"
          value={settings.name}
          maxLength={24}
          onChange={(event) => updateSettings({ name: event.target.value })}
        />
      </label>
      <label className="field">
        Volume
        <input
          type="range"
          min={0}
          max={1}
          step={0.1}
          value={settings.volume}
          onChange={(event) => updateSettings({ volume: Number(event.target.value) })}
        />
      </label>
      <div className="controls-help">
        <h3>Controls</h3>
        <p>Move: WASD or Arrow keys</p>
        <p>Pause: Esc</p>
      </div>
      <div className="menu-buttons">
        <button onClick={save}>Save</button>
        <button onClick={() => setScreen("menu")}>Back</button>
      </div>
    </div>
  );
}
