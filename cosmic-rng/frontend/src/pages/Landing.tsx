import { useEffect, useRef, useState } from "react";
import { withBase } from "../lib/base";
import { useGame } from "../store/game";
import { AuthPanel } from "../components/AuthPanel";

const ERRORS: Record<string, string> = {
  banned: "このアカウントは現在利用できません。",
  oauth_state: "ログインの有効期限が切れました。もう一度お試しください。",
  oauth_failed: "Discord認証に失敗しました。",
  oauth_denied: "Discordログインがキャンセルされました。",
  registration_closed: "現在、新規登録を停止しています。",
  session: "セッションが終了しました。再度ログインしてください。",
};

const FEATURES = [
  { icon: "🎲", title: "サーバー権威のRNG", body: "確率判定はすべてサーバー側。Luck・Biome・装備・Boostが一つの確率テーブルに合成されます。" },
  { icon: "🌌", title: "個人Biome", body: "毎秒抽選される17種のBiome。Void RiftやSingularityでは世界が別物になります。" },
  { icon: "✦", title: "超レア演出", body: "レア度が上がるほど演出は長く豪華に。最高レアは30秒級のクライマックス。" },
  { icon: "🏆", title: "世界初発見", body: "誰も引いたことのないアイテムを最初に引くと、世界中に通知されます。" },
  { icon: "🤝", title: "市場・取引", body: "プレイヤー同士で売買・交換・贈与。相場も履歴も残ります。" },
  { icon: "🌙", title: "オフラインRoll", body: "ブラウザを閉じている間もAuto Rollが進行。復帰時にまとめて結果を確認。" },
];

export function Landing() {
  const config = useGame((s) => s.config);
  // Landing only renders once boot finished — or once App gave up waiting for
  // it. Either way, "not bootstrapped here" means the server never answered.
  const bootstrapped = useGame((s) => s.bootstrapped);
  const unreachable = useGame((s) => s.offline) || !bootstrapped;
  const bootstrap = useGame((s) => s.bootstrap);
  const params = new URLSearchParams(location.search);
  const error = params.get("error");
  const [retrying, setRetrying] = useState(false);
  const attempt = useRef(0);

  const signedIn = async () => {
    await bootstrap();
    history.replaceState(null, "", withBase("/roll"));
  };

  const retry = async () => {
    setRetrying(true);
    try {
      await bootstrap();
    } finally {
      setRetrying(false);
    }
  };

  // Shared hosts put an idle app to sleep, so the first visit after a quiet
  // spell can time out while the process is still waking. Keep trying on the
  // player's behalf, backing off, so the page heals without a manual reload.
  useEffect(() => {
    if (!unreachable) { attempt.current = 0; return; }
    const wait = Math.min(30000, 3000 * 2 ** attempt.current);
    attempt.current += 1;
    const t = window.setTimeout(() => { void bootstrap(); }, wait);
    return () => window.clearTimeout(t);
  }, [unreachable, bootstrap]);

  return (
    <div className="page" style={{ maxWidth: 940, paddingTop: "min(9vh, 70px)" }}>
      <div className="center col" style={{ gap: 18, alignItems: "center" }}>
        <div className="fx-float" style={{ fontSize: "clamp(3rem, 12vw, 5.6rem)", lineHeight: 1, filter: "drop-shadow(0 0 40px rgba(150,120,255,0.6))" }}>✦</div>
        <h1 style={{
          fontSize: "clamp(2rem, 9vw, 4.2rem)", letterSpacing: "0.16em", fontWeight: 900,
          background: "linear-gradient(90deg, #ffffff, #b58cff 45%, #56c8ff)", WebkitBackgroundClip: "text",
          backgroundClip: "text", color: "transparent",
        }}>
          COSMIC RNG
        </h1>
        <p className="muted" style={{ fontSize: "1.02rem", maxWidth: 560 }}>
          巨大な宇宙の片隅で、あなただけのBiomeが渦を巻く。<br />
          次の1回で、世界にまだ存在しないアイテムが生まれるかもしれない。
        </p>

        {error && (
          <div className="glass pad" style={{ borderColor: "rgba(255,92,122,0.5)", maxWidth: 480 }}>
            {ERRORS[error] ?? "エラーが発生しました。"}
          </div>
        )}

        {unreachable && (
          <div className="glass pad col" style={{ borderColor: "rgba(255,193,77,0.5)", maxWidth: 480, gap: 8 }}>
            <strong>サーバーに接続できません</strong>
            <span className="muted small">
              起動直後や混雑時は少し時間がかかることがあります。自動で再接続を試みています。
            </span>
            <button className="btn sm" disabled={retrying} onClick={retry}>
              {retrying ? "接続中…" : "今すぐ再試行"}
            </button>
          </div>
        )}

        {config?.maintenance.enabled && (
          <div className="glass pad" style={{ borderColor: "rgba(255,193,77,0.5)", maxWidth: 480 }}>
            🛠 {config.maintenance.message}
          </div>
        )}

        <div className="col" style={{ gap: 10, width: "min(380px, 100%)" }}>
          <AuthPanel onDone={signedIn} />
        </div>
      </div>

      <div className="grid grid-auto" style={{ marginTop: 44 }}>
        {FEATURES.map((f) => (
          <div key={f.title} className="glass pad">
            <div style={{ fontSize: "1.6rem", marginBottom: 6 }}>{f.icon}</div>
            <h3 style={{ marginBottom: 4 }}>{f.title}</h3>
            <p className="muted small" style={{ margin: 0 }}>{f.body}</p>
          </div>
        ))}
      </div>

      {config && (
        <div className="center muted tiny" style={{ marginTop: 30 }}>
          RNG v{config.rng_version} · Content v{config.content_version} · アイテム、Biome、実績は運営により随時追加されます
        </div>
      )}
    </div>
  );
}
