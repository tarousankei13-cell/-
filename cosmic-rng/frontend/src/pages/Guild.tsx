import { useState } from "react";
import { Link } from "react-router-dom";
import { post } from "../lib/api";
import { useApi, useAction } from "../lib/useApi";
import { useGame } from "../store/game";
import { Avatar, Empty, ErrorBox, Modal, Spinner } from "../components/ui";
import { fmtCompact, fmtInt } from "../lib/format";
import type { UserBrief } from "../lib/types";

type Member = UserBrief & { role: string; online: boolean; weekly_rolls: number };
type Guild = {
  id: number; name: string; tag: string; description: string; owner_id: number; is_open: boolean;
  members: number; max_members: number; weekly_rolls: number; member_list?: Member[]; online_now?: number;
};
type Bonus = { per_member_pct: number; min_online: number; cap: number; max_pct?: number };
type Mine = { guild: Guild | null; role?: string; bonus: Bonus; terms?: Bonus };

function BonusLine({ bonus, online }: { bonus: Bonus; online: number }) {
  const counted = Math.min(bonus.cap, online);
  const active = online >= bonus.min_online;
  return (
    <div className="glass-2 pad-sm row" style={{ gap: 8, flexWrap: "wrap" }}>
      <span className="badge" style={{ color: active ? "var(--good)" : "var(--text-faint)" }}>
        {active ? `Luck +${(counted * bonus.per_member_pct).toFixed(0)}%` : "待機中"}
      </span>
      <span className="small muted">
        同時オンライン{bonus.min_online}人以上で、1人につき +{bonus.per_member_pct}%（最大{bonus.cap}人分）。
        いま {online} 人がオンライン。
      </span>
    </div>
  );
}

