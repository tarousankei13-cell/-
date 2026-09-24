import { memo, useId } from "react";
import type { Visual } from "../lib/types";

/**
 * Renders an item's visual descriptor as layered SVG.
 * The same descriptor is rendered server-side as PNG for Discord notifications.
 */

interface Props {
  visual?: Visual | null;
  size?: number;
  tier?: number;
  silhouette?: boolean;
  animate?: boolean;
  className?: string;
}

function star(cx: number, cy: number, r1: number, r2: number, n = 5, rot = -Math.PI / 2) {
  const pts: string[] = [];
  for (let i = 0; i < n * 2; i++) {
    const r = i % 2 === 0 ? r1 : r2;
    const a = rot + (i * Math.PI) / n;
    pts.push(`${(cx + r * Math.cos(a)).toFixed(2)},${(cy + r * Math.sin(a)).toFixed(2)}`);
  }
  return pts.join(" ");
}

function poly(cx: number, cy: number, r: number, n: number, rot = -Math.PI / 2, sx = 1, sy = 1) {
  return Array.from({ length: n }, (_, i) => {
    const a = rot + (i * 2 * Math.PI) / n;
    return `${(cx + r * sx * Math.cos(a)).toFixed(2)},${(cy + r * sy * Math.sin(a)).toFixed(2)}`;
  }).join(" ");
}

