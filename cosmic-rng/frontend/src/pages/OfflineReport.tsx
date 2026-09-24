import { useMemo } from "react";
import { ItemIcon } from "../components/ItemIcon";
import { Modal } from "../components/ui";
import { fmtDuration, fmtInt, fmtLuck, fmtOdds } from "../lib/format";
import type { OfflineSummary } from "../lib/types";
import { useGame } from "../store/game";

/** Post-login summary of everything Auto Roll produced while the player was away. */
export function OfflineReport({ summary, onClose }: { summary: OfflineSummary; onClose: () => void }) {
  const enqueue = useGame((s) => s.enqueueReveal);
  const highlights = useMemo(() => summary.results.filter((r) => r.reveal || r.first).slice(0, 12), [summary]);
  const rest = useMemo(() => summary.results.filter((r) => !r.reveal && !r.first).slice(0, 60), [summary]);

  const replay = (r: (typeof summary.results)[number]) => {
    enqueue({
      type: "roll",
      full: true,
      roll: {
        id: -Math.floor(Math.random() * 1e6), number: 0, item: r.item, instance_ids: [], odds: r.item.odds ?? 1,
        final_chance: 0, final_odds: null, luck: { base: 1, equipment: 1, biome: 1, temporary: 1, special: 1, event: 1, other: 1, final: summary.luck_max },
        biome: { key: "", name: summary.biomes[0]?.name ?? "Offline", state: null }, special: false, hidden_special: null,
        effects_applied: [], fortune: { key: "lucky", label: "Offline Discovery", top_percent: 0, score: 0 },
        auto_deleted: false, auto_delete_mode: null, auto_sold: 0, overflow: false,
        first_discovery: r.first ? { item: r.item, odds: r.item.odds ?? 1, discovered_at: summary.window_end, player: useGame.getState().me?.user.name ?? "", player_id: useGame.getState().me?.user.id ?? 0, offline: true } : null,
        new_collection: false, duplicated: 0, forced: false, best_of: 1, preview_used: false, xp: 0, cosmic_eye: null,
        progress: { quests_completed: [], achievements: [], level_up: null, unlocked: [] },
      },
    });
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={<span>🌙 オフラインRoll結果</span>}
      wide
      footer={<button className="btn primary" onClick={onClose}>受け取る</button>}
    >
      <div className="col" style={{ gap: 14 }}>
        <div className="row-wrap" style={{ gap: 8 }}>
          <span className="chip">{fmtDuration(summary.seconds)}</span>
          <span className="chip mono">{fmtInt(summary.rolls)} rolls</span>
          <span className="chip">取得 {fmtInt(summary.totals.kept)}</span>
          {summary.totals.deleted > 0 && <span className="chip">自動処理 {fmtInt(summary.totals.deleted)}</span>}
          {summary.totals.overflow > 0 && <span className="chip" style={{ color: "var(--warn)" }}>容量超過 {fmtInt(summary.totals.overflow)}</span>}
          {summary.totals.stardust > 0 && <span className="chip" style={{ color: "var(--gold)" }}>✦ +{fmtInt(summary.totals.stardust)}</span>}
          <span className="chip">Special {fmtInt(summary.specials)}</span>
          <span className="chip">最大 Luck {fmtLuck(summary.luck_max)}</span>
          {summary.efficiency !== undefined && <span className="chip tiny faint">効率 {(summary.efficiency * 100).toFixed(0)}%</span>}
        </div>

        {summary.biomes.length > 0 && (
          <div className="row-wrap tiny muted">
            <span>遭遇したBiome:</span>
            {summary.biomes.map((b) => <span key={b.key} className="chip tiny">{b.name}</span>)}
          </div>
        )}

        {highlights.length > 0 && (
          <div>
            <h3 style={{ marginBottom: 8 }}>ハイライト</h3>
            <div className="grid grid-auto">
              {highlights.map((r, i) => (
                <button key={i} className={`item-card t${r.item.tier}`} onClick={() => replay(r)} title="演出を再生">
                  <div className="row" style={{ gap: 10 }}>
                    <ItemIcon visual={r.item.visual} tier={r.item.tier} size={46} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className={`name r-${r.item.rarity}`}>{r.item.name}</div>
                      <div className="odds mono">{fmtOdds(r.item.odds, r.item.display_odds)}</div>
                      {r.first && <span className="badge" style={{ color: "var(--gold)" }}>WORLD FIRST</span>}
                    </div>
                    {r.count > 1 && <span className="mono">×{r.count}</span>}
                  </div>
                  <div className="tiny faint">タップで演出を再生</div>
                </button>
              ))}
            </div>
          </div>
        )}

        {rest.length > 0 && (
          <div>
            <h3 style={{ marginBottom: 8 }}>獲得アイテム</h3>
            <div className="offline-list">
              {rest.map((r, i) => (
                <div key={i} className="offline-row">
                  <ItemIcon visual={r.item.visual} tier={r.item.tier} size={24} animate={false} />
                  <span className={`r-${r.item.rarity} ellipsis`} style={{ flex: 1 }}>{r.item.name}</span>
                  <span className="mono tiny faint">{fmtOdds(r.item.odds, r.item.display_odds)}</span>
                  <span className="mono">×{fmtInt(r.count)}</span>
                  {r.deleted > 0 && <span className="tiny faint">({r.deleted}件 自動処理)</span>}
                </div>
              ))}
            </div>
            {summary.distinct > rest.length + highlights.length && (
              <div className="tiny faint center" style={{ marginTop: 6 }}>他 {summary.distinct - rest.length - highlights.length} 種類</div>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
