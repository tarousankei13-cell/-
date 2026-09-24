import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useGame } from "./store/game";
import { connectSocket, disconnectSocket } from "./lib/socket";
import { audio, bgmForBiome } from "./audio/engine";
import { CosmosRenderer } from "./visual/cosmos";
import { RollCutscene, ArtifactCutscene } from "./components/Cutscene";
import { Avatar, Spinner } from "./components/ui";
import { fmtCompact, fmtInt } from "./lib/format";
import { post } from "./lib/api";
import type { FirstDiscovery } from "./lib/types";
import { OfflineReport } from "./pages/OfflineReport";
import { Landing } from "./pages/Landing";
import { RollPage } from "./pages/Roll";

const Inventory = lazy(() => import("./pages/Inventory").then((m) => ({ default: m.Inventory })));
const Collection = lazy(() => import("./pages/Collection").then((m) => ({ default: m.Collection })));
const Equipment = lazy(() => import("./pages/Equipment").then((m) => ({ default: m.Equipment })));
const Biomes = lazy(() => import("./pages/Biomes").then((m) => ({ default: m.Biomes })));
const Shop = lazy(() => import("./pages/Shop").then((m) => ({ default: m.Shop })));
const Market = lazy(() => import("./pages/Market").then((m) => ({ default: m.Market })));
const Trade = lazy(() => import("./pages/Trade").then((m) => ({ default: m.Trade })));
const Quests = lazy(() => import("./pages/Quests").then((m) => ({ default: m.Quests })));
const Achievements = lazy(() => import("./pages/Achievements").then((m) => ({ default: m.Achievements })));
const Ranking = lazy(() => import("./pages/Ranking").then((m) => ({ default: m.Ranking })));
const Profile = lazy(() => import("./pages/Profile").then((m) => ({ default: m.Profile })));
const Settings = lazy(() => import("./pages/Settings").then((m) => ({ default: m.Settings })));
const Admin = lazy(() => import("./pages/admin/Admin").then((m) => ({ default: m.Admin })));

const NAV = [
  { to: "/roll", icon: "✦", label: "抽選" },
  { to: "/inventory", icon: "🎒", label: "所持品", feature: "inventory" },
  { to: "/collection", icon: "📖", label: "図鑑", feature: "collection" },
  { to: "/equipment", icon: "⚙", label: "装備", feature: "equipment" },
  { to: "/biomes", icon: "🌌", label: "Biome", feature: "biome" },
  { to: "/shop", icon: "🛒", label: "商店", feature: "shop" },
  { to: "/market", icon: "💱", label: "市場", feature: "market" },
  { to: "/trade", icon: "🤝", label: "取引", feature: "trade" },
  { to: "/quests", icon: "📜", label: "依頼", feature: "quests" },
  { to: "/achievements", icon: "🏆", label: "実績", feature: "achievements" },
  { to: "/ranking", icon: "📊", label: "順位", feature: "ranking" },
  { to: "/profile", icon: "👤", label: "戦績", feature: "profile" },
  { to: "/settings", icon: "⚡", label: "設定" },
];

// ------------------------------------------------------------------ cosmos
let renderer: CosmosRenderer | null = null;
export const getCosmos = () => renderer;

