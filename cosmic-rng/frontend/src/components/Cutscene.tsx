import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ItemIcon } from "./ItemIcon";
import { Name } from "./ui";
import { fmtInt, fmtLuck, fmtOdds, fmtPercent } from "../lib/format";
import type { RollResult } from "../lib/types";
import { audio } from "../audio/engine";
import { useGame } from "../store/game";
import type { CosmosRenderer } from "../visual/cosmos";
import "../visual/cutscene.css";

/**
 * Staged reveal cinematic.
 *
 * Every rarity tier gets a longer, louder sequence built from the same phase
 * vocabulary, so players learn to read "how rare was that?" from the show
 * itself before they read the number:
 *
 *   1 Common       flash                         ~0.4s
 *   2 Rare         light + chime                 ~1.4s
 *   3 Epic         particles + shockwave         ~2.4s
 *   4 Legendary    background bloom + rays       ~5s
 *   5 Secret       blackout → crack → reveal     ~9s
 *   6 Ultra Secret + galaxy + celestial body     ~16s
 *   7 ???          the full climax               ~28s
 *   8 Admin        reality itself is rewritten   ~20s
 */

export type Phase =
  | "blackout" | "silence" | "ember" | "crack" | "stars" | "body" | "collapse"
  | "silhouette" | "name" | "odds" | "full" | "reveal" | "done";

interface Plan {
  phases: [Phase, number][]; // phase, duration in ms
  total: number;
}

function planFor(tier: number, firstDiscovery: boolean, isAdmin: boolean): Plan {
  let phases: [Phase, number][];
  if (isAdmin) {
    phases = [["blackout", 700], ["silence", 500], ["crack", 1400], ["collapse", 2200], ["stars", 2400], ["body", 2600],
              ["silhouette", 1800], ["name", 1800], ["odds", 1200], ["full", 2600], ["reveal", 3000]];
  } else if (tier >= 7) {
    phases = [["blackout", 1100], ["silence", 900], ["ember", 2000], ["crack", 1800], ["stars", 3000], ["body", 3800],
              ["collapse", 2200], ["silhouette", 2600], ["name", 2200], ["odds", 1800], ["full", 3200], ["reveal", 3600]];
  } else if (tier >= 6) {
    phases = [["blackout", 700], ["ember", 1100], ["crack", 1200], ["stars", 2000], ["body", 2400],
              ["silhouette", 1600], ["name", 1600], ["odds", 1300], ["reveal", 2600]];
  } else if (tier >= 5) {
    phases = [["blackout", 550], ["ember", 800], ["crack", 1000], ["stars", 1400], ["silhouette", 1100],
              ["name", 1200], ["odds", 1000], ["reveal", 2000]];
  } else if (tier >= 4) {
    phases = [["ember", 500], ["stars", 900], ["name", 900], ["odds", 800], ["reveal", 1900]];
  } else if (tier >= 3) {
    phases = [["ember", 320], ["name", 620], ["reveal", 1500]];
  } else if (tier >= 2) {
    phases = [["name", 420], ["reveal", 1000]];
  } else {
    phases = [["reveal", 420]];
  }
  if (firstDiscovery) phases = [...phases, ["full", 2600]];
  return { phases, total: phases.reduce((a, [, d]) => a + d, 0) };
}

const SFX_BY_TIER = ["reveal_common", "reveal_common", "reveal_rare", "reveal_epic", "reveal_legendary", "reveal_secret", "reveal_ultra", "reveal_mythic", "reveal_admin"] as const;

/** Nebula tints come straight from item palettes, which may be pure white or pure black
 *  (OMEGA is #000000/#ffffff). Either extreme renders the conic swirl as a flat grey
 *  smear, so luminance is pulled back into a band where the hue survives. */
