import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, get, post } from "../lib/api";
import { events, serverNow, useGame } from "../store/game";
import { audio } from "../audio/engine";
import { getCosmos } from "../App";
import { ItemIcon } from "../components/ItemIcon";
import { Countdown, Empty, Modal, UserChip, Name } from "../components/ui";
import { fmtCompact, fmtInt, fmtLuck, fmtOdds, fmtPercent, timeAgo } from "../lib/format";
import type { ActiveEffect, FeedEvent, HudState, RollResponse, RollResult } from "../lib/types";
import "./roll.css";

const SPEED_LABEL: Record<string, string> = { normal: "標準", fast: "高速", ultra: "最速" };

function useCooldown(hud: HudState | null) {
  const [remaining, setRemaining] = useState(0);
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const target = hud?.next_roll_at ? new Date(hud.next_roll_at).getTime() : 0;
      setRemaining(Math.max(0, target - serverNow()));
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [hud?.next_roll_at]);
  return remaining;
}

function EffectChip({ e }: { e: ActiveEffect }) {
  const [, force] = useState(0);
  useEffect(() => {
    if (!e.expires_at) return;
    const id = window.setInterval(() => force((n) => n + 1), 1000);
    return () => window.clearInterval(id);
  }, [e.expires_at]);
  const secs = e.expires_at ? Math.max(0, Math.round((new Date(e.expires_at).getTime() - serverNow()) / 1000)) : null;
  const label = e.effect_type === "luck" ? `+${fmtCompact(e.value)}%`
    : e.effect_type === "luck_mult" ? `×${fmtCompact(e.value)}`
    : e.effect_type === "min_rarity" ? `最低 ${String(e.params.tier ?? "")}`
    : e.effect_type === "roll_speed" ? `速度+${e.value}%`
    : e.effect_type === "cooldown_mult" ? `間隔×${e.value}`
    : e.effect_type === "biome_chance" ? `Biome×${fmtCompact(e.value)}`
    : e.effect_type === "table_flatten" ? "RNG干渉"
    : e.effect_type === "best_of" ? `${e.value}回抽選`
    : e.effect_type === "compounding_luck" ? `累積+${Math.round(e.value * 100)}%/roll`
    : e.effect_type;
  return (
    <span className={`chip effect-chip ${e.source_type === "artifact" ? "artifact" : e.source_type === "admin" ? "admin" : ""}`}>
      <strong>{e.name}</strong>
      <span className="mono tiny">{label}</span>
      {e.remaining_rolls !== null && <span className="tiny" style={{ color: "var(--gold)" }}>残り{e.remaining_rolls}回</span>}
      {secs !== null && <span className="tiny faint">{secs > 60 ? `${Math.ceil(secs / 60)}分` : `${secs}秒`}</span>}
      {e.biome_keys.length > 0 && <span className="tiny faint">{e.biome_keys.length}Biome限定</span>}
    </span>
  );
}

