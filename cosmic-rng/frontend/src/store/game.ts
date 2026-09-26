import { create } from "zustand";
import { ApiError, get, put, setCsrf } from "../lib/api";
import type {
  AchievementGrant,
  Cinematic,
  FeedEvent,
  FirstDiscovery,
  HudState,
  LiveReveal,
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
  /** Toasts withheld while a cutscene is on screen. */
  heldToasts: Toast[];
  /** Set while a roll is in flight, before its cutscene exists to hold them. */
  toastHold: boolean;
  /** The last boot could not reach the server at all. */
  offline: boolean;
  reveals: RevealJob[];
  worldBanners: FirstDiscovery[];
  /** Rare finds from other players, shown briefly at the edge of the screen. */
  live: LiveReveal[];
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
  flushHeldToasts: () => void;
  setToastHold: (on: boolean) => void;
  pushFeed: (ev: FeedEvent) => void;
  setFeed: (evs: FeedEvent[]) => void;
  saveSettings: (patch: Partial<PlayerSettings> | Record<string, any>) => Promise<void>;
  enqueueReveal: (job: RevealJob) => void;
  shiftReveal: () => void;
  pushBanner: (b: FirstDiscovery) => void;
  shiftBanner: () => void;
  pushLive: (r: LiveReveal) => void;
  shiftLive: () => void;
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
  heldToasts: [],
  toastHold: false,
  offline: false,
  reveals: [],
  worldBanners: [],
  live: [],
  adminStats: null,
  liveRankings: {},
  maintenance: null,

  set: (p) => set(p),

  bootstrap: async () => {
    // Short deadlines here on purpose: this runs before anything is on screen,
    // and a slow answer must not be the difference between a login form and a
    // spinner. Anything that misses it is treated as "server unreachable",
    // which the landing page says out loud and offers to retry.
    const BOOT_TIMEOUT = 8000;
    let reachable = true;
    const unreachable = (e: unknown) => { if (e instanceof ApiError && e.status === 0) reachable = false; };

    // Both in flight at once: they do not depend on each other, and boot should
    // take one timeout to fail, not two in a row.
    const [config, meRes] = await Promise.all([
      get<PublicConfig>("/api/config", undefined, undefined, BOOT_TIMEOUT).catch((e) => { unreachable(e); return null; }),
      get<Me>("/api/me", undefined, undefined, BOOT_TIMEOUT).then(
        (me) => ({ me }),
        (e) => {
          // 401 is the normal answer for a signed-out visitor, not a failure.
          if (!(e instanceof ApiError) || e.status !== 401) console.warn("bootstrap", e);
          unreachable(e);
          return { me: null };
        },
      ),
    ]);
    set({ config, maintenance: config?.maintenance ?? null });

    if (meRes.me) {
      setCsrf(meRes.me.csrf);
      set({ me: meRes.me, unread: meRes.me.unread });
      try {
        getState().setHud(await get<HudState>("/api/state", undefined, undefined, BOOT_TIMEOUT));
      } catch (e) {
        unreachable(e);
        console.warn("bootstrap state", e);
      }
    } else {
      set({ me: null });
    }
    set({ bootstrapped: true, offline: !reachable });
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
    const st = getState();
    if (st.me && !st.me.settings.notifications.toasts && kind === "info") return;
    const t: Toast = { id: toastId++, kind, title, body, ttl };
    // A reveal is the moment the whole game is built around; five notification
    // cards stacked over it is the opposite of a payoff. Hold them and show the
    // lot once the stage is clear.
    // The socket announces a world first as soon as the server commits it, which
    // can land before the roll's own response has come back and queued the
    // cutscene. Without the hold flag that one notification beats the reveal
    // onto the screen — and it is the notification for the very item being
    // revealed.
    if (st.reveals.length > 0 || st.toastHold) {
      set((s) => ({ heldToasts: [...s.heldToasts, t].slice(-8) }));
      return;
    }
    set((s) => ({ toasts: [...s.toasts.slice(-2), t] }));
    window.setTimeout(() => getState().dismissToast(t.id), ttl);
  },

  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),

  setToastHold: (on) => {
    set({ toastHold: on });
    if (!on && getState().reveals.length === 0) getState().flushHeldToasts();
  },

  /** Release what the cutscene held back, spaced so they can be read. */
  flushHeldToasts: () => {
    const held = getState().heldToasts;
    if (!held.length) return;
    set({ heldToasts: [] });
    held.forEach((t, i) => window.setTimeout(() => {
      set((s) => ({ toasts: [...s.toasts.slice(-2), t] }));
      window.setTimeout(() => getState().dismissToast(t.id), t.ttl);
    }, i * 550));
  },

  pushFeed: (ev) => set((s) => (s.feed.some((f) => f.id === ev.id) ? {} : { feed: [ev, ...s.feed].slice(0, 60) })),
  setFeed: (evs) => set({ feed: evs.slice(0, 60) }),

  saveSettings: async (patch) => {
    const res = await put<{ settings: PlayerSettings }>("/api/settings", { settings: patch });
    set((s) => (s.me ? { me: { ...s.me, settings: res.settings } } : {}));
  },

  enqueueReveal: (job) => set((s) => ({ reveals: [...s.reveals, job].slice(-20) })),
  shiftReveal: () => {
    set((s) => ({ reveals: s.reveals.slice(1) }));
    if (getState().reveals.length === 0 && !getState().toastHold) getState().flushHeldToasts();
  },
  pushBanner: (b) => set((s) => ({ worldBanners: [...s.worldBanners, b].slice(-5) })),
  shiftBanner: () => set((s) => ({ worldBanners: s.worldBanners.slice(1) })),
  pushLive: (r) => set((s) => ({ live: [...s.live, r].slice(-3) })),
  shiftLive: () => set((s) => ({ live: s.live.slice(1) })),

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