function tint(hex: string, accent: string): string {
  const rgb = (h: string): [number, number, number] | null => {
    const m = /^#?([0-9a-f]{6})$/i.exec(h.trim());
    if (!m) return null;
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  };
  const c = rgb(hex);
  if (!c) return hex;
  const lum = (v: [number, number, number]) => (0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]) / 255;
  let [r, g, b] = c;
  const l = lum(c);
  if (l > 0.78 || l < 0.14) {
    const a = rgb(accent) ?? [106, 44, 255];
    r = r * 0.3 + a[0] * 0.7;
    g = g * 0.3 + a[1] * 0.7;
    b = b * 0.3 + a[2] * 0.7;
    const l2 = lum([r, g, b]);
    if (l2 > 0.7) { const k = 0.7 / l2; r *= k; g *= k; b *= k; }
    if (l2 < 0.12) { r = r * 0.5 + 90; g = g * 0.5 + 60; b = b * 0.5 + 150; }
  }
  const h2 = (x: number) => Math.round(Math.max(0, Math.min(255, x))).toString(16).padStart(2, "0");
  return `#${h2(r)}${h2(g)}${h2(b)}`;
}

function Sparks({ count, color }: { count: number; color: string }) {
  const sparks = useMemo(
    () =>
      Array.from({ length: count }, () => {
        const a = Math.random() * Math.PI * 2;
        const d = 120 + Math.random() * 420;
        return { tx: `${Math.cos(a) * d}px`, ty: `${Math.sin(a) * d}px`, delay: `${Math.random() * 0.9}s`, dur: `${1.4 + Math.random() * 1.8}s` };
      }),
    [count],
  );
  return (
    <>
      {sparks.map((s, i) => (
        <span key={i} className="cut-spark" style={{ ["--tx" as any]: s.tx, ["--ty" as any]: s.ty, ["--delay" as any]: s.delay, ["--d" as any]: s.dur, ["--glow" as any]: color }} />
      ))}
    </>
  );
}

