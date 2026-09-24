import { useMemo, useState } from "react";
import { post } from "../lib/api";
import { useAction, useApi } from "../lib/useApi";
import { useGame } from "../store/game";
import { audio } from "../audio/engine";
import { ItemIcon } from "../components/ItemIcon";
import { Empty, ErrorBox, LockedFeature, Modal, RarityBadge, Spinner, Tabs, useConfirm } from "../components/ui";
import { fmtInt } from "../lib/format";

interface Equip {
  id: number; key: string; name: string; description: string; slot: string; rarity: any; visual: any;
  passives: any[]; base_luck: number; base_speed: number; luck_bonus: number; speed_bonus: number;
  quality: number; quality_tier: string; quality_name: string; equipped_slot: string | null; locked: boolean;
  source: string; obtained_at: string; sell_value: number;
}

interface Artifact {
  instance_id: number; state: string; serial: number | null; obtained_at: string; meta: any;
  artifact: { key: string; name: string; ability: string; theme: string; tier: number; visual: any; player_usable: boolean; cooldown_sec: number; duration_sec: number | null; equip_passive: any; target: string };
  usable: boolean; uses_remaining: number | null; ready_at: string | null;
}

const SLOT_LABEL: Record<string, string> = { gauntlet: "手甲", core: "コア", relic: "遺物", artifact: "管理者遺物" };
const QUALITY_COLOR: Record<string, string> = { normal: "var(--text-dim)", fine: "var(--r-rare)", superior: "var(--r-epic)", perfect: "var(--r-legendary)", god: "var(--gold)" };

function PassiveText({ p }: { p: any }) {
  const t = p.type;
  const text =
    t === "biome_luck" ? `${p.biome} 中 Luck ×${p.mult}` :
    t === "nth_roll_luck" ? `${p.every}回ごとに Luck ×${p.mult}` :
    t === "state_luck" ? `特殊状態中 Luck ×${p.mult}` :
    t === "night_luck" ? `${p.hours?.[0]}時〜${p.hours?.[1]}時 Luck ×${p.mult}` :
    t === "luck_mult" ? `常時 Luck ×${p.mult}` :
    t === "biome_chance" ? `Biome出現率 ×${p.mult}` :
    t === "special_interval" ? `Special Roll間隔 ${p.delta}` :
    t === "special_bonus" ? `Special Roll強化 +${p.add}` :
    t === "sell_bonus" ? `売却価格 +${Math.round((p.add ?? 0) * 100)}%` :
    t === "xp_bonus" ? `獲得XP +${Math.round((p.add ?? 0) * 100)}%` :
    t === "offline_efficiency" ? `オフライン効率 +${Math.round((p.add ?? 0) * 100)}%` : t;
  return <span className="chip tiny" style={{ color: "var(--good)" }}>{text}</span>;
}