function BiomeCard() {
  const hud = useGame((s) => s.hud);
  const refresh = useGame((s) => s.refreshHud);
  const biome = hud?.biome;
  if (!biome) return null;
  const theme = biome.theme ?? {};
  return (
    <div className="glass pad biome-card" style={{ borderColor: theme.accent ? `${theme.accent}55` : undefined }}>
      <div className="row">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="tiny faint" style={{ letterSpacing: "0.16em" }}>現在のBiome</div>
          <div className="row" style={{ gap: 8 }}>
            <h2 style={{ color: theme.accent ?? "var(--accent)", textShadow: `0 0 20px ${theme.accent ?? "#8ab4ff"}66` }}><Name en={biome.name} ja={biome.name_ja} /></h2>
            {biome.kind === "admin" && <span className="badge r-admin">ADMIN</span>}
            {biome.forced && <span className="badge" style={{ color: "var(--gold)" }}>FORCED</span>}
          </div>
          <div className="muted small ellipsis">{biome.description}</div>
        </div>
        <div className="center">
          <div className="stat">
            <span className="k">Biome補正</span>
            <span className="v" style={{ color: biome.luck_mult > 1 ? "var(--good)" : undefined }}>×{biome.luck_mult.toFixed(2)}</span>
          </div>
        </div>
      </div>
      {biome.state && (
        <div className="state-banner" style={{ borderColor: `${theme.accent2 ?? "#fff"}66` }}>
          <span className="badge" style={{ color: theme.accent2 ?? "var(--gold)" }}>{biome.state.name}</span>
          <span className="small">{biome.state.description}</span>
          <span className="spacer" />
          <Countdown to={biome.state.ends_at} onDone={refresh} />
        </div>
      )}
      {biome.ends_at && (
        <div className="row tiny muted">
          <span>残り</span>
          <Countdown to={biome.ends_at} onDone={refresh} />
          {biome.locked_until && <span style={{ color: "var(--warn)" }}>🔒 固定中</span>}
        </div>
      )}
      {biome.reveal && (
        <div className="reveal-box tiny mono">
          <span style={{ color: "var(--r-admin)" }}>COSMIC EYE</span>
          <span>次の変化: {biome.reveal.next_biome ?? "—"} @ <Countdown to={biome.reveal.next_change_at} /></span>
          {biome.reveal.next_state && <span>特殊状態: {biome.reveal.next_state} @ <Countdown to={biome.reveal.next_state_at} /></span>}
        </div>
      )}
    </div>
  );
}

function ResultStrip({ roll }: { roll: RollResult }) {
  return (
    <div className={`result-strip t${roll.item.tier}`}>
      <ItemIcon visual={roll.item.visual} tier={roll.item.tier} size={44} />
      <div style={{ flex: 1, minWidth: 0 }}>
        <div className="row" style={{ gap: 6 }}>
          <span className={`r-${roll.item.rarity}`} style={{ fontWeight: 700 }}><Name en={roll.item.name} ja={roll.item.name_ja} /></span>
          {roll.new_collection && <span className="badge" style={{ color: "var(--good)" }}>NEW</span>}
          {roll.special && <span className="badge" style={{ color: "var(--accent)" }}>SPECIAL</span>}
          {roll.auto_deleted && <span className="badge" style={{ color: "var(--text-faint)" }}>{roll.auto_delete_mode === "sell" ? "自動売却" : "自動削除"}</span>}
          {roll.overflow && <span className="badge" style={{ color: "var(--warn)" }}>容量超過</span>}
        </div>
        <div className="tiny muted mono">
          {fmtOdds(roll.odds, roll.item.display_odds)} · 実質 {fmtOdds(roll.final_odds)} · Luck {fmtLuck(roll.luck.final)}
          {roll.auto_sold > 0 && ` · +✦${fmtInt(roll.auto_sold)}`}
        </div>
      </div>
      <span className="tiny" style={{ color: "var(--gold)" }}>{roll.fortune.label_ja || roll.fortune.label}</span>
    </div>
  );
}

