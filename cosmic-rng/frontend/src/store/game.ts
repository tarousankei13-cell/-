import { create } from "zustand";
import { ApiError, get, put, setCsrf } from "../lib/api";
import type {
  AchievementGrant,
  Cinematic,
  FeedEvent,
  FirstDiscovery,
  HudState,
  Me,
  OfflineSummary,
  PlayerSettings,
  PublicConfig,
  RollResult,
} from "../lib/types";

export type Toast = { id: number; kind: "info" | "success" | "error" | "warning" | "cosmic"; title: string; body?: string; ttl: number };

export type RevealJob =
  | { type: "roll"; roll: RollResult; full: boolean }
  | { type: "offline"; summary: OfflineSummary }
  | { type: "artifact"; cinematic: Cinematic; result?: any; global?: boolean }
  | { type: "global_event"; data: Record<string, any> };

type Listener = (data: any) => void;

/** Tiny pub/sub so pages can react to realtime events without coupling to the socket. */
class Emitter {
  private map = new Map<string, Set<Listener>>();
  on(type: string, fn: Listener): () => void {
    if (!this.map.has(type)) this.map.set(type, new Set());
    this.map.get(type)!.add(fn);
    return () => {
      this.map.get(type)?.delete(fn);
    };
  }
  emit(type: string, data: any) {
    this.map.get(type)?.forEach((fn) => {
      try {
        fn(data);
      } catch (e) {
        console.error(e);
      }
    });
  }
}
export const events = new Emitter();

interface GameStore {
  config: PublicConfig | null;
  me: Me | null;
  hud: HudState | null;
  serverOffset: number;
  feed: FeedEvent[];
  online: number;
  unread: number;
  connected: boolean;
  bootstrapped: boolean;
  toasts: Toast[];
  reveals: RevealJob[];
  worldBanners: FirstDiscovery[];
  adminStats: Record<string, any> | null;
  liveRankings: Record<string, any[]>;
  maintenance: { enabled: boolean; message: string } | null;

  bootstrap: () => Promise<void>;
  setHud: (h: HudState) => void;
  refreshHud: () => Promise<void>;
  patchHud: (p: Partial<HudState>) => void;
  setStardust: (n: number) => void;
  toast: (title: string, kind?: Toast["kind"], body?: string, ttl?: number) => void;
  dismissToast: (id: number) => void;
  pushFeed: (ev: FeedEvent) => void;
  setFeed: (evs: FeedEvent[]) => void;
  saveSettings: (patch: Partial<PlayerSettings> | Record<string, any>) => Promise<void>;
  enqueueReveal: (job: RevealJob) => void;
  shiftReveal: () => void;
  pushBanner: (b: FirstDiscovery) => void;
  shiftBanner: () => void;
  set: (p: Partial<GameStore>) => void;
  announceAchievements: (list: AchievementGrant[]) => void;
}

let toastId = 1;
const recentAchievements = new Map<string, number>();

export const useGame = create<GameStore>((set, getState) => ({
  config: null,
  me: null,
  hud: null,
  serverOffset: 0,
  feed: [],
  online: 0,
  unread: 0,
  connected: false,
  bootstrapped: false,
  toasts: [],
  reveals: [],
  worldBanners: [],
  adminStats: null,
  liveRankings: {},
  maintenance: null,

  set: (p) => set(p),

  bootstrap: async () => {
    const config = await get<PublicConfig>("/api/config").catch(() => null);
    set({ config, maintenance: config?.maintenance ?? null });
    try {
      const me = await get<Me>("/api/me");
      setCsrf(me.csrf);
      set({ me, unread: me.unread });
      const hud = await get<HudState>("/api/state");
      getState().setHud(hud);
    } catch (e) {
      if (!(e instanceof ApiError) || e.status !== 401) console.warn("bootstrap", e);
      set({ me: null });
    } finally {
      set({ bootstrapped: true });
    }
  },

  setHud: (hud) => {
    const offset = new Date(hud.server_time).getTime() - Date.now();
    set((s) => ({ hud, serverOffset: offset, me: s.me ? { ...s.me, user: { ...s.me.user, stardust: hud.stardust, level: hud.level, xp: hud.xp, roll_counter: hud.roll_counter, auto_roll: hud.auto_roll } } : s.me }));
  },

  refreshHud: async () => {
    if (!getState().me) return;
    try {
      getState().setHud(await get<HudState>("/api/state"));
    } catch {
      /* transient */
    }
  },

  patchHud: (p) => set((s) => (s.hud ? { hud: { ...s.hud, ...p } } : {})),

  setStardust: (n) => set((s) => ({ hud: s.hud ? { ...s.hud, stardust: n } : s.hud, me: s.me ? { ...s.me, user: { ...s.me.user, stardust: n } } : s.me })),

  toast: (title, kind = "info", body, ttl = 4200) => {
    const me = getState().me;
    if (me && !me.settings.notifications.toasts && kind === "info") return;
    const t: Toast = { id: toastId++, kind, title, body, ttl };
    set((s) => ({ toasts: [...s.toasts.slice(-4), t] }));
    window.setTimeout(() => getState().dismissToast(t.id), ttl);
  },

  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  pushFeed: (ev) => set((s) => (s.feed.some((f) => f.id === ev.id) ? {} : { feed: [ev, ...s.feed].slice(0, 60) })),
  setFeed: (evs) => set({ feed: evs.slice(0, 60) }),

  saveSettings: async (patch) => {
    const res = await put<{ settings: PlayerSettings }>("/api/settings", { settings: patch });
    set((s) => (s.me ? { me: { ...s.me, settings: res.settings } } : {}));
  },

  enqueueReveal: (job) => set((s) => ({ reveals: [...s.reveals, job].slice(-20) })),
  shiftReveal: () => set((s) => ({ reveals: s.reveals.slice(1) })),
  pushBanner: (b) => set((s) => ({ worldBanners: [...s.worldBanners, b].slice(-5) })),
  shiftBanner: () => set((s) => ({ worldBanners: s.worldBanners.slice(1) })),

  announceAchievements: (list) => {
    const now = Date.now();
    for (const a of list) {
      const last = recentAchievements.get(a.key);
      if (last && now - last < 15000) continue;
      recentAchievements.set(a.key, now);
      const reward = a.rewards?.stardust ? `報酬 ✦${a.rewards.stardust.toLocaleString()}` : "";
      getState().toast(`${a.world_first ? "🌍 WORLD FIRST — " : "実績解除: "}${a.name}`, a.world_first ? "cosmic" : "success",
        [a.description, reward].filter(Boolean).join(" / "), a.world_first ? 9000 : 5500);
    }
  },
}));

export function serverNow(): number {
  return Date.now() + useGame.getState().serverOffset;
}

export function useRarity(key: string | undefined) {
  const rarities = useGame((s) => s.config?.rarities);
  return rarities?.find((r) => r.key === key);
}