function Shape({ shape, c1, c2, c3, gid }: { shape: string; c1: string; c2: string; c3: string; gid: string }) {
  const cx = 50;
  const cy = 50;
  const f = `url(#${gid}-f)`;
  switch (shape) {
    case "star":
      return <polygon points={star(cx, cy, 34, 14)} fill={f} stroke={c3} strokeWidth="1" strokeLinejoin="round" />;
    case "crystal":
      return (
        <>
          <polygon points={poly(cx, cy, 34, 6, -Math.PI / 2, 0.62)} fill={f} stroke={c3} strokeWidth="1" />
          <polygon points={poly(cx, cy, 18, 6, -Math.PI / 2, 0.62)} fill={c2} opacity="0.7" />
        </>
      );
    case "shard":
      return <polygon points="34,84 50,14 69,66" fill={f} stroke={c3} strokeWidth="1" strokeLinejoin="round" />;
    case "diamond":
      return (
        <>
          <polygon points={poly(cx, cy, 34, 4)} fill={f} stroke={c3} strokeWidth="1" />
          <polygon points={poly(cx, cy, 16, 4)} fill={c3} opacity="0.55" />
        </>
      );
    case "prism":
      return (
        <>
          <polygon points={poly(cx, cy + 4, 34, 3)} fill={f} stroke={c3} strokeWidth="1" />
          <polygon points={poly(cx, cy + 6, 16, 3)} fill={c2} opacity="0.65" />
        </>
      );
    case "cube":
    case "core":
      return (
        <>
          <polygon points={poly(cx, cy, 32, 6, -Math.PI / 6)} fill={c2} stroke={c3} strokeWidth="1" />
          <polygon points="50,50 22,34 50,18 78,34" fill={f} />
          <polygon points="50,50 22,34 22,66 50,82" fill={c1} opacity="0.55" />
        </>
      );
    case "crown":
      return (
        <>
          <path d="M18 66 L18 34 L32 50 L50 26 L68 50 L82 34 L82 66 Z" fill={f} stroke={c3} strokeWidth="1" strokeLinejoin="round" />
          <rect x="18" y="64" width="64" height="10" rx="3" fill={c2} />
          <circle cx="50" cy="22" r="4" fill={c3} />
        </>
      );
    case "ring":
      return (
        <>
          <circle cx={cx} cy={cy} r="30" fill="none" stroke={f} strokeWidth="11" />
          <circle cx={cx} cy={cy} r="30" fill="none" stroke={c3} strokeWidth="1" opacity="0.6" />
          <circle cx={cx} cy={cy} r="7" fill={c3} opacity="0.8" />
        </>
      );
    case "orb":
      return (
        <>
          <circle cx={cx} cy={cy} r="32" fill={f} stroke={c3} strokeWidth="1" />
          <ellipse cx="40" cy="38" rx="11" ry="8" fill="#fff" opacity="0.32" />
        </>
      );
    case "atom":
      return (
        <>
          <ellipse cx={cx} cy={cy} rx="34" ry="13" fill="none" stroke={c1} strokeWidth="2.5" />
          <ellipse cx={cx} cy={cy} rx="34" ry="13" fill="none" stroke={c2} strokeWidth="2.5" transform={`rotate(60 ${cx} ${cy})`} />
          <ellipse cx={cx} cy={cy} rx="34" ry="13" fill="none" stroke={c3} strokeWidth="2.5" transform={`rotate(120 ${cx} ${cy})`} />
          <circle cx={cx} cy={cy} r="9" fill={f} />
        </>
      );
    case "hourglass":
      return (
        <>
          <path d="M28 18 L72 18 L50 50 Z" fill={f} />
          <path d="M28 82 L72 82 L50 50 Z" fill={c2} />
          <rect x="24" y="14" width="52" height="5" rx="2" fill={c3} />
          <rect x="24" y="81" width="52" height="5" rx="2" fill={c3} />
        </>
      );
    case "bolt":
      return <polygon points="56,12 26,54 46,54 40,88 74,42 52,42 62,12" fill={f} stroke={c3} strokeWidth="1" strokeLinejoin="round" />;
    case "tear":
      return <path d="M50 14 C68 40 74 52 74 62 A24 24 0 0 1 26 62 C26 52 32 40 50 14 Z" fill={f} stroke={c3} strokeWidth="1" />;
    case "flame":
      return (
        <>
          <path d="M50 12 C64 34 76 46 76 60 A26 26 0 0 1 24 60 C24 44 38 36 50 12 Z" fill={f} />
          <path d="M50 42 C58 54 62 58 62 65 A12 12 0 0 1 38 65 C38 57 44 54 50 42 Z" fill={c3} opacity="0.75" />
        </>
      );
    case "heart":
      return <path d="M50 82 C22 62 14 46 20 33 A17 17 0 0 1 50 30 A17 17 0 0 1 80 33 C86 46 78 62 50 82 Z" fill={f} stroke={c3} strokeWidth="1" />;
    case "feather":
    case "wing":
      return (
        <>
          <path d="M28 82 C30 46 44 24 72 16 C76 46 62 70 34 80 Z" fill={f} stroke={c3} strokeWidth="1" />
          <path d="M30 80 L70 20" stroke={c3} strokeWidth="1.6" opacity="0.8" />
        </>
      );
    case "flower":
    case "lotus":
      return (
        <>
          {[0, 1, 2, 3, 4].map((i) => (
            <ellipse key={i} cx={cx} cy={cy - 20} rx="10" ry="22" fill={f} opacity="0.88" transform={`rotate(${i * 72} ${cx} ${cy})`} />
          ))}
          <circle cx={cx} cy={cy} r="8" fill={c3} />
        </>
      );
    case "spiral":
      return (
        <path
          d="M50 50 m0 0 a4 4 0 1 1 6 3 a10 10 0 1 1 -14 4 a18 18 0 1 1 26 8 a28 28 0 1 1 -40 -12"
          fill="none"
          stroke={f}
          strokeWidth="5"
          strokeLinecap="round"
        />
      );
    case "galaxy":
      return (
        <>
          <ellipse cx={cx} cy={cy} rx="36" ry="15" fill={f} opacity="0.55" transform={`rotate(-20 ${cx} ${cy})`} />
          <ellipse cx={cx} cy={cy} rx="24" ry="9" fill={c2} opacity="0.7" transform={`rotate(-20 ${cx} ${cy})`} />
          <circle cx={cx} cy={cy} r="7" fill="#fff" />
        </>
      );
    case "blackhole":
      return (
        <>
          <ellipse cx={cx} cy={cy} rx="42" ry="13" fill="none" stroke={c2} strokeWidth="5" opacity="0.85" />
          <circle cx={cx} cy={cy} r="21" fill="#04030a" stroke={c3} strokeWidth="2" />
          <ellipse cx={cx} cy={cy} rx="42" ry="13" fill="none" stroke={c1} strokeWidth="2" opacity="0.6" transform={`rotate(12 ${cx} ${cy})`} />
        </>
      );
    case "planet":
      return (
        <>
          <circle cx={cx} cy={cy} r="26" fill={f} />
          <ellipse cx={cx} cy={cy} rx="42" ry="11" fill="none" stroke={c2} strokeWidth="5" transform={`rotate(-16 ${cx} ${cy})`} />
        </>
      );
    case "moon":
      return (
        <path d="M62 16 A36 36 0 1 0 62 84 A29 29 0 1 1 62 16 Z" fill={f} stroke={c3} strokeWidth="1" />
      );
    case "sun":
      return (
        <>
          {Array.from({ length: 12 }, (_, i) => (
            <line key={i} x1={cx} y1="8" x2={cx} y2="20" stroke={c2} strokeWidth="4" strokeLinecap="round" transform={`rotate(${i * 30} ${cx} ${cy})`} />
          ))}
          <circle cx={cx} cy={cy} r="24" fill={f} />
        </>
      );
    case "comet":
      return (
        <>
          <path d="M20 78 L66 30" stroke={c2} strokeWidth="9" strokeLinecap="round" opacity="0.55" />
          <path d="M28 80 L62 42" stroke={c1} strokeWidth="4" strokeLinecap="round" opacity="0.8" />
          <circle cx="70" cy="28" r="13" fill={f} />
        </>
      );
    case "snowflake":
      return (
        <>
          {Array.from({ length: 6 }, (_, i) => (
            <g key={i} transform={`rotate(${i * 60} ${cx} ${cy})`}>
              <line x1={cx} y1={cy} x2={cx} y2="16" stroke={f} strokeWidth="3.5" strokeLinecap="round" />
              <line x1={cx} y1="28" x2="41" y2="21" stroke={f} strokeWidth="2.5" strokeLinecap="round" />
              <line x1={cx} y1="28" x2="59" y2="21" stroke={f} strokeWidth="2.5" strokeLinecap="round" />
            </g>
          ))}
          <circle cx={cx} cy={cy} r="5" fill={c3} />
        </>
      );
    case "dust":
      return (
        <>
          {[[32, 40, 6], [56, 32, 9], [44, 62, 7], [66, 58, 5], [38, 74, 4], [60, 74, 6]].map(([x, y, r], i) => (
            <circle key={i} cx={x} cy={y} r={r} fill={i % 2 ? c2 : f} opacity={0.55 + i * 0.07} />
          ))}
        </>
      );
    case "key":
      return (
        <>
          <circle cx="36" cy="34" r="16" fill="none" stroke={f} strokeWidth="8" />
          <path d="M45 46 L74 76" stroke={f} strokeWidth="8" strokeLinecap="round" />
          <path d="M62 64 L72 54 M70 72 L80 62" stroke={c2} strokeWidth="6" strokeLinecap="round" />
        </>
      );
    case "gate":
      return (
        <>
          <path d="M22 86 L22 44 A28 28 0 0 1 78 44 L78 86 Z" fill={f} stroke={c3} strokeWidth="1.5" />
          <path d="M36 86 L36 48 A14 14 0 0 1 64 48 L64 86 Z" fill={c2} opacity="0.75" />
        </>
      );
    case "bell":
      return (
        <>
          <path d="M28 70 C28 44 34 26 50 22 C66 26 72 44 72 70 Z" fill={f} stroke={c3} strokeWidth="1" />
          <rect x="24" y="70" width="52" height="7" rx="3" fill={c2} />
          <circle cx="50" cy="83" r="5" fill={c3} />
        </>
      );
    case "eye":
      return (
        <>
          <path d="M12 50 C28 26 72 26 88 50 C72 74 28 74 12 50 Z" fill={c2} stroke={c3} strokeWidth="1.5" />
          <circle cx={cx} cy={cy} r="16" fill={f} />
          <circle cx={cx} cy={cy} r="7" fill="#050310" />
          <circle cx="44" cy="44" r="3.5" fill="#fff" opacity="0.85" />
        </>
      );
    case "mask":
      return (
        <>
          <path d="M22 28 L78 28 C80 56 68 82 50 86 C32 82 20 56 22 28 Z" fill={f} stroke={c3} strokeWidth="1.5" />
          <ellipse cx="38" cy="50" rx="7" ry="5" fill="#050310" />
          <ellipse cx="62" cy="50" rx="7" ry="5" fill="#050310" />
          <path d="M40 68 Q50 74 60 68" stroke={c3} strokeWidth="2.5" fill="none" strokeLinecap="round" />
        </>
      );
    case "compass":
      return (
        <>
          <circle cx={cx} cy={cy} r="32" fill="none" stroke={f} strokeWidth="4" />
          <circle cx={cx} cy={cy} r="24" fill="none" stroke={c2} strokeWidth="1" opacity="0.7" />
          <polygon points="50,24 57,50 50,76 43,50" fill={c2} />
          <polygon points="50,24 57,50 50,50" fill={c3} />
          <circle cx={cx} cy={cy} r="4" fill={c3} />
        </>
      );
    case "anchor":
      return (
        <>
          <circle cx={cx} cy="24" r="9" fill="none" stroke={f} strokeWidth="5" />
          <path d="M50 33 L50 82" stroke={f} strokeWidth="6" strokeLinecap="round" />
          <path d="M26 58 C26 82 74 82 74 58" fill="none" stroke={f} strokeWidth="6" strokeLinecap="round" />
          <path d="M34 44 L66 44" stroke={c2} strokeWidth="5" strokeLinecap="round" />
        </>
      );
    case "shell":
      return (
        <>
          <path d="M50 84 C20 66 16 34 50 16 C84 34 80 66 50 84 Z" fill={f} stroke={c3} strokeWidth="1" />
          {[-24, -12, 0, 12, 24].map((d, i) => (
            <path key={i} d={`M50 84 C${50 + d} 58 ${50 + d * 1.2} 36 50 18`} stroke={c3} strokeWidth="1.2" fill="none" opacity="0.65" />
          ))}
        </>
      );
    case "glove":
      return (
        <>
          <path d="M30 86 L30 44 A8 8 0 0 1 46 44 L46 30 A8 8 0 0 1 62 30 L62 42 A8 8 0 0 1 76 46 L76 70 C76 80 68 86 58 86 Z" fill={f} stroke={c3} strokeWidth="1.4" />
          <path d="M30 60 L76 60" stroke={c2} strokeWidth="3" opacity="0.7" />
        </>
      );
    case "rune":
    case "sigil":
      return (
        <>
          <polygon points={poly(cx, cy, 34, 3)} fill="none" stroke={f} strokeWidth="3.5" />
          <polygon points={poly(cx, cy, 34, 3, Math.PI / 2)} fill="none" stroke={c2} strokeWidth="3.5" opacity="0.85" />
          <circle cx={cx} cy={cy} r="11" fill={c3} opacity="0.85" />
          <circle cx={cx} cy={cy} r="30" fill="none" stroke={c1} strokeWidth="1" opacity="0.5" />
        </>
      );
    default:
      return (
        <>
          <circle cx={cx} cy={cy} r="30" fill={f} stroke={c3} strokeWidth="1" />
          <ellipse cx="41" cy="39" rx="9" ry="6" fill="#fff" opacity="0.28" />
        </>
      );
  }
}