export function GuildPage() {
  const me = useGame((s) => s.me);
  const mine = useApi<Mine>("/api/guilds/mine");
  const list = useApi<{ guilds: Guild[]; terms: Bonus }>("/api/guilds");
  const board = useApi<{ board: any[] }>("/api/guilds/board");
  const { busy, run } = useAction();
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", tag: "", description: "" });

  const reloadAll = () => { mine.reload(); list.reload(); board.reload(); };
  const guild = mine.data?.guild ?? null;
  const bonus = mine.data?.bonus ?? mine.data?.terms ?? list.data?.terms ?? { per_member_pct: 2, min_online: 2, cap: 5 };

  const create = async () => {
    const res = await run(() => post("/api/guilds/create", form, true), { success: "ギルドを設立しました" });
    if (res) { setCreating(false); setForm({ name: "", tag: "", description: "" }); reloadAll(); }
  };

  if (mine.loading && !mine.data) return <Spinner label="ギルドを読み込み中…" />;

  return (
    <div className="page">
      <div className="page-head">
        <h1>ギルド</h1>
        <span className="sub">仲間と同時にオンラインだと、全員のLuckが上がります</span>
      </div>
      <ErrorBox error={mine.error} onRetry={reloadAll} />

      {guild ? (
        <div className="col">
          <div className="glass pad col">
            <div className="row-wrap">
              <span className="guild-tag">[{guild.tag}]</span>
              <h2 style={{ flex: 1, minWidth: 0 }} className="ellipsis">{guild.name}</h2>
              <span className="chip tiny">{guild.members}/{guild.max_members}人</span>
              <span className="chip tiny">今週 {fmtInt(guild.weekly_rolls)} Roll</span>
            </div>
            {guild.description && <div className="muted small">{guild.description}</div>}
            <BonusLine bonus={bonus} online={guild.online_now ?? 0} />
            <div className="row-wrap">
              <button className="btn sm ghost" disabled={busy}
                onClick={() => run(() => post("/api/guilds/leave"), { success: "脱退しました" }).then(reloadAll)}>
                脱退する
              </button>
            </div>
          </div>

          <div className="glass pad col">
            <h3>メンバー</h3>
            {(guild.member_list ?? []).map((m) => (
              <div className="row friend-row" key={m.id}>
                <span className={`presence ${m.online ? "on" : ""}`} aria-hidden />
                <Avatar user={m} size={30} />
                <Link to={`/profile/${m.id}`} className="ellipsis" style={{ flex: 1, color: "inherit" }}>
                  {m.name} <span className="tiny faint">Lv.{m.level}</span>
                </Link>
                {m.role === "owner" && <span className="badge" style={{ color: "var(--gold)" }}>OWNER</span>}
                <span className="tiny mono faint">{fmtCompact(m.weekly_rolls)}</span>
                {mine.data?.role === "owner" && m.id !== me?.user.id && (
                  <button className="btn xs ghost" disabled={busy}
                    onClick={() => run(() => post("/api/guilds/kick", { user_id: m.id }), { success: "除名しました" }).then(reloadAll)}>
                    除名
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div className="col">
          <div className="glass pad row-wrap">
            <div style={{ flex: 1, minWidth: 220 }}>
              <h3>まだどこにも所属していません</h3>
              <div className="muted small">既存のギルドに加入するか、自分で設立できます（設立費 ✦10,000）。</div>
            </div>
            <button className="btn primary" onClick={() => setCreating(true)}>ギルドを設立</button>
          </div>

          <div className="glass pad col">
            <h3>募集中のギルド</h3>
            {!list.data?.guilds.length ? <Empty icon="🏳">まだギルドがありません。最初のひとつを作りましょう。</Empty> : (
              <div className="grid grid-auto">
                {list.data.guilds.map((g) => (
                  <div className="glass-2 pad-sm col" key={g.id} style={{ gap: 6 }}>
                    <div className="row">
                      <span className="guild-tag">[{g.tag}]</span>
                      <strong className="ellipsis" style={{ flex: 1 }}>{g.name}</strong>
                    </div>
                    <div className="tiny faint ellipsis">{g.description || "—"}</div>
                    <div className="row tiny muted">
                      <span>{g.members}/{g.max_members}人</span>
                      <span className="spacer" />
                      <span className="mono">今週 {fmtCompact(g.weekly_rolls)}</span>
                    </div>
                    <button className="btn xs" disabled={busy || !g.is_open || g.members >= g.max_members}
                      onClick={() => run(() => post("/api/guilds/join", { guild_id: g.id }), { success: "加入しました" }).then(reloadAll)}>
                      {g.members >= g.max_members ? "満員" : g.is_open ? "加入する" : "募集停止中"}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      <div className="glass pad col" style={{ marginTop: 12 }}>
        <h3>週間ギルドランキング</h3>
        {!board.data?.board.length ? <div className="muted small">今週はまだ記録がありません</div> : (
          <div className="col" style={{ gap: 4 }}>
            {board.data.board.map((row: any, i: number) => (
              <div className="row rank-row" key={row.id}>
                <span className="rank-n">{i + 1}</span>
                <span className="guild-tag">[{row.tag}]</span>
                <span className="ellipsis" style={{ flex: 1 }}>{row.name}</span>
                <span className="tiny faint">{row.members}人</span>
                <span className="mono">{fmtInt(row.weekly_rolls)}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <Modal open={creating} onClose={() => setCreating(false)} title="ギルドを設立"
        footer={<>
          <button className="btn ghost" onClick={() => setCreating(false)}>やめる</button>
          <button className="btn primary" disabled={busy || form.name.length < 2 || form.tag.length < 2} onClick={create}>
            ✦10,000 で設立
          </button>
        </>}>
        <div className="col">
          <div>
            <label htmlFor="g-name">ギルド名（2〜24文字）</label>
            <input id="g-name" value={form.name} maxLength={24} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <label htmlFor="g-tag">タグ（英数字2〜5文字）</label>
            <input id="g-tag" value={form.tag} maxLength={5} onChange={(e) => setForm({ ...form, tag: e.target.value.toUpperCase() })} />
          </div>
          <div>
            <label htmlFor="g-desc">紹介文</label>
            <textarea id="g-desc" value={form.description} maxLength={140} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
        </div>
      </Modal>
    </div>
  );
}
