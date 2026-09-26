/** WebSocket client: session-cookie authenticated, auto-reconnecting, dispatches realtime events into the store. */
import { withBase } from "./base";
import { events, useGame } from "../store/game";
import type { FeedEvent, FirstDiscovery } from "./types";
import { audio } from "../audio/engine";

let ws: WebSocket | null = null;
let retry = 0;
let pingTimer: number | undefined;
let reconnectTimer: number | undefined;
let stopped = false;

function url() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}${withBase("/ws")}`;
}

export function connectSocket() {
  stopped = false;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(url());
  } catch {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    retry = 0;
    useGame.getState().set({ connected: true });
    window.clearInterval(pingTimer);
    pingTimer = window.setInterval(() => send({ t: "ping" }), 25000);
  };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      handle(msg.t, msg.d);
    } catch (e) {
      console.warn("bad ws message", e);
    }
  };
  ws.onclose = (ev) => {
    useGame.getState().set({ connected: false });
    window.clearInterval(pingTimer);
    if (ev.code === 4001) {
      location.href = withBase("/?error=session");
      return;
    }
    if (!stopped) scheduleReconnect();
  };
  ws.onerror = () => ws?.close();
}

export function disconnectSocket() {
  stopped = true;
  window.clearTimeout(reconnectTimer);
  ws?.close();
  ws = null;
}

function scheduleReconnect() {
  window.clearTimeout(reconnectTimer);
  const delay = Math.min(30000, 800 * 2 ** retry) + Math.random() * 500;
  retry = Math.min(retry + 1, 6);
  reconnectTimer = window.setTimeout(connectSocket, delay);
}

function send(obj: unknown) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function handle(type: string, data: any) {
  const st = useGame.getState();
  switch (type) {
    case "hello":
      st.setFeed(data.feed as FeedEvent[]);
      st.set({ online: data.online });
      break;
    case "online":
      st.set({ online: data.count });
      break;
    case "feed": {
      st.pushFeed(data as FeedEvent);
      break;
    }
    case "first_discovery": {
      const fd = data as FirstDiscovery;
      if (fd.player_id !== st.me?.user.id) {
        st.pushBanner(fd);
        audio.sfx("world_notice");
      }
      break;
    }
    case "biome":
      if (st.hud) {
        const prev = st.hud.biome?.key;
        st.patchHud({ biome: data });
        if (prev !== data.key || data.state) {
          events.emit("biome_change", data);
          audio.sfx(data.kind === "default" ? "biome_end" : "biome_shift");
        }
      }
      break;
    case "live_reveal": {
      // Someone, somewhere, just found something extraordinary. Opt-out lives
      // in the player's own notification settings.
      if (data.user?.id === st.me?.user.id) break;
      if (st.me?.settings.notifications.live === false) break;
      st.pushLive(data);
      audio.sfx("world_notice");
      break;
    }
    case "game_event":
      events.emit("game_event", data);
      if (data.kind === "community_goal_done") {
        st.toast("世界目標を達成しました", "cosmic", data.message || "全員にLuckボーナスが付与されました", 12000);
      }
      break;
    case "ranking":
      st.set({ liveRankings: data.boards });
      break;
    case "notify":
      st.set({ unread: st.unread + 1 });
      st.toast(data.title, "info", data.body);
      events.emit("notify", data);
      if (String(data.type || "").startsWith("trade")) events.emit("trade", data);
      break;
    case "achievement":
      st.announceAchievements([data]);
      break;
    case "trade":
      events.emit("trade", data);
      break;
    case "market":
      events.emit("market", data);
      break;
    case "admin_event":
      if (data.kind === "broadcast") {
        st.toast(data.title, data.style === "warning" ? "warning" : "cosmic", data.body, 12000);
      } else if (data.kind === "artifact") {
        st.enqueueReveal({ type: "artifact", cinematic: data, result: data.result, global: true });
      } else if (data.kind === "granted" || data.kind === "blessing") {
        st.enqueueReveal({ type: "artifact", cinematic: { artifact: data.artifact, name: data.name, theme: data.theme, tier: Math.min(3, data.tier || 2), visual: data.visual || {} }, result: { granted: data.kind } });
        st.refreshHud();
      }
      break;
    case "admin_stats":
      st.set({ adminStats: data });
      break;
    case "admin_notify":
      events.emit("admin_notify", data);
      st.toast(`[Admin] ${data.title}`, "warning", data.body);
      break;
    case "effects_changed":
    case "state_changed":
      st.refreshHud();
      events.emit("state_changed", data);
      break;
    case "maintenance":
      st.set({ maintenance: data });
      if (data.enabled) st.toast("メンテナンス", "warning", data.message, 10000);
      break;
    case "refresh":
      events.emit("refresh", data);
      st.refreshHud();
      break;
    case "force_logout":
      location.href = withBase("/?error=session");
      break;
    default:
      break;
  }
}