function CosmosCanvas() {
  const ref = useRef<HTMLCanvasElement>(null);
  const graphics = useGame((s) => s.me?.settings.graphics);
  const biomeTheme = useGame((s) => s.hud?.biome?.theme);

  useEffect(() => {
    if (!ref.current) return;
    const r = new CosmosRenderer(ref.current);
    renderer = r;
    r.start();
    const onResize = () => r.resize();
    const onPointer = (e: PointerEvent) => r.setPointer(e.clientX / window.innerWidth, e.clientY / window.innerHeight);
    const onVisibility = () => (document.hidden ? r.stop() : r.start());
    window.addEventListener("resize", onResize);
    window.addEventListener("pointermove", onPointer, { passive: true });
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("pointermove", onPointer);
      document.removeEventListener("visibilitychange", onVisibility);
      r.stop();
      renderer = null;
    };
  }, []);

  useEffect(() => {
    renderer?.setQuality(graphics?.quality ?? "high", graphics?.particles ?? 1, graphics?.reduced_motion ?? false);
  }, [graphics?.quality, graphics?.particles, graphics?.reduced_motion]);

  useEffect(() => {
    if (graphics?.background_fx === false) {
      renderer?.setTheme({ bg: ["#05060f", "#080b1c", "#0d1026"], nebula: ["#12183a", "#1a1030", "#0d2440"], particles: "none", intensity: 0.1, vignette: 0.35 }, true);
    } else {
      renderer?.setTheme(biomeTheme);
    }
  }, [biomeTheme, graphics?.background_fx]);

  return <canvas id="cosmos" ref={ref} aria-hidden="true" />;
}

// ------------------------------------------------------------------ toasts
function Toasts() {
  const toasts = useGame((s) => s.toasts);
  const dismiss = useGame((s) => s.dismissToast);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} onClick={() => dismiss(t.id)}>
          <div className="t">{t.title}</div>
          {t.body && <div className="b">{t.body}</div>}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------- world banners
function WorldBanner() {
  const banners = useGame((s) => s.worldBanners);
  const shift = useGame((s) => s.shiftBanner);
  const current: FirstDiscovery | undefined = banners[0];
  useEffect(() => {
    if (!current) return;
    const t = window.setTimeout(shift, 7000);
    return () => window.clearTimeout(t);
  }, [current, shift]);
  if (!current) return null;
  return (
    <div className="world-banner" onClick={shift}>
      <span className="label">FIRST DISCOVERY</span>
      <span className="ellipsis">
        <strong style={{ color: "var(--gold)" }}>{current.player}</strong> が{" "}
        <strong className={`r-${current.item.rarity}`}>{current.item.name}</strong> を世界初発見
      </span>
    </div>
  );
}

// --------------------------------------------------------- cutscene queue
function RevealQueue() {
  const reveals = useGame((s) => s.reveals);
  const shift = useGame((s) => s.shiftReveal);
  const job = reveals[0];
  if (!job) return null;
  if (job.type === "roll") return <RollCutscene key={job.roll.id} roll={job.roll} cosmos={getCosmos()} onDone={shift} />;
  if (job.type === "artifact") return <ArtifactCutscene key={job.cinematic.artifact + reveals.length} cinematic={job.cinematic} cosmos={getCosmos()} onDone={shift} global={job.global} />;
  if (job.type === "offline") return <OfflineReport summary={job.summary} onClose={shift} />;
  return null;
}