const FX_CLASS: Record<string, string> = {
  pulse: "fx-pulse",
  sparkle: "fx-pulse",
  spin: "fx-spin",
  orbit: "fx-spin",
  rainbow: "fx-rainbow",
  glitch: "fx-glitch",
  flame: "fx-pulse",
  void: "fx-pulse",
  artifact: "fx-pulse",
};

function ItemIconBase({ visual, size = 64, tier = 1, silhouette = false, animate = true, className = "" }: Props) {
  const uid = useId().replace(/:/g, "");
  const colors = visual?.colors ?? ["#c0c8e0", "#7080a8", "#ffffff"];
  const c1 = colors[0] ?? "#c0c8e0";
  const c2 = colors[1] ?? c1;
  const c3 = colors[2] ?? "#ffffff";
  const glow = visual?.glow ?? c1;
  const shape = visual?.shape ?? "orb";
  const fx = visual?.fx ?? "none";
  const fxClass = animate && FX_CLASS[fx] ? FX_CLASS[fx] : "";

  if (silhouette) {
    return (
      <svg width={size} height={size} viewBox="0 0 100 100" className={className} aria-hidden="true">
        <defs>
          <linearGradient id={`${uid}-f`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#2a3150" />
            <stop offset="1" stopColor="#141a2e" />
          </linearGradient>
        </defs>
        <g opacity="0.85">
          <Shape shape={shape} c1="#2a3150" c2="#1d2338" c3="#39415f" gid={uid} />
        </g>
        <text x="50" y="60" textAnchor="middle" fontSize="30" fill="#4a5478" fontWeight="bold">?</text>
      </svg>
    );
  }

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      className={`${fxClass} ${className}`}
      style={{ color: glow, overflow: "visible", filter: tier >= 5 ? `drop-shadow(0 0 ${tier * 1.6}px ${glow})` : undefined }}
      aria-hidden="true"
    >
      <defs>
        <linearGradient id={`${uid}-f`} x1="0.15" y1="0" x2="0.85" y2="1">
          <stop offset="0" stopColor={c1} />
          <stop offset="1" stopColor={c2} />
        </linearGradient>
        <radialGradient id={`${uid}-g`} cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor={glow} stopOpacity={tier >= 6 ? 0.55 : tier >= 4 ? 0.36 : 0.2} />
          <stop offset="1" stopColor={glow} stopOpacity="0" />
        </radialGradient>
      </defs>
      <circle cx="50" cy="50" r="48" fill={`url(#${uid}-g)`} />
      <Shape shape={shape} c1={c1} c2={c2} c3={c3} gid={uid} />
      {tier >= 7 && (
        <circle cx="50" cy="50" r="46" fill="none" stroke={c3} strokeWidth="0.8" opacity="0.55" strokeDasharray="3 6">
          {animate && <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="14s" repeatCount="indefinite" />}
        </circle>
      )}
      {tier >= 8 && (
        <circle cx="50" cy="50" r="40" fill="none" stroke={c1} strokeWidth="1.4" opacity="0.7" strokeDasharray="1 7">
          {animate && <animateTransform attributeName="transform" type="rotate" from="360 50 50" to="0 50 50" dur="9s" repeatCount="indefinite" />}
        </circle>
      )}
    </svg>
  );
}

export const ItemIcon = memo(ItemIconBase);