export function RollPage() {
  const hud = useGame((s) => s.hud);
  const me = useGame((s) => s.me);
  const feed = useGame((s) => s.feed);
  const setHud = useGame((s) => s.setHud);
  const enqueue = useGame((s) => s.enqueueReveal);
  const toast = useGame((s) => s.toast);
  const remaining = useCooldown(hud);
  const [history, setHistory] = useState<RollResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [auto, setAuto] = useState(false);
  const [showTable, setShowTable] = useState(false);
  const [table, setTable] = useState<any>(null);
  const [lastError, setLastError] = useState<string | null>(null);
  const rollingRef = useRef(false);
  const autoRef = useRef(false);
  const settings = me?.settings;

  const cutsceneTier = useMemo(() => {
    const key = settings?.roll.full_cutscene_min_tier ?? "legendary";
    return useGame.getState().config?.rarities.find((r) => r.key === key)?.tier ?? 4;
  }, [settings?.roll.full_cutscene_min_tier]);

  const skipThreshold = settings?.auto_skip.enabled ? settings.auto_skip.threshold : 0;

  const doRoll = useCallback(async () => {
    if (rollingRef.current) return;
    rollingRef.current = true;
    setBusy(true);
    setLastError(null);
    audio.sfx("roll_start", 0.8);
    getCosmos()?.pulse(hud?.biome?.theme?.accent ?? "#8ab4ff", 0.16);
    // Hold notifications from here, not from when the cutscene mounts: the
    // socket can announce this very roll before its response has arrived.
    useGame.getState().setToastHold(true);
    try {
      const res = await post<RollResponse>("/api/roll", { auto: autoRef.current });
      setHud(res.state);
      const roll = res.roll;
      setHistory((h) => [roll, ...h].slice(0, 40));

      // Queue the cutscene before anything that raises a toast: the store holds
      // notifications back while a reveal is on screen, and it can only do that
      // if it already knows one is coming.
      const skippedByFilter = skipThreshold > 0 && roll.odds < skipThreshold && roll.item.tier < cutsceneTier;
      const cutscenesOn = settings?.roll.cutscenes !== false;
      const forceFull = roll.item.tier >= cutsceneTier || !!roll.first_discovery || roll.item.rarity === "admin";
      if (cutscenesOn && (forceFull || (!skippedByFilter && settings?.roll.speed === "normal"))) {
        enqueue({ type: "roll", roll, full: forceFull });
      } else {
        audio.sfx(roll.item.tier >= 3 ? "reveal_epic" : roll.item.tier >= 2 ? "reveal_rare" : "reveal_common", 0.7);
        if (roll.item.tier >= 4) getCosmos()?.pulse(roll.item.visual?.glow ?? "#fff", 0.4);
      }
      useGame.getState().setToastHold(false);

      useGame.getState().announceAchievements(roll.progress.achievements);
      if (roll.progress.level_up) {
        audio.sfx("level_up");
        toast(`LEVEL UP — Lv.${roll.progress.level_up.to}`, "success",
          roll.progress.unlocked.length ? `解放: ${roll.progress.unlocked.join(", ")}` : undefined);
      }
      for (const q of roll.progress.quests_completed) toast(`クエスト達成: ${q.name}`, "success", "報酬を受け取れます");
      if (res.offline) enqueue({ type: "offline", summary: res.offline });
    } catch (e) {
      const err = e as ApiError;
      if (err.code === "cooldown") {
        // clock drift: trust the server and re-sync
        useGame.getState().refreshHud();
      } else {
        setLastError(err.message);
        audio.sfx("error");
        if (err.code !== "network") toast(err.message, "error");
        autoRef.current = false;
        setAuto(false);
      }
    } finally {
      // A thrown roll never reaches the release above, and a permanent hold
      // would silence the game.
      useGame.getState().setToastHold(false);
      rollingRef.current = false;
      setBusy(false);
    }
  }, [cutsceneTier, enqueue, hud?.biome?.theme?.accent, setHud, settings?.roll.cutscenes, settings?.roll.speed, skipThreshold, toast]);

  // auto roll loop
  useEffect(() => {
    autoRef.current = auto;
    if (!auto) return;
    let stop = false;
    const loop = async () => {
      while (!stop && autoRef.current) {
        const target = useGame.getState().hud?.next_roll_at;
        const wait = target ? new Date(target).getTime() - serverNow() : 0;
        if (wait > 0) await new Promise((r) => setTimeout(r, Math.min(wait + 25, 1500)));
        if (stop || !autoRef.current) break;
        if (useGame.getState().reveals.length > 2) {
          await new Promise((r) => setTimeout(r, 500));
          continue;
        }
        await doRoll();
      }
    };
    loop();
    return () => {
      stop = true;
    };
  }, [auto, doRoll]);

  // keyboard: space to roll
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement;
      if (el && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return;
      if (useGame.getState().reveals.length) return;
      if (e.code === "Space" || e.code === "Enter") {
        e.preventDefault();
        if (remaining <= 0 && !busy) doRoll();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [doRoll, remaining, busy]);

  useEffect(() => {
    const off = events.on("state_changed", () => useGame.getState().refreshHud());
    return off;
  }, []);

  const toggleAutoServer = async (on: boolean) => {
    try {
      await post("/api/roll/auto", { enabled: on });
      useGame.getState().refreshHud();
      toast(on ? "Auto Rollを有効にしました" : "Auto Rollを無効にしました", "info",
        on ? "ブラウザを閉じている間もオフラインRollが進行します" : undefined);
    } catch (e) {
      toast((e as ApiError).message, "error");
    }
  };

  const openTable = async () => {
    setShowTable(true);
    try {
      setTable(await get("/api/roll/table"));
    } catch (e) {
      setTable({ error: (e as ApiError).message });
    }
  };

  if (!hud) return null;
  const cdTotal = Math.max(0.05, hud.cooldown) * 1000;
  const progress = remaining > 0 ? 1 - remaining / cdTotal : 1;
  const ready = remaining <= 0;
  const luck = hud.luck;
  const xpSpan = Math.max(1, hud.xp_next - hud.xp_level);
  const xpInto = Math.max(0, hud.xp - hud.xp_level);

  return (
    <div className="page roll-page">
      <div className="roll-layout">
        <div className="col" style={{ gap: 12 }}>
          <BiomeCard />

          <div className="glass pad roll-main">
            <div className="luck-display">
              <div className="tiny faint" style={{ letterSpacing: "0.2em" }}>最終Luck</div>
              <div className="luck-value">{fmtLuck(luck.final)}</div>
              <div className="luck-parts">
                {[
                  ["Base", luck.base], ["装備", luck.equipment], ["Biome", luck.biome],
                  ["Boost", luck.temporary], ["Special", hud.next_special ? luck.special : 1],
                  ["Event", luck.event], ["Artifact", luck.other],
                ].filter(([, v]) => Math.abs((v as number) - 1) > 1e-9 || v === luck.base).map(([k, v]) => (
                  <span key={k as string} className="chip tiny mono">{k} ×{fmtCompact(v as number)}</span>
                ))}
              </div>
            </div>

            <button
              className={`roll-button ${ready ? "ready" : ""} ${hud.next_special ? "special" : ""}`}
              onClick={doRoll}
              disabled={!ready || busy}
              style={{ ["--accent-c" as any]: hud.biome?.theme?.accent ?? "#8ab4ff", ["--accent-c2" as any]: hud.biome?.theme?.accent2 ?? "#b58cff" }}
              aria-label="Rollする"
            >
              <svg className="roll-ring" viewBox="0 0 120 120" aria-hidden="true">
                <circle cx="60" cy="60" r="54" className="ring-bg" />
                <circle cx="60" cy="60" r="54" className="ring-fg" style={{ strokeDashoffset: 339.3 * (1 - progress) }} />
              </svg>
              <span className="roll-label">
                {busy ? "…" : ready ? "ROLL" : (remaining / 1000).toFixed(remaining < 3000 ? 2 : 1)}
              </span>
              {hud.next_special && ready && <span className="special-tag">SPECIAL</span>}
            </button>

            <div className="row-wrap center" style={{ justifyContent: "center", gap: 8 }}>
              <span className="chip tiny">#{fmtInt(hud.roll_counter + 1)}</span>
              <span className="chip tiny">{hud.roll_speed.toFixed(1)} 回/秒</span>
              <span className="chip tiny">Special まで {hud.special_in}</span>
              <span className="chip tiny">{SPEED_LABEL[settings?.roll.speed ?? "normal"]}</span>
            </div>

            <div className="row-wrap" style={{ justifyContent: "center", gap: 8 }}>
              <button className={`btn sm ${auto ? "active" : "ghost"}`} onClick={() => { audio.sfx("click"); setAuto((a) => !a); }}>
                {auto ? "■ Auto停止" : "▶ Auto Roll"}
              </button>
              <label className="switch" title="オフラインでもRollを進める">
                <input type="checkbox" checked={hud.auto_roll} onChange={(e) => toggleAutoServer(e.target.checked)} />
                <span className="track" />
                <span className="tiny">オフラインRoll</span>
              </label>
              {(hud.unlocks.includes("rng_analyzer") || hud.reveal_rng) && (
                <button className="btn sm ghost" onClick={openTable}>RNG解析</button>
              )}
            </div>

            {lastError && <div className="center small" style={{ color: "var(--bad)" }}>{lastError}</div>}

            <div className="xp-row">
              <div className="row tiny muted">
                <span>Lv.{hud.level}</span>
                <span className="spacer" />
                <span className="mono">{fmtInt(xpInto)} / {fmtInt(xpSpan)} XP</span>
              </div>
              <div className="bar"><i style={{ width: `${Math.min(100, (xpInto / xpSpan) * 100)}%` }} /></div>
            </div>
          </div>

          {hud.effects.length > 0 && (
            <div className="glass pad">
              <div className="tiny faint" style={{ letterSpacing: "0.16em", marginBottom: 6 }}>ACTIVE BOOSTS</div>
              <div className="row-wrap">{hud.effects.map((e) => <EffectChip key={e.id} e={e} />)}</div>
            </div>
          )}

          <div className="glass pad">
            <div className="row" style={{ marginBottom: 8 }}>
              <div className="tiny faint" style={{ letterSpacing: "0.16em", flex: 1 }}>装備</div>
              <Link className="btn xs ghost" to="/equipment">装備変更</Link>
            </div>
            {hud.equipment.length === 0 ? (
              <div className="muted small">装備なし{hud.level < 3 ? "（Lv.3で解放）" : ""}</div>
            ) : (
              <div className="row-wrap">
                {hud.equipment.map((e) => (
                  <span key={e.slot} className={`chip equip-chip r-${e.rarity}`} style={e.aura ? { boxShadow: `0 0 16px currentColor` } : undefined}>
                    <ItemIcon visual={e.visual} size={20} tier={e.rarity === "admin" ? 8 : 3} animate={false} />
                    <span className="ellipsis" style={{ maxWidth: 140 }}>{e.name}</span>
                    {e.quality_tier === "god" && <span className="tiny" style={{ color: "var(--gold)" }}>GOD</span>}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="col" style={{ gap: 12 }}>
          <div className="glass pad">
            <div className="tiny faint" style={{ letterSpacing: "0.16em", marginBottom: 8 }}>直近の抽選</div>
            {history.length === 0 ? (
              <Empty icon="✦">Rollして宇宙を観測しましょう</Empty>
            ) : (
              <div className="col" style={{ gap: 6, maxHeight: 420, overflow: "auto" }}>
                {history.map((r, i) => <ResultStrip key={`${r.id}-${i}`} roll={r} />)}
              </div>
            )}
          </div>

          <div className="glass pad">
            <div className="row" style={{ marginBottom: 8 }}>
              <div className="tiny faint" style={{ letterSpacing: "0.16em", flex: 1 }}>世界の動き</div>
              <span className="tiny faint">{feed.length}</span>
            </div>
            {feed.length === 0 ? (
              <div className="muted small">まだ動きがありません</div>
            ) : (
              <div className="col" style={{ gap: 5, maxHeight: 360, overflow: "auto" }}>
                {feed.slice(0, 40).map((ev) => <FeedRow key={ev.id} ev={ev} />)}
              </div>
            )}
          </div>
        </div>
      </div>

      <Modal open={showTable} onClose={() => setShowTable(false)} title="RNG ANALYZER — 現在の最終確率" wide>
        {!table ? <div className="muted">計算中…</div> : table.error ? <div style={{ color: "var(--bad)" }}>{table.error}</div> : (
          <>
            <div className="row-wrap" style={{ marginBottom: 10 }}>
              <span className="chip">Luck {fmtLuck(table.luck.final)}</span>
              <span className="chip">{table.biome}</span>
              {table.special && <span className="chip" style={{ color: "var(--accent)" }}>SPECIAL</span>}
              <span className="chip">{table.count} items</span>
            </div>
            <div className="table-wrap" style={{ maxHeight: "56vh" }}>
              <table className="table">
                <thead><tr><th>アイテム</th><th>レア度</th><th>基礎</th><th>現在の確率</th></tr></thead>
                <tbody>
                  {table.items.map((it: any) => (
                    <tr key={it.key}>
                      <td><Name en={it.name} ja={(it as any).name_ja} /></td>
                      <td><span className={`r-${it.rarity}`}>{it.rarity}</span></td>
                      <td className="mono tiny">{fmtOdds(it.odds)}</td>
                      <td className="mono">{fmtOdds(1 / it.p)} <span className="faint tiny">({fmtPercent(it.p * 100)})</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Modal>
    </div>
  );
}

function FeedRow({ ev }: { ev: FeedEvent }) {
  const p = ev.payload ?? {};
  const item = p.item;
  if (ev.type === "first_discovery") {
    return (
      <div className="feed-row first">
        <span className="badge" style={{ color: "var(--gold)" }}>世界初</span>
        {item && <ItemIcon visual={item.visual} tier={item.tier} size={20} animate={false} />}
        <span className="ellipsis" style={{ flex: 1 }}>
          <span className={`r-${item?.rarity}`}>{item?.name}</span> — {ev.user ? <UserChip user={ev.user} size={16} /> : <span className="faint">匿名</span>}
        </span>
        <span className="tiny faint">{timeAgo(ev.created_at)}</span>
      </div>
    );
  }
  if (ev.type === "rare_drop") {
    return (
      <div className="feed-row">
        {item && <ItemIcon visual={item.visual} tier={item.tier} size={20} animate={false} />}
        <span className="ellipsis" style={{ flex: 1 }}>
          <span className={`r-${item?.rarity}`}>{item?.name}</span>
          <span className="faint tiny mono"> {fmtOdds(p.odds)}</span>
          {p.count > 1 && <span className="tiny"> ×{p.count}</span>}
        </span>
        {ev.user ? <UserChip user={ev.user} size={16} /> : <span className="faint tiny">匿名</span>}
        <span className="tiny faint">{timeAgo(ev.created_at)}</span>
      </div>
    );
  }
  if (ev.type === "rare_biome") {
    return (
      <div className="feed-row">
        <span className="badge" style={{ color: "var(--accent2)" }}>BIOME</span>
        <span className="ellipsis" style={{ flex: 1 }}>{p.name}</span>
        {ev.user && <UserChip user={ev.user} size={16} />}
        <span className="tiny faint">{timeAgo(ev.created_at)}</span>
      </div>
    );
  }
  if (ev.type === "world_first_achievement") {
    return (
      <div className="feed-row first">
        <span className="badge" style={{ color: "var(--good)" }}>実績</span>
        <span className="ellipsis" style={{ flex: 1 }}>{p.name}</span>
        {ev.user && <UserChip user={ev.user} size={16} />}
      </div>
    );
  }
  if (ev.type === "admin_artifact" || ev.type === "admin_broadcast") {
    return (
      <div className="feed-row" style={{ borderColor: "rgba(255,210,120,0.4)" }}>
        <span className="badge r-admin">{ev.type === "admin_broadcast" ? "NOTICE" : "ARTIFACT"}</span>
        <span className="ellipsis" style={{ flex: 1 }}>{p.title ?? p.name} {p.body ? `— ${p.body}` : ""}</span>
        <span className="tiny faint">{timeAgo(ev.created_at)}</span>
      </div>
    );
  }
  return null;
}