export function RollCutscene({ roll, cosmos, onDone }: { roll: RollResult; cosmos: CosmosRenderer | null; onDone: () => void }) {
  const config = useGame((s) => s.config);
  const me = useGame((s) => s.me);
  const rarity = config?.rarities.find((r) => r.key === roll.item.rarity);
  const tier = roll.item.tier;
  const isAdmin = roll.item.rarity === "admin";
  const first = !!roll.first_discovery;
  const reduced = me?.settings.graphics.reduced_motion ?? false;
  const skipConfirmTier = config?.rarities.find((r) => r.key === me?.settings.roll.skip_confirm_min_tier)?.tier ?? 5;

  const plan = useMemo(() => planFor(reduced ? Math.min(tier, 3) : tier, first, isAdmin), [tier, first, isAdmin, reduced]);
  const [idx, setIdx] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [confirmSkip, setConfirmSkip] = useState(false);
  const startedAt = useRef(performance.now());
  const doneRef = useRef(false);

  const accent = rarity?.color2 ?? rarity?.color ?? "#6a2cff";
  const c1 = tint(roll.item.visual?.colors?.[0] ?? rarity?.color ?? "#ffffff", accent);
  const c2 = tint(roll.item.visual?.colors?.[1] ?? accent, accent);
  const glow = roll.item.visual?.glow ?? rarity?.color ?? c1;

  const finish = useCallback(() => {
    if (doneRef.current) return;
    doneRef.current = true;
    cosmos?.setWarp(0);
    onDone();
  }, [cosmos, onDone]);

  // phase machine
  useEffect(() => {
    if (idx >= plan.phases.length) {
      const t = window.setTimeout(finish, 120);
      return () => window.clearTimeout(t);
    }
    const [phase, dur] = plan.phases[idx];
    // audio & background cues per phase
    if (phase === "blackout") {
      audio.duck(Math.min(12, plan.total / 1000), 0.05);
      cosmos?.pulse("#000000", 0.4);
    } else if (phase === "silence") {
      audio.silence(Math.min(10, plan.total / 1200));
    } else if (phase === "ember") {
      audio.sfx("charge", tier >= 6 ? 1.2 : 0.7);
    } else if (phase === "crack") {
      audio.sfx("shatter", 1);
      cosmos?.shakeScreen(tier >= 6 ? 14 : 7);
      cosmos?.pulse("#ffffff", 0.7);
    } else if (phase === "stars") {
      audio.sfx("whoosh", 1);
      cosmos?.setWarp(Math.min(1, tier / 7));
    } else if (phase === "body") {
      audio.sfx("heartbeat", 1.2);
    } else if (phase === "collapse") {
      audio.sfx(isAdmin ? "glitch" : "impact", 1.2);
      cosmos?.shakeScreen(18);
    } else if (phase === "silhouette") {
      audio.sfx("sparkle", 1);
    } else if (phase === "name") {
      audio.sfx(tier >= 5 ? "impact" : "sparkle", tier >= 5 ? 0.8 : 0.6);
    } else if (phase === "odds") {
      audio.sfx("coin", 1);
    } else if (phase === "full") {
      audio.sfx(first ? "first_discovery" : "sparkle", 1.2);
      cosmos?.pulse(glow, 0.8);
    } else if (phase === "reveal") {
      audio.sfx(SFX_BY_TIER[Math.min(8, tier)], 1);
      cosmos?.pulse(glow, Math.min(1, 0.25 + tier * 0.1));
      if (tier >= 5) cosmos?.shakeScreen(tier * 2);
    }
    const t = window.setTimeout(() => setIdx((i) => i + 1), dur);
    return () => window.clearTimeout(t);
  }, [idx, plan, tier, first, isAdmin, glow, cosmos, finish]);

  // progress bar
  useEffect(() => {
    if (plan.total < 1500) return;
    const id = window.setInterval(() => setElapsed(performance.now() - startedAt.current), 90);
    return () => window.clearInterval(id);
  }, [plan.total]);

  const phase: Phase = idx >= plan.phases.length ? "done" : plan.phases[idx][0];
  const seen = plan.phases.slice(0, idx + 1).map(([p]) => p);
  const has = (p: Phase) => seen.includes(p);
  const showItem = has("silhouette") || has("name") || has("reveal") || plan.phases[0][0] === "reveal" || has("odds");
  const revealed = has("reveal") || (!plan.phases.some(([p]) => p === "silhouette") && has("name"));
  const showName = has("name") || has("reveal");
  const showOdds = has("odds") || has("reveal");
  const bigTier = tier >= 5 || isAdmin;

  const requestSkip = () => {
    if (bigTier && tier >= skipConfirmTier && !confirmSkip && plan.total > 6000) {
      setConfirmSkip(true);
      window.setTimeout(() => setConfirmSkip(false), 2600);
      return;
    }
    audio.sfx("click");
    finish();
  };

  const veilStyle = {
    ["--veil-c" as any]: bigTier ? 0.82 : 0.35,
    ["--veil-e" as any]: bigTier ? 0.98 : 0.7,
    ["--veil-t" as any]: phase === "blackout" ? "0.7s" : "0.3s",
  };

  return (
    <div className={`cut tier-${tier} interactive`} onClick={requestSkip} role="dialog" aria-label={`${roll.item.name} 獲得演出`}>
      <div className="cut-veil" style={veilStyle} />
      {isAdmin && <div className="cut-scanlines" />}
      {isAdmin && has("collapse") && <div className="cut-grid" />}
      {(has("collapse") && (isAdmin || tier >= 7)) && <div className="cut-fracture" />}

      <div className={`cut-stage ${showItem ? "focus" : ""}`} style={{ ["--glow" as any]: glow, ["--c1" as any]: c1, ["--c2" as any]: c2 }}>
        {has("ember") && !reduced && (
          <div style={{
            position: "absolute", width: 6, height: 6, borderRadius: "50%", background: glow,
            boxShadow: `0 0 60px 20px ${glow}`, animation: "pulseGlow 1.4s ease-in-out infinite",
          }} />
        )}
        {has("crack") && (
          <>
            <div className="crack" />
            {tier >= 6 && <div className="crack vertical" style={{ animationDelay: "0.25s" }} />}
          </>
        )}
        {has("stars") && !reduced && <div className="cut-galaxy" />}
        {has("body") && !reduced && <div className="cut-body" />}
        {(has("full") || tier >= 6) && !reduced && <div className="rays" />}
        {has("reveal") && !reduced && <div className="shockwave" />}
        {has("reveal") && tier >= 4 && <div className="halo" style={{ ["--dur" as any]: `${1.4 + tier * 0.25}s` }} />}
        {has("reveal") && tier >= 3 && !reduced && <Sparks count={Math.min(60, tier * 9)} color={glow} />}

        {showItem && (
          <div className="cut-item">
            {first && has("full") && (
              <div className="cut-banner" style={{ color: "var(--gold)" }}>FIRST DISCOVERY<span className="cut-banner-ja">世界初発見</span></div>
            )}
            <div className={`cut-icon-wrap ${!revealed ? "silhouette" : "reveal-flash"}`} style={{ ["--glow" as any]: glow }}>
              <ItemIcon visual={roll.item.visual} tier={tier} size={Math.min(260, Math.max(120, 90 + tier * 22))} animate={!reduced} />
            </div>
            {showName && (
              <>
                <div
                  className={`cut-name r-${roll.item.rarity} ${isAdmin || tier >= 7 ? "glitch-text" : ""}`}
                  data-text={roll.item.name}
                  style={{ color: rarity?.color }}
                >
                  {roll.item.name}
                </div>
                {roll.item.name_ja && roll.item.name_ja !== roll.item.name && (
                  <div className="cut-name-ja">{roll.item.name_ja}</div>
                )}
              </>
            )}
            {showName && (
              <div className="row-wrap" style={{ justifyContent: "center", gap: 8 }}>
                <span className={`badge r-${roll.item.rarity}`}>
                  {rarity?.name ?? roll.item.rarity}
                  {rarity?.name_ja && rarity.name_ja !== rarity.name && <span className="badge-ja">{rarity.name_ja}</span>}
                </span>
                {roll.special && <span className="badge" style={{ color: "var(--accent)" }}>SPECIAL ROLL</span>}
                {roll.hidden_special && <span className="badge" style={{ color: "var(--gold)" }}>{roll.hidden_special.name}</span>}
                {roll.duplicated > 0 && <span className="badge" style={{ color: "var(--good)" }}>×{1 + roll.duplicated} 複製</span>}
                {roll.forced && <span className="badge" style={{ color: "var(--r-admin)" }}>FATE REWRITTEN</span>}
              </div>
            )}
            {showOdds && (
              <div className="cut-odds">{fmtOdds(roll.odds, roll.item.display_odds)}</div>
            )}
            {revealed && (
              <>
                <div className="cut-meta">
                  <span className="chip">Luck {fmtLuck(roll.luck.final)}</span>
                  <span className="chip">実質 {fmtOdds(roll.final_odds)}</span>
                  <span className="chip"><Name en={roll.biome.name} ja={roll.biome.name_ja} /></span>
                  <span className="chip" style={{ color: "var(--gold)" }}>
                    <Name en={roll.fortune.label} ja={roll.fortune.label_ja} />
                  </span>
                  {roll.item.serial && <span className="chip">世界で {fmtInt(roll.item.serial)} 個目</span>}
                  {roll.new_collection && <span className="chip" style={{ color: "var(--good)" }}>NEW</span>}
                </div>
                {first && (
                  <div className="cut-sub">
                    <strong style={{ color: "var(--gold)" }}>{roll.first_discovery?.player}</strong> が世界で初めて発見しました
                    <div className="tiny faint">上位 {fmtPercent(roll.fortune.top_percent)} の幸運</div>
                  </div>
                )}
                {roll.item.lore && tier >= 5 && <div className="cut-sub" style={{ fontStyle: "italic", opacity: 0.8 }}>「{roll.item.lore}」</div>}
              </>
            )}
          </div>
        )}
      </div>

      {plan.total > 1200 && (
        <button className="cut-skip" onClick={(e) => { e.stopPropagation(); requestSkip(); }}>
          {confirmSkip ? "もう一度タップでスキップ" : "スキップ ▸"}
        </button>
      )}
      {plan.total > 1500 && (
        <div className="cut-progress"><i style={{ width: `${Math.min(100, (elapsed / plan.total) * 100)}%`, ["--glow" as any]: glow }} /></div>
      )}
    </div>
  );
}

