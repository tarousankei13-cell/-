import { useState } from "react";
import { Link } from "react-router-dom";
import { post, get } from "../lib/api";
import { useApi, useAction } from "../lib/useApi";
import { Avatar, Empty, ErrorBox, Spinner, TimeAgo } from "../components/ui";
import { ItemIcon } from "../components/ItemIcon";
import { fmtOdds } from "../lib/format";
import type { UserBrief } from "../lib/types";

type Friend = UserBrief & { online: boolean; last_seen: string | null };
type FriendData = { friends: Friend[]; incoming: UserBrief[]; outgoing: UserBrief[] };
type Activity = { user: UserBrief; item: any; odds: number; at: string };

function Person({ u, online, right }: { u: UserBrief; online?: boolean; right?: React.ReactNode }) {
  return (
    <div className="row friend-row">
      <span className={`presence ${online ? "on" : ""}`} aria-hidden />
      <Avatar user={u} size={32} />
      <Link to={`/profile/${u.id}`} className="ellipsis" style={{ flex: 1, color: "inherit" }}>
        {u.name} <span className="tiny faint">Lv.{u.level}</span>
      </Link>
      {right}
    </div>
  );
}

export function Friends() {
  const { data, loading, error, reload } = useApi<FriendData>("/api/friends");
  const activity = useApi<{ activity: Activity[] }>("/api/friends/activity");
  const { busy, run } = useAction();
  const [q, setQ] = useState("");
  const [found, setFound] = useState<UserBrief[] | null>(null);

  const search = async () => {
    if (q.trim().length < 1) return;
    const res = await run(() => get<{ users: UserBrief[] }>("/api/users/search", { q: q.trim() }));
    if (res) setFound(res.users);
  };

  const act = (path: string, body: any) =>
    run(() => post(path, body), { success: "更新しました" }).then(() => {
      reload();
      activity.reload();
    });

  if (loading && !data) return <Spinner label="フレンドを読み込み中…" />;

  return (
    <div className="page">
      <div className="page-head">
        <h1>フレンド</h1>
        <span className="sub">{data?.friends.length ?? 0}人 · オンライン {data?.friends.filter((f) => f.online).length ?? 0}人</span>
      </div>
      <ErrorBox error={error} onRetry={reload} />

      <div className="grid grid-2">
        <div className="col">
          <div className="glass pad col">
            <h3>フレンドを探す</h3>
            <div className="row">
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="ユーザー名で検索"
                onKeyDown={(e) => e.key === "Enter" && search()} aria-label="ユーザー名で検索" />
              <button className="btn" onClick={search} disabled={busy}>検索</button>
            </div>
            {found && (found.length === 0
              ? <div className="muted small">見つかりませんでした</div>
              : <div className="col" style={{ gap: 4 }}>
                  {found.map((u) => (
                    <Person key={u.id} u={u} right={
                      <button className="btn xs" disabled={busy} onClick={() => act("/api/friends/request", { user_id: u.id })}>申請</button>
                    } />
                  ))}
                </div>)}
          </div>

          {!!data?.incoming.length && (
            <div className="glass pad col">
              <h3>届いている申請 <span className="badge" style={{ color: "var(--gold)" }}>{data.incoming.length}</span></h3>
              {data.incoming.map((u) => (
                <Person key={u.id} u={u} right={
                  <span className="row" style={{ gap: 6 }}>
                    <button className="btn xs primary" disabled={busy} onClick={() => act("/api/friends/respond", { user_id: u.id, accept: true })}>承認</button>
                    <button className="btn xs ghost" disabled={busy} onClick={() => act("/api/friends/respond", { user_id: u.id, accept: false })}>拒否</button>
                  </span>
                } />
              ))}
            </div>
          )}

          <div className="glass pad col">
            <h3>フレンド</h3>
            {!data?.friends.length ? <Empty icon="🤝">まだフレンドがいません</Empty> : data.friends.map((f) => (
              <Person key={f.id} u={f} online={f.online} right={
                <span className="row" style={{ gap: 6 }}>
                  <span className="tiny faint">{f.online ? "オンライン" : <TimeAgo iso={f.last_seen} />}</span>
                  <button className="btn xs ghost" disabled={busy} onClick={() => act("/api/friends/remove", { user_id: f.id })}>解除</button>
                </span>
              } />
            ))}
          </div>

          {!!data?.outgoing.length && (
            <div className="glass pad col">
              <h3 className="muted">送信中の申請</h3>
              {data.outgoing.map((u) => <Person key={u.id} u={u} right={<span className="tiny faint">承認待ち</span>} />)}
            </div>
          )}
        </div>

        <div className="glass pad col">
          <h3>フレンドの発見</h3>
          {!activity.data?.activity.length ? (
            <Empty icon="✦">まだ動きがありません</Empty>
          ) : activity.data.activity.map((a, i) => (
            <div className="row feed-row" key={i}>
              <ItemIcon visual={a.item?.visual} tier={a.item?.tier} size={22} animate={false} />
              <span className="ellipsis" style={{ flex: 1 }}>
                <span className={`r-${a.item?.rarity}`}>{a.item?.name_ja || a.item?.name}</span>
                <span className="faint tiny mono"> {fmtOdds(a.odds)}</span>
              </span>
              <span className="tiny faint ellipsis" style={{ maxWidth: 90 }}>{a.user?.name}</span>
              <span className="tiny faint"><TimeAgo iso={a.at} /></span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