// ------------------------------------------------------------------ topbar
function TopBar() {
  const me = useGame((s) => s.me);
  const hud = useGame((s) => s.hud);
  const online = useGame((s) => s.online);
  const connected = useGame((s) => s.connected);
  const unread = useGame((s) => s.unread);
  const [muted, setMuted] = useState(() => audio.volumes.muted);

  const toggleMute = () => {
    const next = !muted;
    setMuted(next);
    audio.setVolumes({ muted: next });
    useGame.getState().saveSettings({ audio: { ...(me?.settings.audio ?? { master: 0.8, bgm: 0.5, sfx: 0.8 }), muted: next } }).catch(() => undefined);
  };

  return (
    <header className="topbar">
      <Link to="/roll" className="brand" style={{ textDecoration: "none" }} aria-label="COSMIC RNG">
        <span className="brand-full">COSMIC RNG</span>
        <span className="brand-mark" aria-hidden>✦ CRNG</span>
      </Link>
      <div className="spacer" />
      {hud && (
        <>
          <span className="chip mono" title="Stardust">✦ {fmtCompact(hud.stardust)}</span>
          <span className="chip mono" title={`レベル ${hud.level}`}>Lv.{hud.level}</span>
        </>
      )}
      <span className="chip tiny" title={connected ? "リアルタイム接続中" : "再接続中…"}>
        <span style={{ width: 7, height: 7, borderRadius: "50%", background: connected ? "var(--good)" : "var(--warn)", display: "inline-block" }} />
        {fmtInt(online)}
      </span>
      <button className="icon-btn" onClick={toggleMute} aria-label={muted ? "音を有効化" : "ミュート"} title={muted ? "音を有効化" : "ミュート"}>
        {muted ? "🔇" : "🔊"}
      </button>
      <Link to="/profile" className="row user-chip" style={{ gap: 6, textDecoration: "none", color: "inherit", position: "relative" }} aria-label="プロフィール">
        <Avatar user={me?.user ?? null} size={30} />
        {unread > 0 && (
          <span className="badge" style={{ position: "absolute", top: -4, right: -4, color: "var(--bad)", background: "rgba(20,8,16,0.95)", padding: "0 5px" }}>
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </Link>
    </header>
  );
}

function BottomNav() {
  const me = useGame((s) => s.me);
  const unlocks = useGame((s) => s.config?.unlocks);
  const level = useGame((s) => s.hud?.level ?? s.me?.user.level ?? 1);
  const unread = useGame((s) => s.unread);
  const navRef = useRef<HTMLElement>(null);
  const { pathname } = useLocation();
  // On a phone the strip is wider than the screen, so the destination you are on
  // can sit off the edge with nothing saying the row scrolls at all.
  useEffect(() => {
    const el = navRef.current?.querySelector<HTMLElement>("a.active");
    el?.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
  }, [pathname]);
  if (!me) return null;
  return (
    <nav className="bottom-nav" ref={navRef} aria-label="メインナビゲーション">
      {NAV.map((n) => {
        const need = n.feature ? unlocks?.[n.feature] ?? 1 : 1;
        const locked = level < need;
        return (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => (isActive ? "active" : "")} style={locked ? { opacity: 0.4 } : undefined}
            title={locked ? `Lv.${need}で解放` : n.label} onClick={() => audio.sfx("click")}>
            <span className="ic">{locked ? "🔒" : n.icon}</span>
            <span>{n.label}</span>
            {n.to === "/profile" && unread > 0 && <span className="dot" />}
          </NavLink>
        );
      })}
      {me.is_admin && (
        <NavLink to="/admin" className={({ isActive }) => `admin ${isActive ? "active" : ""}`} onClick={() => audio.sfx("click")}>
          <span className="ic">🛠</span>
          <span>管理</span>
        </NavLink>
      )}
    </nav>
  );
}

// --------------------------------------------------------------- audio gate
function useAudioBootstrap() {
  const me = useGame((s) => s.me);
  const biome = useGame((s) => s.hud?.biome);
  const unlocked = useRef(false);

  useEffect(() => {
    const onGesture = async () => {
      if (unlocked.current) return;
      unlocked.current = true;
      const ok = await audio.unlock();
      if (ok) {
        const s = useGame.getState().me?.settings.audio;
        if (s) audio.setVolumes({ master: s.master, bgm: s.bgm, sfx: s.sfx, muted: s.muted });
        audio.startBgm(bgmForBiome(useGame.getState().hud?.biome?.theme));
      }
    };
    window.addEventListener("pointerdown", onGesture, { once: false });
    window.addEventListener("keydown", onGesture, { once: false });
    return () => {
      window.removeEventListener("pointerdown", onGesture);
      window.removeEventListener("keydown", onGesture);
    };
  }, []);

  useEffect(() => {
    const a = me?.settings.audio;
    if (a) audio.setVolumes({ master: a.master, bgm: a.bgm, sfx: a.sfx, muted: a.muted });
  }, [me?.settings.audio]);

  useEffect(() => {
    if (audio.ready) audio.startBgm(bgmForBiome(biome?.theme));
  }, [biome?.theme?.bgm, biome?.key]);
}

// ---------------------------------------------------------------- app root
function Shell() {
  const me = useGame((s) => s.me);
  const location = useLocation();
  useAudioBootstrap();

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
  }, [location.pathname]);

  useEffect(() => {
    const ui = me?.settings.ui;
    document.documentElement.style.setProperty("--font-scale", String(ui?.font_scale ?? 1));
    document.body.classList.toggle("high-contrast", !!ui?.high_contrast);
    document.body.classList.toggle("reduced-motion", !!me?.settings.graphics.reduced_motion);
  }, [me?.settings.ui, me?.settings.graphics.reduced_motion]);

  return (
    <div className="app-shell">
      <TopBar />
      <BottomNav />
      <main>
        <Suspense fallback={<Spinner label="読み込み中…" />}>
          <Routes>
            <Route path="/roll" element={<RollPage />} />
            <Route path="/inventory" element={<Inventory />} />
            <Route path="/collection" element={<Collection />} />
            <Route path="/equipment" element={<Equipment />} />
            <Route path="/biomes" element={<Biomes />} />
            <Route path="/shop" element={<Shop />} />
            <Route path="/market" element={<Market />} />
            <Route path="/trade" element={<Trade />} />
            <Route path="/quests" element={<Quests />} />
            <Route path="/achievements" element={<Achievements />} />
            <Route path="/ranking" element={<Ranking />} />
            <Route path="/profile" element={<Profile />} />
            <Route path="/profile/:userId" element={<Profile />} />
            <Route path="/settings" element={<Settings />} />
            {me?.is_admin && <Route path="/admin/*" element={<Admin />} />}
            <Route path="*" element={<Navigate to="/roll" replace />} />
          </Routes>
        </Suspense>
      </main>
    </div>
  );
}

