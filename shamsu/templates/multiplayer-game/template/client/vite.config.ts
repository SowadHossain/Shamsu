import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The server (Colyseus + SQLite) runs on port 2567 by default. The client talks
// to it over a websocket for gameplay and over /api for leaderboard and
// settings, proxied here so everything is same-origin in the browser. Ports can
// be overridden with VITE_SERVER_PORT and VITE_CLIENT_PORT.
const serverPort = process.env.VITE_SERVER_PORT || "2567";
const clientPort = Number(process.env.VITE_CLIENT_PORT || "5173");

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: clientPort,
    strictPort: Boolean(process.env.VITE_CLIENT_PORT),
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${serverPort}`,
        changeOrigin: true,
      },
    },
  },
});
