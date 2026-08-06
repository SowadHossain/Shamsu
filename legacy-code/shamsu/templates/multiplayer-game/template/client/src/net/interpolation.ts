// Remote player interpolation. Network snapshots arrive a few times per second;
// we render at 60fps, so we ease each remote player toward its latest known
// transform instead of snapping. This keeps other players moving smoothly.

export interface RenderTransform {
  x: number;
  y: number;
  z: number;
  ry: number;
}

export class RemoteInterpolator {
  private targets = new Map<string, RenderTransform>();
  private current = new Map<string, RenderTransform>();

  // Feed the latest known transform for a remote player.
  setTarget(id: string, transform: RenderTransform): void {
    this.targets.set(id, transform);
    if (!this.current.has(id)) this.current.set(id, { ...transform });
  }

  remove(id: string): void {
    this.targets.delete(id);
    this.current.delete(id);
  }

  keep(ids: Set<string>): void {
    for (const id of [...this.current.keys()]) {
      if (!ids.has(id)) this.remove(id);
    }
  }

  // Ease every tracked player toward its target. `alpha` in 0..1 per frame.
  step(alpha: number): void {
    for (const [id, target] of this.targets) {
      const now = this.current.get(id) ?? { ...target };
      now.x += (target.x - now.x) * alpha;
      now.y += (target.y - now.y) * alpha;
      now.z += (target.z - now.z) * alpha;
      now.ry = lerpAngle(now.ry, target.ry, alpha);
      this.current.set(id, now);
    }
  }

  get(id: string): RenderTransform | undefined {
    return this.current.get(id);
  }
}

function lerpAngle(a: number, b: number, alpha: number): number {
  let diff = b - a;
  while (diff > Math.PI) diff -= Math.PI * 2;
  while (diff < -Math.PI) diff += Math.PI * 2;
  return a + diff * alpha;
}