/** Admin Artifact activation — "the world itself is being rewritten". */
export function ArtifactCutscene({
  cinematic, cosmos, onDone, global: isGlobal,
}: { cinematic: { artifact: string; name: string; theme: string; tier: number; visual: any; user?: { name: string } }; cosmos: CosmosRenderer | null; onDone: () => void; global?: boolean }) {
  const reduced = useGame((s) => s.me?.settings.graphics.reduced_motion ?? false);
  const tier = Math.max(1, Math.min(5, cinematic.tier || 3));
  const duration = reduced ? 1400 : [0, 3200, 5200, 8200, 12000, 18000][tier];
  const [stage, setStage] = useState(0);
  const colors = cinematic.visual?.colors ?? ["#ffe9a8", "#ff3cac", "#ffffff"];
  const glow = cinematic.visual?.glow ?? colors[0];
  const doneRef = useRef(false);

  const finish = useCallback(() => {
    if (doneRef.current) return;
    doneRef.current = true;
    cosmos?.setWarp(0);
    onDone();
  }, [cosmos, onDone]);

  useEffect(() => {
    audio.duck(duration / 1000, 0.06);
    audio.sfx(cinematic.theme === "void" ? "void" : cinematic.theme === "time" ? "time" : cinematic.theme === "system" ? "glitch" : "charge", 1.2);
    cosmos?.setWarp(tier / 5);
    const t1 = window.setTimeout(() => {
      setStage(1);
      audio.sfx("reveal_admin", 1);
      cosmos?.shakeScreen(10 + tier * 4);
      cosmos?.pulse(glow, 0.9);
    }, duration * 0.42);
    const t2 = window.setTimeout(() => setStage(2), duration * 0.72);
    const t3 = window.setTimeout(finish, duration);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      window.clearTimeout(t3);
    };
  }, [duration, tier, glow, cinematic.theme, cosmos, finish]);

  return (
    <div className="cut tier-8 interactive" onClick={finish} role="dialog" aria-label={`${cinematic.name} 発動`}>
      <div className="cut-veil" style={{ ["--veil-c" as any]: 0.86, ["--veil-e" as any]: 0.99 }} />
      <div className="cut-scanlines" />
      {!reduced && stage >= 1 && <div className="cut-grid" />}
      {!reduced && stage >= 1 && <div className="cut-fracture" />}
      <div className="cut-stage" style={{ ["--glow" as any]: glow, ["--c1" as any]: tint(colors[0], colors[1] ?? "#ff3cac"), ["--c2" as any]: tint(colors[1] ?? "#ff3cac", colors[0]) }}>
        {!reduced && <div className="rays" />}
        {!reduced && stage >= 1 && <div className="cut-galaxy" />}
        {stage >= 1 && <div className="halo" style={{ ["--dur" as any]: "2.6s" }} />}
        {!reduced && stage >= 1 && <Sparks count={48} color={glow} />}
        <div className="cut-item">
          <div className="cut-banner" style={{ color: glow }}>{isGlobal ? "WORLD EVENT" : "ADMIN ARTIFACT"}</div>
          <div className="cut-icon-wrap" style={{ ["--glow" as any]: glow }}>
            <ItemIcon visual={cinematic.visual} tier={8} size={220} animate={!reduced} />
          </div>
          <div className="cut-name glitch-text" data-text={cinematic.name} style={{ color: glow }}>{cinematic.name}</div>
          {stage >= 2 && (
            <>
              <div className="cut-meta">
                <span className="chip" style={{ color: glow }}>{cinematic.theme.toUpperCase()}</span>
                {cinematic.user && <span className="chip">{cinematic.user.name}</span>}
              </div>
              <div className="cut-sub">世界の理が書き換えられた。</div>
            </>
          )}
        </div>
      </div>
      <button className="cut-skip" onClick={(e) => { e.stopPropagation(); finish(); }}>スキップ ▸</button>
    </div>
  );
}