function MaintenanceBar() {
  const m = useGame((s) => s.maintenance);
  const isAdmin = useGame((s) => s.me?.is_admin);
  if (!m?.enabled) return null;
  return (
    <div style={{
      position: "sticky", top: 0, zIndex: 90, padding: "7px 14px", textAlign: "center",
      background: "linear-gradient(90deg, rgba(120,60,20,0.95), rgba(70,30,10,0.95))",
      borderBottom: "1px solid rgba(255,193,77,0.5)", fontSize: "0.86rem",
    }}>
      🛠 {m.message || "メンテナンス中です"}{isAdmin ? "（管理者は操作可能）" : ""}
    </div>
  );
}

export default function App() {
  const bootstrapped = useGame((s) => s.bootstrapped);
  const me = useGame((s) => s.me);
  const bootstrap = useGame((s) => s.bootstrap);
  const claimedRef = useRef(false);

  useEffect(() => {
    bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    if (!me) return;
    connectSocket();
    return () => disconnectSocket();
  }, [me?.user.id]);

  // Offline auto-roll results are claimed once per session on load.
  const claimOffline = useCallback(async () => {
    if (claimedRef.current || !me) return;
    claimedRef.current = true;
    try {
      const res = await post<{ offline: any; state: any; progress: any }>("/api/offline/claim");
      if (res.state) useGame.getState().setHud(res.state);
      if (res.offline) useGame.getState().enqueueReveal({ type: "offline", summary: res.offline });
      if (res.progress) useGame.getState().announceAchievements(res.progress.achievements ?? []);
    } catch {
      /* not critical */
    }
  }, [me]);

  useEffect(() => {
    if (me) claimOffline();
  }, [me?.user.id, claimOffline]);

  return (
    <>
      <CosmosCanvas />
      <MaintenanceBar />
      {!bootstrapped ? (
        <Spinner label="宇宙を展開しています…" />
      ) : me ? (
        <Shell />
      ) : (
        <Routes>
          <Route path="*" element={<Landing />} />
        </Routes>
      )}
      <Toasts />
      <WorldBanner />
      <RevealQueue />
    </>
  );
}
