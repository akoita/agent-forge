export class Terminal {
  cols = 80;
  rows = 24;
  private dataListeners = new Set<(data: string) => void>();
  written: string[] = [];

  loadAddon(): void {}
  open(): void {}
  write(data: string): void {
    this.written.push(data);
  }
  clear(): void {
    this.written = [];
  }
  onData(cb: (data: string) => void): { dispose: () => void } {
    this.dataListeners.add(cb);
    return {
      dispose: () => {
        this.dataListeners.delete(cb);
      },
    };
  }
  dispose(): void {
    this.dataListeners.clear();
  }
}
