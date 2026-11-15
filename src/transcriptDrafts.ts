export type Segment = {start:number; end:number; text:string};
type Draft = { value:Segment[]; revision:number; saved:number; timer?:ReturnType<typeof setTimeout>; task?:Promise<void> };

/** Serializes writes per recording while allowing other recordings to save independently. */
export class TranscriptDrafts {
  private drafts = new Map<string, Draft>();
  private save:(id:string, value:Segment[])=>Promise<void>;
  private saved:(id:string, value:Segment[])=>void;
  private failed:(error:unknown)=>void;
  private delay:number;
  constructor(save:(id:string, value:Segment[])=>Promise<void>, saved:(id:string, value:Segment[])=>void, failed:(error:unknown)=>void, delay=700) {
    this.save=save; this.saved=saved; this.failed=failed; this.delay=delay;
  }

  get(id:string):Segment[]|undefined { return this.drafts.get(id)?.value; }
  update(id:string, value:Segment[]) {
    const draft = this.drafts.get(id) ?? {value, revision:0, saved:0};
    draft.value = value; draft.revision += 1;
    if (draft.timer) clearTimeout(draft.timer);
    this.drafts.set(id, draft);
    draft.timer = setTimeout(() => { void this.flush(id).catch(this.failed); }, this.delay);
  }
  async flush(id:string):Promise<void> {
    const draft = this.drafts.get(id);
    if (!draft) return;
    if (draft.timer) { clearTimeout(draft.timer); draft.timer = undefined; }
    if (draft.task) { await draft.task; return this.flush(id); }
    const task = (async () => {
      while (draft.saved < draft.revision) {
        const revision = draft.revision;
        const snapshot = draft.value;
        await this.save(id, snapshot);
        draft.saved = revision;
      }
      if (draft.timer) clearTimeout(draft.timer);
      this.saved(id, draft.value);
      this.drafts.delete(id);
    })();
    draft.task = task;
    try { await task; } finally { draft.task = undefined; }
  }
  async flushAll():Promise<void> {
    // Edits can arrive while a save is in flight; flush until the map is empty.
    while (this.drafts.size) await Promise.all([...this.drafts.keys()].map(id => this.flush(id)));
  }
}
