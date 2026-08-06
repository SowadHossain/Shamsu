import { createServer } from "http";
import { Room, Server } from "colyseus";

type RelayMessage = {
  type: "state";
  state: unknown;
};

class RelayRoom extends Room {
  onCreate() {
    this.onMessage("state", (client, state: RelayMessage["state"]) => {
      this.broadcast("state", state, { except: client });
    });
  }
}

const port = Number(process.env.PORT || 8787);
const server = createServer();
const gameServer = new Server({ server });

gameServer.define("default", RelayRoom);

server.listen(port, "127.0.0.1", () => {
  console.log(`SHAMSU Colyseus relay listening on ws://127.0.0.1:${port}`);
});
