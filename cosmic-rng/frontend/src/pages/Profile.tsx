import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { get, post, put } from "../lib/api";
import { useAction, useApi } from "../lib/useApi";
import { useGame } from "../store/game";
import { ItemIcon } from "../components/ItemIcon";
import { Avatar, Empty, ErrorBox, Modal, Spinner, Tabs, TimeAgo, Name } from "../components/ui";
import { fmtCompact, fmtDate, fmtInt, fmtOdds } from "../lib/format";
import type { Instance, InventoryGroup, Notification } from "../lib/types";

export function Profile() {
  const { userId } = useParams();
  const me = useGame((s) => s.me);
  const id = userId ?? me?.user.id;
  const { data, loading, error, reload } = useApi<any>(id ? `/api/profile/${id}` : null);
  const [tab, setTab] = useState<"overview" | "achievements" | "customize" | "notifications">("overview");
  const isOwn = data?.own;

  return (
    <div className="page">
      <ErrorBox error={error} onRetry={reload} />
      {loading && !data ? <Spinner /> : !data ? null : (
        <>
          <div className="glass pad" style={{ marginBottom: 12, position: "relative", overflow: "hidden" }}>
            <BackgroundArt bg={data.user.background} />
            <div className="row" style={{ gap: 14, position: "relative", flexWrap: "wrap" }}>
              <Avatar user={data.user} size={72} />
              <div style={{ flex: 1, minWidth: 200 }}>
                <div className="row-wrap" style={{ gap: 8 }}>
                  <h1>{data.user.name}</h1>
                  {data.user.title_name && <span className={`badge r-${data.user.title_rarity}`}>{data.user.title_name}</span>}
                </div>
                <div className="row-wrap tiny muted">
                  <span>Lv.{data.user.level}</span>
                  <span>登録 {fmtDate(data.user.created_at)}</span>
                </div>
                {data.user.bio && <p className="small" style={{ marginTop: 6, marginBottom: 0 }}>{data.user.bio}</p>}
                {data.user.badges?.length > 0 && (
                  <div className="row-wrap" style={{ marginTop: 6, gap: 4 }}>
                    {data.user.badges.map((b: any) => (
                      <span key={b.key} className={`chip tiny r-${b.rarity}`} title={b.description}>
                        <ItemIcon visual={b.visual} size={14} animate={false} />{b.name}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>

          {data.private ? (
            <Empty icon="🔒">このプロフィールは非公開です</Empty>
          ) : (
            <>
              <Tabs value={tab} onChange={setTab} tabs={[
                { key: "overview", label: "概要" },
                { key: "achievements", label: `実績 (${data.achievements?.length ?? 0})` },
                ...(isOwn ? [{ key: "customize" as const, label: "カスタマイズ" }, { key: "notifications" as const, label: "通知" }] : []),
              ]} />

              <div style={{ marginTop: 12 }}>
                {tab === "overview" && <Overview data={data} />}
                {tab === "achievements" && (
                  !data.achievements?.length ? <Empty icon="🏆">まだありません</Empty> : (
                    <div className="grid grid-auto">
                      {data.achievements.map((a: any) => (
                        <div key={a.key} className="glass-2 pad-sm row" style={{ gap: 8 }}>
                          <span style={{ fontSize: "1.2rem" }}>{a.world_first ? "👑" : "🏆"}</span>
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div className="ellipsis">{a.name}</div>
                            <div className="tiny faint">{fmtDate(a.achieved_at)}</div>
                          </div>
                          {a.world_first && <span className="badge" style={{ color: "var(--gold)" }}>世界初</span>}
                        </div>
                      ))}
                    </div>
                  )
                )}
                {tab === "customize" && isOwn && <Customize data={data} onSaved={reload} />}
                {tab === "notifications" && isOwn && <Notifications />}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

function BackgroundArt({ bg }: { bg: string }) {
  const config = useGame((s) => s.config);
  const theme = useMemo(() => {
    const key = (bg || "bg_default").replace("bg_", "");
    const map: Record<string, string> = { default: "stellar_drift", nebula: "nebula_bloom", solar: "solar_flare", aurora: "aurora_veil",
      frost: "frozen_comet", eclipse: "crimson_eclipse", starfall: "starfall", void: "void_rift", singularity: "singularity",
      genesis: "genesis", golden: "architects_domain" };
    return config?.biomes.find((b) => b.key === (map[key] ?? "stellar_drift"))?.theme;
  }, [bg, config]);
  const colors = theme?.nebula ?? ["#1d2a6b", "#4b2d8a", "#0f4a7a"];
  return (
    <div aria-hidden style={{
      position: "absolute", inset: 0, opacity: 0.35, pointerEvents: "none",
      background: `radial-gradient(120% 140% at 15% -20%, ${colors[0]}, transparent 55%), radial-gradient(100% 120% at 85% 0%, ${colors[1]}, transparent 60%)`,
    }} />
  );
}

function Overview({ data }: { data: any }) {
  const s = data.stats;
  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="glass pad">
        <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(128px, 1fr))", gap: 12 }}>
          <div className="stat"><span className="k">Total Rolls</span><span className="v mono">{fmtCompact(s.total_rolls)}</span></div>
          <div className="stat"><span className="k">Best</span><span className="v mono">{fmtOdds(s.best_odds)}</span></div>
          <div className="stat"><span className="k">Collection</span><span className="v mono">{fmtInt(s.discovered)} <span className="tiny faint">({(s.collection_rate * 100).toFixed(1)}%)</span></span></div>
          <div className="stat"><span className="k">Assets</span><span className="v mono">✦{fmtCompact(s.assets)}</span></div>
          <div className="stat"><span className="k">First Discoveries</span><span className="v mono">{fmtInt(s.first_discoveries)}</span></div>
          <div className="stat"><span className="k">Achievements</span><span className="v mono">{fmtInt(s.achievements)}</span></div>
          <div className="stat"><span className="k">Special Rolls</span><span className="v mono">{fmtCompact(s.special_rolls)}</span></div>
        </div>
      </div>

      {s.best_item && (
        <div className="glass pad">
          <div className="tiny faint" style={{ letterSpacing: "0.14em", marginBottom: 8 }}>BEST ITEM</div>
          <div className="row" style={{ gap: 12 }}>
            <ItemIcon visual={s.best_item.visual} tier={s.best_item.tier} size={56} />
            <div>
              <div className={`r-${s.best_item.rarity}`} style={{ fontWeight: 700, fontSize: "1.05rem" }}><Name en={s.best_item.name} ja={(s.best_item as any).name_ja} /></div>
              <div className="mono tiny">{fmtOdds(s.best_item.odds, s.best_item.display_odds)}</div>
            </div>
          </div>
        </div>
      )}

      {data.equipment?.length > 0 && (
        <div className="glass pad">
          <div className="tiny faint" style={{ letterSpacing: "0.14em", marginBottom: 8 }}>装備</div>
          <div className="row-wrap">
            {data.equipment.map((e: any) => (
              <span key={e.slot} className={`chip r-${e.rarity}`}>
                <ItemIcon visual={e.visual} size={18} animate={false} tier={e.rarity === "admin" ? 8 : 3} />{e.name}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="glass pad">
        <div className="tiny faint" style={{ letterSpacing: "0.14em", marginBottom: 8 }}>SHOWCASE</div>
        {!data.showcase?.length ? <div className="muted small">未設定</div> : (
          <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))" }}>
            {data.showcase.map((sc: any) => (
              <div key={sc.slot} className={`item-card t${sc.instance.item?.tier ?? 1}`} style={{ cursor: "default", alignItems: "center", textAlign: "center" }}>
                <ItemIcon visual={sc.instance.item?.visual} tier={sc.instance.item?.tier ?? 1} size={52} />
                <div className={`tiny r-${sc.instance.item?.rarity}`}>{sc.instance.item?.name}</div>
                {sc.instance.serial && <div className="tiny faint mono">#{sc.instance.serial}</div>}
              </div>
            ))}
          </div>
        )}
      </div>

      {Object.keys(s.rarity_counts ?? {}).length > 0 && (
        <div className="glass pad">
          <div className="tiny faint" style={{ letterSpacing: "0.14em", marginBottom: 8 }}>RARITY BREAKDOWN</div>
          <div className="row-wrap">
            {Object.entries(s.rarity_counts).map(([k, v]) => (
              <span key={k} className={`chip tiny r-${k}`}>{k} {fmtCompact(v as number)}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Customize({ data, onSaved }: { data: any; onSaved: () => void }) {
  const [bio, setBio] = useState(data.user.bio ?? "");
  const [title, setTitle] = useState(data.user.title_key ?? "");
  const [bg, setBg] = useState(data.user.background ?? "bg_default");
  const [badges, setBadges] = useState<string[]>((data.user.badges ?? []).map((b: any) => b.key));
  const [showcaseOpen, setShowcaseOpen] = useState(false);
  const { run, busy } = useAction();

  const save = async () => {
    await run(() => put("/api/profile", { title_key: title || null, clear_title: !title, background: bg, badges, bio }), { success: "保存しました" });
    onSaved();
  };

  return (
    <div className="col" style={{ gap: 12 }}>
      <div className="glass pad col">
        <h3>プロフィール</h3>
        <div><label>自己紹介（200文字まで）</label><textarea value={bio} maxLength={200} onChange={(e) => setBio(e.target.value)} /></div>
        <div>
          <label>称号</label>
          <select value={title} onChange={(e) => setTitle(e.target.value)}>
            <option value="">（なし）</option>
            {(data.titles ?? []).map((t: any) => <option key={t.key} value={t.key}>{t.name}</option>)}
          </select>
        </div>
        <div>
          <label>背景</label>
          <select value={bg} onChange={(e) => setBg(e.target.value)}>
            {(data.backgrounds ?? []).map((b: any) => <option key={b.key} value={b.key}>{b.name}</option>)}
          </select>
        </div>
        <div>
          <label>バッジ（最大5個）</label>
          <div className="row-wrap" style={{ gap: 5 }}>
            {(data.badges_all ?? []).map((b: any) => (
              <button key={b.key} className={`chip ${badges.includes(b.key) ? "on" : ""} r-${b.rarity}`}
                onClick={() => setBadges((cur) => cur.includes(b.key) ? cur.filter((x) => x !== b.key) : cur.length < 5 ? [...cur, b.key] : cur)}>
                {b.name}
              </button>
            ))}
            {!data.badges_all?.length && <span className="muted small">実績でバッジを獲得できます</span>}
          </div>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button className="btn primary" disabled={busy} onClick={save}>保存</button>
          <button className="btn ghost" onClick={() => setShowcaseOpen(true)}>Showcaseを編集</button>
        </div>
      </div>
      {showcaseOpen && <ShowcaseEditor current={data.showcase ?? []} onClose={() => setShowcaseOpen(false)} onSaved={onSaved} />}
    </div>
  );
}

function ShowcaseEditor({ current, onClose, onSaved }: { current: any[]; onClose: () => void; onSaved: () => void }) {
  const [slots, setSlots] = useState<(number | null)[]>(() => {
    const arr: (number | null)[] = [null, null, null, null, null, null];
    for (const s of current) arr[s.slot] = s.instance.id;
    return arr;
  });
  const [picking, setPicking] = useState<number | null>(null);
  const { data: inv } = useApi<{ groups: InventoryGroup[] }>("/api/inventory", { per_page: 120, sort: "rarity:desc" });
  const [instances, setInstances] = useState<Instance[]>([]);
  const { run, busy } = useAction();

  useEffect(() => {
    (async () => {
      const all: Instance[] = [];
      for (const g of (inv?.groups ?? []).slice(0, 30)) {
        try {
          const r = await get<{ instances: Instance[] }>(`/api/inventory/item/${g.item.id}`, { per_page: 4 });
          all.push(...r.instances);
        } catch {
          /* ignore */
        }
      }
      setInstances(all);
    })();
  }, [inv]);

  const save = async () => {
    await run(() => put("/api/profile/showcase", { instance_ids: slots }), { success: "Showcaseを更新しました" });
    onSaved();
    onClose();
  };

  return (
    <Modal open onClose={onClose} title="Showcase（最大6個）" wide
      footer={<><button className="btn ghost" onClick={onClose}>閉じる</button><button className="btn primary" disabled={busy} onClick={save}>保存</button></>}>
      <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(110px, 1fr))", marginBottom: 14 }}>
        {slots.map((id, i) => {
          const inst = instances.find((x) => x.id === id);
          return (
            <button key={i} className="item-card" style={{ alignItems: "center", textAlign: "center", minHeight: 120 }}
              onClick={() => setPicking(i)}>
              {inst ? (
                <>
                  <ItemIcon visual={inst.item?.visual} tier={inst.item?.tier ?? 1} size={46} />
                  <div className={`tiny r-${inst.item?.rarity}`}>{inst.item?.name}</div>
                </>
              ) : <div className="faint" style={{ fontSize: "1.6rem" }}>＋</div>}
            </button>
          );
        })}
      </div>
      {picking !== null && (
        <div className="col">
          <div className="row"><strong>スロット {picking + 1} に配置</strong>
            <span className="spacer" />
            <button className="btn xs ghost" onClick={() => { setSlots((s) => s.map((v, i) => (i === picking ? null : v))); setPicking(null); }}>空にする</button>
          </div>
          <div className="col" style={{ gap: 3, maxHeight: "36vh", overflow: "auto" }}>
            {instances.map((i) => (
              <button key={i.id} className="row" style={{ gap: 8, padding: "5px 8px", borderRadius: 8, background: "rgba(255,255,255,0.03)", cursor: "pointer", border: "1px solid transparent", textAlign: "left" }}
                onClick={() => { setSlots((s) => s.map((v, idx) => (idx === picking ? i.id : v === i.id ? null : v))); setPicking(null); }}>
                <ItemIcon visual={i.item?.visual} tier={i.item?.tier ?? 1} size={22} animate={false} />
                <span className={`r-${i.item?.rarity} ellipsis`} style={{ flex: 1 }}>{i.item?.name}</span>
                {i.serial && <span className="mono tiny faint">#{i.serial}</span>}
              </button>
            ))}
          </div>
        </div>
      )}
    </Modal>
  );
}

function Notifications() {
  const { data, reload } = useApi<{ notifications: Notification[]; unread: number }>("/api/notifications");
  const setUnread = useGame((s) => s.set);
  useEffect(() => {
    if (data && data.unread > 0) {
      post("/api/notifications/read", {}).then(() => {
        setUnread({ unread: 0 });
        reload(true);
      }).catch(() => undefined);
    }
  }, [data?.unread]);

  if (!data?.notifications.length) return <Empty icon="🔔">通知はありません</Empty>;
  return (
    <div className="col" style={{ gap: 6 }}>
      {data.notifications.map((n) => (
        <div key={n.id} className="glass-2 pad-sm col" style={{ gap: 2, opacity: n.read ? 0.72 : 1 }}>
          <div className="row"><strong style={{ flex: 1 }}>{n.title}</strong><TimeAgo iso={n.created_at} /></div>
          {n.body && <div className="small muted">{n.body}</div>}
        </div>
      ))}
    </div>
  );
}
