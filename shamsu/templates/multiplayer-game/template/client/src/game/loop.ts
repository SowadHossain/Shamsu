// Fixed-timestep game loop. The render loop (R3F useFrame) calls update() with
// the real frame delta; we run game ticks at a fixed rate so movement and rules
// are stable regardless of frame rate. A Rapier physics world is initialized and
// stepped every tick so game logic in the holes can use it.
import RAPIER from "@dimforge/rapier3d-compat";
import { World, createWorld, updateWorld } from "./rules";
import { InputState, createInput } from "./controller";
import { NetClient } from "../net/client";

const STEP_SECONDS = 1 / 60;
const SEND_INTERVAL = 0.05; // 20 transform updates per second

export class GameLoop {
  world: World | null = null;
  input: InputState = createInput();
  tick = 0;

  private accumulator = 0;
  private sendTimer = 0;
  private rapier: RAPIER.World | null = null;
  private ready = false;
  private readonly net: NetClient;

  constructor(net: NetClient) {
    this.net = net;
  }

  async init(localId: string, winScore: number): Promise<void> {
    await RAPIER.init();
    this.rapier = new RAPIER.World({ x: 0, y: -9.81, z: 0 });
    // A ground collider so the physics world has something to simulate.
    this.rapier.createCollider(RAPIER.ColliderDesc.cuboid(20, 0.1, 20));
    this.world = createWorld(localId, winScore);
    this.ready = true;
  }

  update(realDelta: number): void {
    if (!this.ready || !this.world) return;
    this.accumulator += Math.min(realDelta, 0.1);
    while (this.accumulator >= STEP_SECONDS) {
      this.runTick(STEP_SECONDS);
      this.accumulator -= STEP_SECONDS;
    }
  }

  private runTick(dt: number): void {
    if (!this.world) return;
    updateWorld(this.world, {
      input: this.input,
      dt,
      onScore: (delta) => this.net.sendScore(delta),
    });
    this.rapier?.step();
    this.tick += 1;

    this.sendTimer += dt;
    if (this.sendTimer >= SEND_INTERVAL) {
      this.sendTimer = 0;
      const player = this.world.entities.get(this.world.localId);
      if (player) {
        this.net.sendMove({ x: player.x, y: player.y, z: player.z, ry: player.ry });
      }
    }
  }

  dispose(): void {
    this.rapier?.free();
    this.rapier = null;
    this.ready = false;
    this.world = null;
  }
}