export function Equipment() {
  const [tab, setTab] = useState<"equipment" | "artifacts">("equipment");
  const { data, loading, error, locked, reload } = useApi<{ items: Equip[]; equipped: any[]; slots: string[]; relic_unlocked: boolean }>("/api/equipment");
  const { data: arts, reload: reloadArts } = useApi<{ artifacts: Artifact[] }>("/api/artifacts");
  const [sel, setSel] = useState<Equip | null>(null);
  const { run, busy } = useAction();
  const { confirm, node } = useConfirm();
  const refreshHud = useGame((s) => s.refreshHud);
  const setStardust = useGame((s) => s.setStardust);
  const enqueue = useGame((s) => s.enqueueReveal);
  const toast = useGame((s) => s.toast);

  const bySlot = useMemo(() => {
    const m: Record<string, Equip[]> = { gauntlet: [], core: [], relic: [] };
    for (const e of data?.items ?? []) (m[e.slot] ??= []).push(e);
    for (const k of Object.keys(m)) m[k].sort((a, b) => Number(!!b.equipped_slot) - Number(!!a.equipped_slot) || b.luck_bonus + b.speed_bonus - (a.luck_bonus + a.speed_bonus));
    return m;
  }, [data]);

  if (locked !== null) return <LockedFeature feature="equipment" />;

  const equip = async (e: Equip) => {
    const ok = await run(() => post("/api/equipment/equip", { instance_id: e.id }));
    if (ok) {
      audio.sfx("equip");
      reload();
      refreshHud();
      setSel(null);
    }
  };
  const unequip = async (slot: string) => {
    await run(() => post("/api/equipment/unequip", { slot }));
    reload();
    reloadArts();
    refreshHud();
    setSel(null);
  };
  const sell = async (e: Equip) => {
    const ok = await confirm("装備を売却", `${e.name}（${e.quality_name}）を ✦${fmtInt(e.sell_value)} で売却します。`, { danger: true });
    if (!ok) return;
    const res = await run(() => post<{ earned: number; stardust: number }>("/api/equipment/sell", { instance_ids: [e.id] }, true));
    if (res) {
      audio.sfx("coin");
      setStardust(res.stardust);
      reload();
      setSel(null);
    }
  };
  const toggleLock = async (e: Equip) => {
    await run(() => post("/api/equipment/lock", { instance_id: e.id, locked: !e.locked }));
    reload();
    setSel((s) => (s ? { ...s, locked: !s.locked } : s));
  };

  const useArtifact = async (a: Artifact) => {
    const needsTarget = a.artifact.target === "user";
    let params: any = {};
    if (needsTarget) {
      const id = window.prompt("対象プレイヤーのIDを入力してください");
      if (!id) return;
      params.target_user_id = Number(id);
    }
    const res = await run(() => post<any>("/api/artifacts/use", { instance_id: a.instance_id, params }, true));
    if (res) {
      enqueue({ type: "artifact", cinematic: res.cinematic, result: res.result });
      reloadArts();
      refreshHud();
      if (res.result?.burst) {
        toast(`${fmtInt(res.result.burst.rolls)} 回のBurst Rollを実行`, "cosmic");
        enqueue({ type: "offline", summary: res.result.burst });
      }
    }
  };

  const equipArtifact = async (a: Artifact | null) => {
    await run(() => post("/api/artifacts/equip", { instance_id: a?.instance_id ?? null }));
    audio.sfx("equip");
    reloadArts();
    refreshHud();
  };

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>装備<span className="h1-en">Equipment</span></h1>
          <div className="sub">Luck・Roll速度・パッシブ効果。品質（GOD ROLLまで）で性能が変わります。</div>
        </div>
      </div>

      <Tabs tabs={[{ key: "equipment", label: `装備 (${data?.items.length ?? 0})` }, { key: "artifacts", label: `管理者遺物 (${arts?.artifacts.length ?? 0})` }]} value={tab} onChange={setTab} />

      {tab === "equipment" ? (
        <>
          <div className="grid grid-2" style={{ margin: "12px 0" }}>
            {["gauntlet", "core", "relic"].map((slot) => {
              const eq = data?.items.find((e) => e.equipped_slot === slot);
              const slotLocked = slot === "relic" && !data?.relic_unlocked;
              return (
                <div key={slot} className="glass pad" style={{ borderColor: eq ? "rgba(138,180,255,0.4)" : undefined }}>
                  <div className="row" style={{ marginBottom: 8 }}>
                    <span className="tiny faint" style={{ letterSpacing: "0.14em", flex: 1 }}>{SLOT_LABEL[slot].toUpperCase()}</span>
                    {eq && <button className="btn xs ghost" onClick={() => unequip(slot)} disabled={busy}>外す</button>}
                  </div>
                  {slotLocked ? <div className="muted small">🔒 上位レベルで解放</div> : eq ? (
                    <div className="row" style={{ gap: 10 }}>
                      <ItemIcon visual={eq.visual} tier={4} size={44} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div className={`r-${eq.rarity}`} style={{ fontWeight: 600 }}>{eq.name}</div>
                        <div className="tiny mono">
                          {eq.luck_bonus > 0 && `Luck +${(eq.luck_bonus * 100).toFixed(0)}% `}
                          {eq.speed_bonus > 0 && `速度 +${(eq.speed_bonus * 100).toFixed(0)}%`}
                        </div>
                        <span className="tiny" style={{ color: QUALITY_COLOR[eq.quality_tier] }}>{eq.quality_name} ({eq.quality.toFixed(2)}×)</span>
                      </div>
                    </div>
                  ) : <div className="muted small">未装備</div>}
                </div>
              );
            })}
          </div>

          <ErrorBox error={error} onRetry={reload} />
          {loading && !data ? <Spinner /> : !data?.items.length ? (
            <Empty icon="⚙">装備がありません。Craftで作成しましょう。</Empty>
          ) : (
            ["gauntlet", "core", "relic"].map((slot) => bySlot[slot]?.length ? (
              <div key={slot} style={{ marginBottom: 16 }}>
                <h3 style={{ marginBottom: 8 }}>{SLOT_LABEL[slot]}</h3>
                <div className="grid grid-auto">
                  {bySlot[slot].map((e) => (
                    <button key={e.id} className="item-card" onClick={() => setSel(e)}
                      style={e.equipped_slot ? { borderColor: "var(--accent)", boxShadow: "0 0 0 1px var(--accent-glow)" } : undefined}>
                      <div className="corner tiny">{e.locked ? "🔒" : ""}{e.equipped_slot ? " 装備中" : ""}</div>
                      <div className="row" style={{ gap: 10 }}>
                        <ItemIcon visual={e.visual} tier={3} size={42} />
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div className={`name r-${e.rarity}`}>{e.name}</div>
                          <div className="tiny mono">
                            {e.luck_bonus > 0 && `L+${(e.luck_bonus * 100).toFixed(0)}% `}
                            {e.speed_bonus > 0 && `S+${(e.speed_bonus * 100).toFixed(0)}%`}
                          </div>
                        </div>
                        <span className="tiny" style={{ color: QUALITY_COLOR[e.quality_tier] }}>{e.quality_tier === "god" ? "GOD" : e.quality_name}</span>
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            ) : null)
          )}
        </>
      ) : (
        <div style={{ marginTop: 12 }}>
          {!arts?.artifacts.length ? (
            <Empty icon="✨">Admin Artifactは所持していません。<br /><span className="tiny">管理者から付与されることがあります。</span></Empty>
          ) : (
            <div className="grid grid-auto">
              {arts.artifacts.map((a) => (
                <div key={a.instance_id} className="item-card t8" style={{ cursor: "default" }}>
                  <div className="row" style={{ gap: 10 }}>
                    <ItemIcon visual={a.artifact.visual} tier={8} size={48} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="name r-admin">{a.artifact.name}</div>
                      <div className="tiny faint">{a.artifact.theme.toUpperCase()} · TIER {a.artifact.tier}</div>
                    </div>
                    {a.state === "equipped" && <span className="badge r-admin">装備中</span>}
                  </div>
                  <div className="small muted">{a.artifact.ability}</div>
                  <div className="row-wrap tiny">
                    {a.uses_remaining !== null && <span className="chip tiny">残り{a.uses_remaining}回</span>}
                    {a.ready_at && <span className="chip tiny" style={{ color: "var(--warn)" }}>CD中</span>}
                    {a.serial && <span className="chip tiny">#{a.serial}</span>}
                  </div>
                  <div className="row-wrap" style={{ gap: 6 }}>
                    <button className="btn xs gold" disabled={!a.usable || busy || !!a.ready_at} onClick={() => useArtifact(a)}>
                      使用する
                    </button>
                    {a.state === "equipped"
                      ? <button className="btn xs ghost" disabled={busy} onClick={() => equipArtifact(null)}>外す</button>
                      : <button className="btn xs ghost" disabled={busy} onClick={() => equipArtifact(a)}>装備（オーラ）</button>}
                  </div>
                  {!a.usable && <div className="tiny faint">※ 管理者のみ使用可能</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {sel && (
        <Modal open onClose={() => setSel(null)} title={sel.name}
          footer={
            <>
              <button className="btn sm ghost" disabled={busy} onClick={() => toggleLock(sel)}>{sel.locked ? "🔓 ロック解除" : "🔒 ロック"}</button>
              <button className="btn sm danger" disabled={busy || sel.locked || !!sel.equipped_slot} onClick={() => sell(sel)}>売却 ✦{fmtInt(sel.sell_value)}</button>
              {sel.equipped_slot
                ? <button className="btn sm" disabled={busy} onClick={() => unequip(sel.equipped_slot!)}>外す</button>
                : <button className="btn sm primary" disabled={busy} onClick={() => equip(sel)}>装備する</button>}
            </>
          }>
          <div className="col" style={{ gap: 12 }}>
            <div className="row" style={{ gap: 14 }}>
              <ItemIcon visual={sel.visual} tier={4} size={80} />
              <div className="col" style={{ gap: 6, flex: 1 }}>
                <div className="row-wrap">
                  <RarityBadge rarity={sel.rarity} />
                  <span className="chip">{SLOT_LABEL[sel.slot]}</span>
                  <span className="chip" style={{ color: QUALITY_COLOR[sel.quality_tier], borderColor: "currentColor" }}>{sel.quality_name} ×{sel.quality.toFixed(3)}</span>
                </div>
                <p className="muted small" style={{ margin: 0 }}>{sel.description}</p>
              </div>
            </div>
            <div className="grid grid-2">
              <div className="glass-2 pad-sm">
                <div className="row"><span className="muted small" style={{ flex: 1 }}>Luck</span>
                  <span className="mono">+{(sel.luck_bonus * 100).toFixed(1)}% <span className="faint tiny">(基礎 {(sel.base_luck * 100).toFixed(0)}%)</span></span></div>
                <div className="row"><span className="muted small" style={{ flex: 1 }}>Roll速度</span>
                  <span className="mono">+{(sel.speed_bonus * 100).toFixed(1)}%</span></div>
              </div>
              <div className="glass-2 pad-sm col">
                <span className="muted small">パッシブ</span>
                {sel.passives.length === 0 ? <span className="tiny faint">なし</span> : (
                  <div className="row-wrap">{sel.passives.map((p, i) => <PassiveText key={i} p={p} />)}</div>
                )}
              </div>
            </div>
          </div>
        </Modal>
      )}
      {node}
    </div>
  );
}
