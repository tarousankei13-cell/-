import type { RarityKey } from "./types";

const nf = new Intl.NumberFormat("en-US");

export function fmtInt(n: number | null | undefined): string {
  if (n === null || n === undefined || !isFinite(n)) return "—";
  return nf.format(Math.round(n));
}

/** Compact number: 1.2K, 3.4M, 5.6B, 7.8T, then scientific. */
export function fmtCompact(n: number | null | undefined): string {
  if (n === null || n === undefined || !isFinite(n)) return "—";
  const a = Math.abs(n);
  if (a < 10_000) return nf.format(Math.round(n * 100) / 100);
  const units: [number, string][] = [[1e15, "Qa"], [1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]];
  if (a >= 1e18) return n.toExponential(2).replace("+", "");
  for (const [v, u] of units) if (a >= v) return `${(n / v).toFixed(a / v >= 100 ? 0 : a / v >= 10 ? 1 : 2)}${u}`;
  return nf.format(n);
}

export function fmtOdds(odds: number | null | undefined, display?: string | null): string {
  if (display) return display;
  if (!odds) return "—";
  return `1 / ${odds >= 1e15 ? odds.toExponential(2).replace("+", "") : nf.format(Math.round(odds))}`;
}

export function fmtLuck(v: number | null | undefined): string {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  if (v >= 1e6) return `×${fmtCompact(v)}`;
  if (v >= 100) return `×${nf.format(Math.round(v))}`;
  return `×${v.toFixed(2)}`;
}

export function fmtPercent(p: number, digits = 2): string {
  if (p >= 1) return `${p.toFixed(digits)}%`;
  if (p >= 0.01) return `${p.toFixed(Math.max(digits, 3))}%`;
  return `${p.toExponential(2)}%`;
}

export function fmtChance(p: number | null | undefined): string {
  if (!p || p <= 0) return "—";
  return fmtOdds(1 / p);
}

export function fmtDuration(seconds: number): string {
  seconds = Math.max(0, Math.floor(seconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}時間${m}分`;
  if (m > 0) return `${m}分${s.toString().padStart(2, "0")}秒`;
  return `${s}秒`;
}

export function fmtClock(seconds: number): string {
  seconds = Math.max(0, Math.floor(seconds));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 10) return "たった今";
  if (d < 60) return `${Math.floor(d)}秒前`;
  if (d < 3600) return `${Math.floor(d / 60)}分前`;
  if (d < 86400) return `${Math.floor(d / 3600)}時間前`;
  return `${Math.floor(d / 86400)}日前`;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("ja-JP", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export const TIER_KEYS: RarityKey[] = ["common", "common", "rare", "epic", "legendary", "secret", "ultra_secret", "mythic", "admin"];
export const TIER_BY_KEY: Record<RarityKey, number> = {
  common: 1, rare: 2, epic: 3, legendary: 4, secret: 5, ultra_secret: 6, mythic: 7, admin: 8,
};
export const RARITY_LABEL: Record<RarityKey, string> = {
  common: "Common", rare: "Rare", epic: "Epic", legendary: "Legendary", secret: "Secret", ultra_secret: "Ultra Secret",
  mythic: "???", admin: "Admin Artifact",
};

export const FEATURE_LABEL: Record<string, string> = {
  inventory: "インベントリ", collection: "コレクション", biome: "Biome図鑑", profile: "プロフィール", ranking: "ランキング",
  achievements: "実績", shop: "ショップ", auto_delete: "自動削除フィルター", equipment: "装備", crafting: "クラフト",
  auto_roll: "Auto Roll", quests: "クエスト", market: "マーケット", trade: "トレード", gift: "ギフト",
  advanced_rng: "高度なRNG解析", relic_slot: "レリックスロット",
};

export function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v));
}

export function hexToRgb(hex: string): [number, number, number] {
  let h = (hex || "#ffffff").replace("#", "");
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const n = parseInt(h.slice(0, 6), 16);
  if (isNaN(n)) return [255, 255, 255];
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

export function rgba(hex: string, a: number): string {
  const [r, g, b] = hexToRgb(hex);
  return `rgba(${r},${g},${b},${a})`;
}
