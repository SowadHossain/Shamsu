// Client UI state (Zustand). Screen navigation, pause, local settings, and a
// stable per-browser player id. Settings and id persist in localStorage.
import { create } from "zustand";

export type Screen =
  | "menu"
  | "settings"
  | "leaderboard"
  | "credits"
  | "join"
  | "lobby"
  | "game"
  | "gameover";

export interface Settings {
  name: string;
  volume: number;
}

interface AppStore {
  screen: Screen;
  paused: boolean;
  settings: Settings;
  playerId: string;
  setScreen: (screen: Screen) => void;
  setPaused: (paused: boolean) => void;
  updateSettings: (patch: Partial<Settings>) => void;
}

const SETTINGS_KEY = "shamsu.settings";
const ID_KEY = "shamsu.playerId";

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<Settings>;
      return { name: parsed.name ?? "Player", volume: parsed.volume ?? 0.6 };
    }
  } catch {
    // ignore malformed storage
  }
  return { name: "Player", volume: 0.6 };
}

function saveSettings(settings: Settings): void {
  try {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // storage may be unavailable; settings just will not persist
  }
}

function loadPlayerId(): string {
  try {
    const existing = localStorage.getItem(ID_KEY);
    if (existing) return existing;
    const id =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `p-${Math.random().toString(36).slice(2)}`;
    localStorage.setItem(ID_KEY, id);
    return id;
  } catch {
    return `p-${Math.random().toString(36).slice(2)}`;
  }
}

export const useStore = create<AppStore>((set) => ({
  screen: "menu",
  paused: false,
  settings: loadSettings(),
  playerId: loadPlayerId(),
  setScreen: (screen) => set({ screen }),
  setPaused: (paused) => set({ paused }),
  updateSettings: (patch) =>
    set((state) => {
      const settings = { ...state.settings, ...patch };
      saveSettings(settings);
      return { settings };
    }),
}));
