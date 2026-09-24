import { memo, useId } from "react";
import type { Visual } from "../lib/types";

/**
 * Item artwork.
 *
 * An item is described by {shape, colors, glow, fx} and drawn here as layered
 * SVG. Nothing is a flat silhouette: every shape is built from a lit body, a
 * shadow side, an internal structure and a specular highlight, so the same
 * descriptor reads as a solid object rather than a pictogram. The renderer
 * groups the shapes into materials — faceted, spherical, metal, organic,
 * cosmic — because what makes a crystal look like a crystal is the internal
 * reflection, and what makes metal look like metal is the hard bevel; the
 * outline barely matters.
 *
 * The same descriptor is rendered server-side as PNG for Discord.
 */

interface Props {
  visual?: Visual | null;
  size?: number;
  tier?: number;
  silhouette?: boolean;
  animate?: boolean;
  className?: string;
}

// --------------------------------------------------------------- geometry
const r2 = (n: number) => Math.round(n * 100) / 100;

function poly(cx: number, cy: number, r: number, n: number, rot = -Math.PI / 2, sx = 1, sy = 1) {
  return Array.from({ length: n }, (_, i) => {
    const a = rot + (i * 2 * Math.PI) / n;
    return `${r2(cx + r * sx * Math.cos(a))},${r2(cy + r * sy * Math.sin(a))}`;
  }).join(" ");
}

function starPts(cx: number, cy: number, r1: number, ri: number, n = 5, rot = -Math.PI / 2) {
  const pts: string[] = [];
  for (let i = 0; i < n * 2; i++) {
    const r = i % 2 === 0 ? r1 : ri;
    const a = rot + (i * Math.PI) / n;
    pts.push(`${r2(cx + r * Math.cos(a))},${r2(cy + r * Math.sin(a))}`);
  }
  return pts.join(" ");
}

/** Ring of evenly spaced points, for orbiting motes and petal roots. */
function ring(cx: number, cy: number, r: number, n: number, rot = 0) {
  return Array.from({ length: n }, (_, i) => {
    const a = rot + (i * 2 * Math.PI) / n;
    return [cx + r * Math.cos(a), cy + r * Math.sin(a)] as const;
  });
}

// ----------------------------------------------------------------- colour
function hex(c: string): [number, number, number] {
  const m = /^#?([0-9a-f]{6})$/i.exec(c.trim());
  if (!m) return [140, 150, 190];
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function mix(a: string, b: string, t: number): string {
  const [r1, g1, b1] = hex(a);
  const [r2c, g2, b2] = hex(b);
  const h = (x: number) => Math.round(Math.max(0, Math.min(255, x))).toString(16).padStart(2, "0");
  return `#${h(r1 + (r2c - r1) * t)}${h(g1 + (g2 - g1) * t)}${h(b1 + (b2 - b1) * t)}`;
}

const lighten = (c: string, t: number) => mix(c, "#ffffff", t);
const darken = (c: string, t: number) => mix(c, "#05060f", t);

// --------------------------------------------------------------- material
type Mat = "faceted" | "sphere" | "metal" | "organic" | "cosmic";

const MATERIAL: Record<string, Mat> = {
  crystal: "faceted", shard: "faceted", diamond: "faceted", prism: "faceted", cube: "faceted",
  rune: "faceted", sigil: "faceted", snowflake: "faceted",
  orb: "sphere", planet: "sphere", moon: "sphere", sun: "sphere", core: "sphere",
  heart: "sphere", tear: "sphere", shell: "sphere", bell: "sphere", eye: "sphere",
  crown: "metal", ring: "metal", key: "metal", anchor: "metal", compass: "metal",
  gate: "metal", glove: "metal", mask: "metal", hourglass: "metal",
  feather: "organic", wing: "organic", flower: "organic", lotus: "organic", flame: "organic",
  star: "cosmic", galaxy: "cosmic", spiral: "cosmic", blackhole: "cosmic", comet: "cosmic",
  atom: "cosmic", dust: "cosmic", bolt: "cosmic",
};

interface Paint {
  c1: string;
  c2: string;
  c3: string;
  gid: string;
  /** id helpers */
  body: string;
  metal: string;
  sheen: string;
  depth: string;
  core: string;
}

// ------------------------------------------------------------------ parts
/** Specular highlight: the single strongest cue that a thing has volume. */
function Sheen({ cx, cy, rx, ry, rot = -22, o = 0.42 }: { cx: number; cy: number; rx: number; ry: number; rot?: number; o?: number }) {
  return <ellipse cx={cx} cy={cy} rx={rx} ry={ry} fill="#fff" opacity={o} transform={`rotate(${rot} ${cx} ${cy})`} />;
}

/** Light catching the far edge, which separates the object from the background. */
function Rim({ d, c, w = 1.6, o = 0.75 }: { d: string; c: string; w?: number; o?: number }) {
  return <path d={d} fill="none" stroke={c} strokeWidth={w} strokeLinecap="round" opacity={o} />;
}

// ----------------------------------------------------------------- shapes
function Faceted({ shape, p }: { shape: string; p: Paint }) {
  const { c1, c2, c3, body, core } = p;
  const hi = lighten(c1, 0.45);
  const lo = darken(c2, 0.45);

  if (shape === "cube") {
    // An isometric box: three faces at three lightnesses is all a cube needs.
    return (
      <g>
        <polygon points="50,16 80,33 50,50 20,33" fill={hi} />
        <polygon points="20,33 50,50 50,86 20,69" fill={darken(c2, 0.25)} />
        <polygon points="80,33 50,50 50,86 80,69" fill={lo} />
        <polygon points="50,16 80,33 50,50 20,33" fill={`url(#${core})`} opacity="0.5" />
        <path d="M50,16 L80,33 M50,16 L20,33 M50,50 L50,86" stroke={lighten(c3, 0.3)} strokeWidth="0.7" opacity="0.5" fill="none" />
        <Rim d="M20,33 L50,16 L80,33" c={lighten(c3, 0.55)} w={1.3} o={0.8} />
      </g>
    );
  }
  if (shape === "snowflake") {
    const arms = ring(50, 50, 32, 6, -Math.PI / 2);
    return (
      <g>
        <circle cx="50" cy="50" r="30" fill={`url(#${core})`} opacity="0.3" />
        {arms.map(([x, y], i) => (
          <g key={i}>
            <line x1="50" y1="50" x2={r2(x)} y2={r2(y)} stroke={`url(#${body})`} strokeWidth="3.4" strokeLinecap="round" />
            <line x1="50" y1="50" x2={r2(x)} y2={r2(y)} stroke={lighten(c1, 0.6)} strokeWidth="1.1" strokeLinecap="round" opacity="0.8" />
            <line x1={r2(50 + (x - 50) * 0.55)} y1={r2(50 + (y - 50) * 0.55)}
                  x2={r2(50 + (x - 50) * 0.55 + (y - 50) * 0.24)} y2={r2(50 + (y - 50) * 0.55 - (x - 50) * 0.24)}
                  stroke={c3} strokeWidth="1.6" strokeLinecap="round" opacity="0.85" />
            <line x1={r2(50 + (x - 50) * 0.55)} y1={r2(50 + (y - 50) * 0.55)}
                  x2={r2(50 + (x - 50) * 0.55 - (y - 50) * 0.24)} y2={r2(50 + (y - 50) * 0.55 + (x - 50) * 0.24)}
                  stroke={c3} strokeWidth="1.6" strokeLinecap="round" opacity="0.85" />
          </g>
        ))}
        <circle cx="50" cy="50" r="6" fill={lighten(c1, 0.7)} />
        <circle cx="50" cy="50" r="3" fill="#fff" opacity="0.9" />
      </g>
    );
  }
  if (shape === "rune" || shape === "sigil") {
    const n = shape === "rune" ? 6 : 8;
    const inner = ring(50, 50, 22, n, -Math.PI / 2);
    return (
      <g>
        <polygon points={poly(50, 50, 34, n)} fill={`url(#${body})`} stroke={lighten(c3, 0.3)} strokeWidth="1.1" strokeLinejoin="round" />
        <polygon points={poly(50, 50, 34, n)} fill={`url(#${p.depth})`} />
        <circle cx="50" cy="50" r="23" fill="none" stroke={lighten(c3, 0.4)} strokeWidth="1" opacity="0.65" />
        <polygon points={inner.map(([x, y]) => `${r2(x)},${r2(y)}`).join(" ")} fill="none"
                 stroke={c3} strokeWidth="1.2" opacity="0.8" />
        <path d={inner.map(([x, y], i) => `${i ? "L" : "M"}${r2(x)},${r2(y)}`).join("") + "Z"}
              fill="none" stroke={lighten(c1, 0.5)} strokeWidth="0.8" opacity="0.55"
              transform="rotate(22 50 50)" />
        <circle cx="50" cy="50" r="9" fill={`url(#${core})`} />
        <circle cx="50" cy="50" r="4" fill="#fff" opacity="0.85" />
        <Rim d={`M${poly(50, 50, 34, n).split(" ").slice(0, 3).join(" L")}`} c={lighten(c3, 0.6)} w={1.2} o={0.6} />
      </g>
    );
  }
  if (shape === "shard") {
    return (
      <g>
        <polygon points="36,86 50,10 68,64" fill={`url(#${body})`} />
        <polygon points="50,10 68,64 54,70" fill={lo} opacity="0.85" />
        <polygon points="50,10 36,86 45,66" fill={hi} opacity="0.5" />
        <polygon points="50,10 45,66 54,70" fill={`url(#${core})`} opacity="0.7" />
        <Rim d="M50,10 L36,86" c={lighten(c3, 0.6)} w={1.4} o={0.85} />
        <Sheen cx={46} cy={34} rx={2.4} ry={12} rot={6} o={0.3} />
      </g>
    );
  }
  if (shape === "prism") {
    return (
      <g>
        <polygon points="50,12 78,74 22,74" fill={`url(#${body})`} />
        <polygon points="50,12 78,74 50,74" fill={lo} opacity="0.8" />
        <polygon points="50,12 22,74 37,74" fill={hi} opacity="0.45" />
        <polygon points="50,30 64,66 36,66" fill={`url(#${core})`} opacity="0.85" />
        <Rim d="M22,74 L50,12 L78,74" c={lighten(c3, 0.55)} w={1.3} o={0.8} />
        <line x1="50" y1="12" x2="50" y2="74" stroke={lighten(c1, 0.5)} strokeWidth="0.7" opacity="0.5" />
      </g>
    );
  }
  if (shape === "diamond") {
    return (
      <g>
        <polygon points="50,12 82,44 50,88 18,44" fill={`url(#${body})`} />
        <polygon points="50,12 82,44 50,44" fill={hi} opacity="0.55" />
        <polygon points="50,12 18,44 50,44" fill={lighten(c1, 0.75)} opacity="0.5" />
        <polygon points="18,44 50,88 50,44" fill={darken(c2, 0.3)} />
        <polygon points="82,44 50,88 50,44" fill={lo} />
        <polygon points="36,44 64,44 50,66" fill={`url(#${core})`} opacity="0.9" />
        <Rim d="M18,44 L50,12 L82,44" c={lighten(c3, 0.65)} w={1.4} o={0.9} />
        <Sheen cx={40} cy={31} rx={7} ry={3} rot={-28} o={0.5} />
      </g>
    );
  }
  // crystal: a cluster reads richer than a single stone
  return (
    <g>
      <polygon points="30,88 24,48 38,30 46,52 40,88" fill={darken(c2, 0.22)} />
      <polygon points="30,88 24,48 38,30 33,56" fill={hi} opacity="0.4" />
      <polygon points="62,88 56,44 72,24 80,54 74,88" fill={`url(#${body})`} />
      <polygon points="62,88 56,44 72,24 66,52" fill={hi} opacity="0.45" />
      <polygon points="72,24 80,54 74,88 68,60" fill={lo} opacity="0.7" />
      <polygon points="44,90 38,38 54,14 66,46 58,90" fill={`url(#${body})`} />
      <polygon points="54,14 66,46 58,90 52,50" fill={lo} opacity="0.55" />
      <polygon points="38,38 54,14 52,50 46,58" fill={lighten(c1, 0.6)} opacity="0.55" />
      <polygon points="46,44 58,44 52,72" fill={`url(#${core})`} opacity="0.9" />
      <Rim d="M38,38 L54,14 L66,46" c={lighten(c3, 0.6)} w={1.4} o={0.85} />
      <Sheen cx={45} cy={38} rx={2.2} ry={10} rot={10} o={0.32} />
    </g>
  );
}

function Sphere({ shape, p, animate }: { shape: string; p: Paint; animate: boolean }) {
  const { c1, c2, c3, body, sheen, depth, core } = p;

  const globe = (r: number) => (
    <>
      <circle cx="50" cy="50" r={r} fill={`url(#${body})`} />
      <circle cx="50" cy="50" r={r} fill={`url(#${depth})`} />
      <circle cx="50" cy="50" r={r} fill={`url(#${sheen})`} />
      <circle cx="50" cy="50" r={r} fill="none" stroke={lighten(c3, 0.35)} strokeWidth="0.9" opacity="0.5" />
      {/* light wrapping the lower-right limb */}
      <path d={`M${50 - r * 0.72},${50 + r * 0.68} A${r},${r} 0 0 0 ${50 + r * 0.82},${50 + r * 0.55}`}
            fill="none" stroke={lighten(c3, 0.5)} strokeWidth="1.6" opacity="0.5" strokeLinecap="round" />
    </>
  );

  if (shape === "sun") {
    return (
      <g>
        {ring(50, 50, 40, 12).map(([x, y], i) => (
          <line key={i} x1="50" y1="50" x2={r2(x)} y2={r2(y)} stroke={lighten(c1, 0.3)} strokeWidth={i % 2 ? 1.6 : 3}
                strokeLinecap="round" opacity={i % 2 ? 0.35 : 0.6}>
            {animate && <animate attributeName="opacity" values={i % 2 ? "0.2;0.5;0.2" : "0.45;0.8;0.45"} dur={`${2.4 + (i % 3) * 0.6}s`} repeatCount="indefinite" />}
          </line>
        ))}
        <circle cx="50" cy="50" r="27" fill={`url(#${core})`} />
        {globe(24)}
        <circle cx="50" cy="50" r="24" fill={lighten(c1, 0.25)} opacity="0.35" />
        <Sheen cx={41} cy={40} rx={8} ry={5} o={0.55} />
      </g>
    );
  }
  if (shape === "moon") {
    return (
      <g>
        {globe(30)}
        <circle cx="61" cy="43" r="21" fill={darken(c2, 0.55)} opacity="0.85" />
        <circle cx="40" cy="44" r="4.5" fill={darken(c2, 0.3)} opacity="0.7" />
        <circle cx="35" cy="60" r="3" fill={darken(c2, 0.3)} opacity="0.6" />
        <circle cx="47" cy="66" r="2.2" fill={darken(c2, 0.3)} opacity="0.5" />
        <Sheen cx={38} cy={38} rx={7} ry={4} o={0.4} />
      </g>
    );
  }
  if (shape === "planet") {
    return (
      <g>
        <ellipse cx="50" cy="52" rx="45" ry="12" fill="none" stroke={darken(c3, 0.25)} strokeWidth="4" opacity="0.5" transform="rotate(-18 50 52)" />
        {globe(26)}
        <path d="M26,44 Q50,38 74,46" stroke={darken(c2, 0.35)} strokeWidth="3" fill="none" opacity="0.5" />
        <path d="M27,58 Q50,64 73,55" stroke={darken(c2, 0.3)} strokeWidth="2.2" fill="none" opacity="0.4" />
        <ellipse cx="50" cy="52" rx="45" ry="12" fill="none" stroke={lighten(c3, 0.45)} strokeWidth="2" opacity="0.85" transform="rotate(-18 50 52)"
                 strokeDasharray="60 200" strokeDashoffset="-22" />
        <Sheen cx={40} cy={40} rx={7} ry={4.5} o={0.45} />
      </g>
    );
  }
  if (shape === "eye") {
    return (
      <g>
        <path d="M10,50 Q50,16 90,50 Q50,84 10,50 Z" fill={darken(c2, 0.6)} />
        <path d="M13,50 Q50,20 87,50 Q50,80 13,50 Z" fill={`url(#${body})`} />
        <circle cx="50" cy="50" r="19" fill={`url(#${core})`} />
        <circle cx="50" cy="50" r="19" fill={`url(#${depth})`} />
        {ring(50, 50, 14, 10).map(([x, y], i) => (
          <line key={i} x1="50" y1="50" x2={r2(x)} y2={r2(y)} stroke={lighten(c1, 0.45)} strokeWidth="1.3" opacity="0.55" />
        ))}
        <circle cx="50" cy="50" r="8.5" fill={darken(c2, 0.75)} />
        <circle cx="50" cy="50" r="19" fill="none" stroke={lighten(c3, 0.4)} strokeWidth="1.2" opacity="0.8" />
        <path d="M13,50 Q50,20 87,50" fill="none" stroke={lighten(c3, 0.5)} strokeWidth="1.6" opacity="0.7" />
        <Sheen cx={43} cy={44} rx={5} ry={3.4} o={0.75} />
      </g>
    );
  }
  if (shape === "heart") {
    const d = "M50,84 C22,64 14,46 22,34 C30,22 46,24 50,38 C54,24 70,22 78,34 C86,46 78,64 50,84 Z";
    return (
      <g>
        <path d={d} fill={`url(#${body})`} />
        <path d={d} fill={`url(#${depth})`} />
        <path d="M50,84 C22,64 14,46 22,34 C30,22 46,24 50,38 Z" fill={lighten(c1, 0.22)} opacity="0.35" />
        <ellipse cx="50" cy="52" rx="14" ry="16" fill={`url(#${core})`} opacity="0.8" />
        <Rim d="M22,34 C30,22 46,24 50,38" c={lighten(c3, 0.6)} w={1.8} o={0.8} />
        <Sheen cx={36} cy={38} rx={6} ry={4} rot={-30} o={0.55} />
      </g>
    );
  }
  if (shape === "tear") {
    const d = "M50,12 C64,36 76,50 76,62 A26,26 0 0 1 24,62 C24,50 36,36 50,12 Z";
    return (
      <g>
        <path d={d} fill={`url(#${body})`} />
        <path d={d} fill={`url(#${depth})`} />
        <path d="M50,12 C64,36 76,50 76,62 A26,26 0 0 1 62,84 C74,64 62,40 50,12 Z" fill={darken(c2, 0.3)} opacity="0.6" />
        <ellipse cx="48" cy="62" rx="12" ry="14" fill={`url(#${core})`} opacity="0.75" />
        <Rim d="M50,12 C36,36 24,50 24,62" c={lighten(c3, 0.55)} w={1.6} o={0.75} />
        <Sheen cx={41} cy={54} rx={4.5} ry={8} rot={-12} o={0.5} />
      </g>
    );
  }
  if (shape === "shell") {
    return (
      <g>
        <path d="M50,84 C22,76 14,50 22,30 C34,16 66,16 78,30 C86,50 78,76 50,84 Z" fill={`url(#${body})`} />
        {ring(50, 30, 1, 1).map(() => null)}
        {[-30, -15, 0, 15, 30].map((a, i) => (
          <path key={i} d={`M50,84 Q${50 + a * 1.4},50 ${50 + a * 1.8},28`} fill="none"
                stroke={darken(c2, 0.35)} strokeWidth="1.6" opacity="0.55" />
        ))}
        <path d="M50,84 C22,76 14,50 22,30" fill="none" stroke={lighten(c3, 0.5)} strokeWidth="1.5" opacity="0.7" />
        <Sheen cx={40} cy={40} rx={6} ry={10} rot={-20} o={0.35} />
      </g>
    );
  }
  if (shape === "bell") {
    return (
      <g>
        <path d="M32,68 C32,42 38,24 50,20 C62,24 68,42 68,68 Z" fill={`url(#${body})`} />
        <path d="M50,20 C62,24 68,42 68,68 L50,68 Z" fill={darken(c2, 0.28)} />
        <rect x="27" y="66" width="46" height="7" rx="3.5" fill={lighten(c1, 0.2)} />
        <rect x="27" y="66" width="46" height="3" rx="1.5" fill={lighten(c3, 0.5)} opacity="0.7" />
        <circle cx="50" cy="79" r="6" fill={`url(#${core})`} />
        <circle cx="50" cy="17" r="4" fill={lighten(c1, 0.3)} />
        <Rim d="M32,68 C32,42 38,24 50,20" c={lighten(c3, 0.55)} w={1.6} o={0.7} />
        <Sheen cx={41} cy={40} rx={4} ry={11} rot={-6} o={0.35} />
      </g>
    );
  }
  if (shape === "core") {
    return (
      <g>
        <circle cx="50" cy="50" r="34" fill="none" stroke={darken(c2, 0.2)} strokeWidth="5" opacity="0.7" />
        <circle cx="50" cy="50" r="34" fill="none" stroke={lighten(c3, 0.35)} strokeWidth="1.4" opacity="0.6" strokeDasharray="5 9">
          {animate && <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="11s" repeatCount="indefinite" />}
        </circle>
        {globe(22)}
        <circle cx="50" cy="50" r="12" fill={`url(#${core})`}>
          {animate && <animate attributeName="r" values="11;14;11" dur="2.6s" repeatCount="indefinite" />}
        </circle>
        <Sheen cx={42} cy={42} rx={5.5} ry={3.5} o={0.6} />
      </g>
    );
  }
  // orb
  return (
    <g>
      {globe(31)}
      <path d="M29,58 Q50,70 71,56" fill="none" stroke={lighten(c3, 0.3)} strokeWidth="1.2" opacity="0.35" />
      <Sheen cx={39} cy={38} rx={8.5} ry={5.5} o={0.5} />
      <Sheen cx={60} cy={63} rx={4} ry={2.4} rot={20} o={0.18} />
    </g>
  );
}

function Metal({ shape, p, animate }: { shape: string; p: Paint; animate: boolean }) {
  const { c1, c2, c3, metal, core } = p;
  const edge = lighten(c3, 0.5);

  if (shape === "crown") {
    const d = "M18,70 L24,32 L36,48 L50,22 L64,48 L76,32 L82,70 Z";
    return (
      <g>
        <path d={d} fill={`url(#${metal})`} />
        <path d="M18,70 L24,32 L36,48 L50,22 L50,70 Z" fill={lighten(c1, 0.18)} opacity="0.4" />
        <path d={d} fill="none" stroke={edge} strokeWidth="1.5" strokeLinejoin="round" />
        <rect x="18" y="70" width="64" height="9" rx="3" fill={`url(#${metal})`} />
        <rect x="18" y="70" width="64" height="3" rx="1.5" fill={edge} opacity="0.65" />
        <circle cx="50" cy="40" r="6" fill={`url(#${core})`} />
        <circle cx="26" cy="61" r="3.4" fill={c3} opacity="0.9" />
        <circle cx="74" cy="61" r="3.4" fill={c3} opacity="0.9" />
        <circle cx="50" cy="19" r="3.6" fill={edge} />
        <Sheen cx={34} cy={52} rx={2.6} ry={12} rot={-8} o={0.28} />
      </g>
    );
  }
  if (shape === "ring") {
    return (
      <g>
        <circle cx="50" cy="54" r="27" fill="none" stroke={darken(c2, 0.4)} strokeWidth="11" />
        <circle cx="50" cy="54" r="27" fill="none" stroke={`url(#${metal})`} strokeWidth="8" />
        <circle cx="50" cy="54" r="31" fill="none" stroke={edge} strokeWidth="1" opacity="0.55" />
        <circle cx="50" cy="54" r="23" fill="none" stroke={darken(c2, 0.55)} strokeWidth="1.2" opacity="0.8" />
        <path d="M31,36 A27,27 0 0 1 69,36" fill="none" stroke={lighten(c3, 0.6)} strokeWidth="2.4" opacity="0.65" strokeLinecap="round" />
        <polygon points={poly(50, 20, 12, 4)} fill={`url(#${core})`} stroke={edge} strokeWidth="1" />
        <polygon points={poly(50, 20, 5.5, 4)} fill="#fff" opacity="0.6" />
      </g>
    );
  }
  if (shape === "key") {
    return (
      <g>
        <circle cx="34" cy="34" r="17" fill="none" stroke={`url(#${metal})`} strokeWidth="8" />
        <circle cx="34" cy="34" r="17" fill="none" stroke={edge} strokeWidth="1" opacity="0.5" />
        <circle cx="34" cy="34" r="8" fill={`url(#${core})`} opacity="0.8" />
        <rect x="44" y="44" width="8" height="42" rx="3" fill={`url(#${metal})`} transform="rotate(-45 48 65)" />
        <rect x="45" y="44" width="2.4" height="42" rx="1.2" fill={edge} opacity="0.6" transform="rotate(-45 48 65)" />
        <rect x="60" y="60" width="17" height="7" rx="2.5" fill={`url(#${metal})`} transform="rotate(-45 68 63)" />
        <rect x="68" y="70" width="13" height="7" rx="2.5" fill={`url(#${metal})`} transform="rotate(-45 74 73)" />
        <path d="M22,26 A17,17 0 0 1 44,22" fill="none" stroke={lighten(c3, 0.6)} strokeWidth="2" opacity="0.6" strokeLinecap="round" />
      </g>
    );
  }
  if (shape === "hourglass") {
    return (
      <g>
        <rect x="24" y="14" width="52" height="7" rx="3" fill={`url(#${metal})`} />
        <rect x="24" y="79" width="52" height="7" rx="3" fill={`url(#${metal})`} />
        <path d="M32,21 L68,21 L53,50 L68,79 L32,79 L47,50 Z" fill={darken(c2, 0.55)} opacity="0.75" />
        <path d="M34,23 L66,23 L51,50 L50,50 Z" fill={`url(#${core})`} opacity="0.85" />
        <path d="M38,77 L62,77 L52,58 L48,58 Z" fill={`url(#${core})`} opacity="0.95" />
        <line x1="50" y1="50" x2="50" y2="62" stroke={c3} strokeWidth="1.6" opacity="0.9">
          {animate && <animate attributeName="opacity" values="0.3;1;0.3" dur="1.6s" repeatCount="indefinite" />}
        </line>
        <path d="M32,21 L68,21 L53,50 L68,79 L32,79 L47,50 Z" fill="none" stroke={edge} strokeWidth="1.5" strokeLinejoin="round" />
        <rect x="24" y="14" width="52" height="2.6" rx="1.3" fill={edge} opacity="0.7" />
      </g>
    );
  }
  if (shape === "compass") {
    return (
      <g>
        <circle cx="50" cy="50" r="34" fill={`url(#${metal})`} />
        <circle cx="50" cy="50" r="34" fill="none" stroke={edge} strokeWidth="1.4" opacity="0.7" />
        <circle cx="50" cy="50" r="27" fill={darken(c2, 0.62)} />
        {ring(50, 50, 30, 12).map(([x, y], i) => (
          <circle key={i} cx={r2(x)} cy={r2(y)} r={i % 3 === 0 ? 1.8 : 1} fill={edge} opacity={i % 3 === 0 ? 0.9 : 0.5} />
        ))}
        <g>
          {animate && <animateTransform attributeName="transform" type="rotate" values="-14 50 50;16 50 50;-14 50 50" dur="5s" repeatCount="indefinite" />}
          <polygon points="50,24 55,50 50,58 45,50" fill={c3} />
          <polygon points="50,76 55,50 50,42 45,50" fill={lighten(c1, 0.15)} opacity="0.85" />
        </g>
        <circle cx="50" cy="50" r="4" fill={`url(#${core})`} stroke={edge} strokeWidth="0.8" />
        <path d="M28,30 A34,34 0 0 1 64,18" fill="none" stroke={lighten(c3, 0.6)} strokeWidth="2" opacity="0.5" strokeLinecap="round" />
      </g>
    );
  }
  if (shape === "gate") {
    return (
      <g>
        <path d="M22,86 L22,42 A28,28 0 0 1 78,42 L78,86" fill="none" stroke={`url(#${metal})`} strokeWidth="11" />
        <path d="M27,86 L27,42 A23,23 0 0 1 73,42 L73,86 Z" fill={`url(#${core})`} opacity="0.55" />
        <path d="M27,86 L27,42 A23,23 0 0 1 73,42 L73,86" fill="none" stroke={darken(c2, 0.5)} strokeWidth="1.4" />
        <path d="M22,86 L22,42 A28,28 0 0 1 50,14" fill="none" stroke={lighten(c3, 0.55)} strokeWidth="2" opacity="0.6" />
        <rect x="16" y="84" width="68" height="8" rx="3" fill={`url(#${metal})`} />
        <rect x="16" y="84" width="68" height="2.6" rx="1.3" fill={edge} opacity="0.65" />
        <circle cx="50" cy="40" r="5" fill={c3} opacity="0.9" />
      </g>
    );
  }
  if (shape === "anchor") {
    return (
      <g>
        <circle cx="50" cy="22" r="9" fill="none" stroke={`url(#${metal})`} strokeWidth="6" />
        <rect x="46" y="28" width="8" height="52" rx="3" fill={`url(#${metal})`} />
        <rect x="30" y="38" width="40" height="7" rx="3" fill={`url(#${metal})`} />
        <path d="M22,60 Q26,84 50,84 Q74,84 78,60" fill="none" stroke={`url(#${metal})`} strokeWidth="8" strokeLinecap="round" />
        <path d="M22,60 Q26,84 50,84" fill="none" stroke={lighten(c3, 0.5)} strokeWidth="2" opacity="0.5" strokeLinecap="round" />
        <rect x="47" y="28" width="2.4" height="52" rx="1.2" fill={edge} opacity="0.55" />
        <circle cx="50" cy="52" r="5" fill={`url(#${core})`} />
      </g>
    );
  }
  if (shape === "mask") {
    return (
      <g>
        <path d="M22,30 Q50,20 78,30 Q80,58 64,78 Q50,88 36,78 Q20,58 22,30 Z" fill={`url(#${metal})`} />
        <path d="M22,30 Q50,20 50,20 L50,86 Q36,80 36,78 Q20,58 22,30 Z" fill={lighten(c1, 0.16)} opacity="0.35" />
        <path d="M30,46 Q40,40 48,46 Q40,52 30,46 Z" fill={darken(c2, 0.7)} />
        <path d="M70,46 Q60,40 52,46 Q60,52 70,46 Z" fill={darken(c2, 0.7)} />
        <circle cx="39" cy="46" r="2.6" fill={`url(#${core})`} />
        <circle cx="61" cy="46" r="2.6" fill={`url(#${core})`} />
        <path d="M40,66 Q50,72 60,66" fill="none" stroke={darken(c2, 0.6)} strokeWidth="2" strokeLinecap="round" />
        <path d="M22,30 Q50,20 78,30" fill="none" stroke={edge} strokeWidth="1.6" opacity="0.75" />
        <path d="M50,24 L50,38" stroke={c3} strokeWidth="1.4" opacity="0.7" />
      </g>
    );
  }
  // glove / gauntlet
  return (
    <g>
      <path d="M30,84 L30,44 Q30,34 40,34 L62,34 Q72,34 72,44 L72,84 Z" fill={`url(#${metal})`} />
      <path d="M30,84 L30,44 Q30,34 40,34 L50,34 L50,84 Z" fill={lighten(c1, 0.16)} opacity="0.35" />
      {[38, 48, 58, 68].map((x, i) => (
        <rect key={i} x={x - 4} y={i === 3 ? 26 : 20} width="8" height={i === 3 ? 16 : 20} rx="4" fill={`url(#${metal})`} />
      ))}
      {[38, 48, 58, 68].map((x, i) => (
        <rect key={`h${i}`} x={x - 4} y={i === 3 ? 26 : 20} width="2.4" height={i === 3 ? 16 : 20} rx="1.2" fill={edge} opacity="0.5" />
      ))}
      <rect x="27" y="56" width="48" height="6" rx="3" fill={darken(c2, 0.45)} />
      <circle cx="51" cy="70" r="7" fill={`url(#${core})`} stroke={edge} strokeWidth="1" />
      <path d="M30,44 Q30,34 40,34 L62,34" fill="none" stroke={edge} strokeWidth="1.5" opacity="0.7" />
    </g>
  );
}

function Organic({ shape, p, animate }: { shape: string; p: Paint; animate: boolean }) {
  const { c1, c2, c3, body, core } = p;

  if (shape === "flame") {
    return (
      <g>
        <path d="M50,10 C68,34 78,46 78,60 A28,28 0 0 1 22,60 C22,44 34,36 42,22 C44,36 52,38 50,10 Z"
              fill={`url(#${body})`} opacity="0.95">
          {animate && <animate attributeName="opacity" values="0.85;1;0.85" dur="1.8s" repeatCount="indefinite" />}
        </path>
        <path d="M50,28 C62,44 68,52 68,62 A18,18 0 0 1 32,62 C32,52 42,46 46,36 C47,46 52,46 50,28 Z"
              fill={lighten(c1, 0.35)} opacity="0.8" />
        <path d="M50,48 C56,56 58,60 58,65 A8,8 0 0 1 42,65 C42,60 46,57 48,52 Z" fill={`url(#${core})`} />
        <ellipse cx="50" cy="66" rx="9" ry="7" fill="#fff" opacity="0.55" />
      </g>
    );
  }
  if (shape === "feather") {
    const barbs = Array.from({ length: 13 }, (_, i) => 22 + i * 4.6);
    return (
      <g transform="rotate(-16 50 50)">
        <path d="M50,12 C64,34 66,58 56,86 L50,88 L44,86 C34,58 36,34 50,12 Z" fill={`url(#${body})`} />
        <path d="M50,12 C64,34 66,58 56,86 L50,88 Z" fill={darken(c2, 0.25)} opacity="0.55" />
        {barbs.map((y, i) => {
          const w = 15 * Math.sin((Math.PI * (y - 14)) / 78);
          return (
            <g key={i}>
              <line x1="50" y1={y} x2={r2(50 - w)} y2={y + 5} stroke={lighten(c1, 0.35)} strokeWidth="1.1" opacity="0.6" />
              <line x1="50" y1={y} x2={r2(50 + w)} y2={y + 5} stroke={darken(c2, 0.12)} strokeWidth="1.1" opacity="0.5" />
            </g>
          );
        })}
        <line x1="50" y1="14" x2="50" y2="88" stroke={lighten(c3, 0.5)} strokeWidth="1.8" strokeLinecap="round" opacity="0.9" />
        <ellipse cx="50" cy="40" rx="3" ry="14" fill={`url(#${core})`} opacity="0.4" />
      </g>
    );
  }
  if (shape === "wing") {
    return (
      <g>
        <path d="M50,84 C30,74 12,52 14,26 C30,32 42,44 50,60 Z" fill={`url(#${body})`} />
        <path d="M50,84 C70,74 88,52 86,26 C70,32 58,44 50,60 Z" fill={darken(c2, 0.22)} />
        {[0, 1, 2, 3].map((i) => (
          <path key={i} d={`M50,${78 - i * 6} C34,${68 - i * 6} 20,${52 - i * 5} ${20 + i * 3},${32 - i * 2}`}
                fill="none" stroke={lighten(c1, 0.35)} strokeWidth="1.2" opacity="0.5" />
        ))}
        {[0, 1, 2, 3].map((i) => (
          <path key={`r${i}`} d={`M50,${78 - i * 6} C66,${68 - i * 6} 80,${52 - i * 5} ${80 - i * 3},${32 - i * 2}`}
                fill="none" stroke={lighten(c3, 0.2)} strokeWidth="1.2" opacity="0.35" />
        ))}
        <ellipse cx="50" cy="66" rx="5" ry="16" fill={`url(#${core})`} opacity="0.55" />
        <path d="M50,84 C30,74 12,52 14,26" fill="none" stroke={lighten(c3, 0.5)} strokeWidth="1.5" opacity="0.7" />
      </g>
    );
  }
  // flower / lotus
  const petals = shape === "lotus" ? 8 : 6;
  return (
    <g>
      {ring(50, 50, 0, petals).map((_, i) => {
        const a = (i * 360) / petals;
        return (
          <path key={`b${i}`} d="M50,50 C38,36 40,18 50,10 C60,18 62,36 50,50 Z"
                fill={darken(c2, 0.3)} opacity="0.75" transform={`rotate(${a + 180 / petals} 50 50)`} />
        );
      })}
      {ring(50, 50, 0, petals).map((_, i) => {
        const a = (i * 360) / petals;
        return (
          <g key={i} transform={`rotate(${a} 50 50)`}>
            <path d="M50,50 C36,34 38,16 50,6 C62,16 64,34 50,50 Z" fill={`url(#${body})`} />
            <path d="M50,50 C36,34 38,16 50,6 Z" fill={lighten(c1, 0.3)} opacity="0.45" />
            <path d="M50,46 C44,34 45,22 50,14" fill="none" stroke={lighten(c3, 0.45)} strokeWidth="0.9" opacity="0.6" />
          </g>
        );
      })}
      <circle cx="50" cy="50" r="11" fill={`url(#${core})`}>
        {animate && <animate attributeName="r" values="10;12.5;10" dur="3.2s" repeatCount="indefinite" />}
      </circle>
      <circle cx="50" cy="50" r="5" fill="#fff" opacity="0.7" />
      {ring(50, 50, 8, 6).map(([x, y], i) => <circle key={`s${i}`} cx={r2(x)} cy={r2(y)} r="1.4" fill={c3} opacity="0.8" />)}
    </g>
  );
}

function Cosmic({ shape, p, animate }: { shape: string; p: Paint; animate: boolean }) {
  const { c1, c2, c3, body, core } = p;

  if (shape === "blackhole") {
    return (
      <g>
        <ellipse cx="50" cy="50" rx="46" ry="15" fill="none" stroke={`url(#${body})`} strokeWidth="7" opacity="0.85" transform="rotate(-20 50 50)" />
        <ellipse cx="50" cy="50" rx="46" ry="15" fill="none" stroke={lighten(c3, 0.5)} strokeWidth="2" opacity="0.6" transform="rotate(-20 50 50)">
          {animate && <animateTransform attributeName="transform" type="rotate" from="-20 50 50" to="340 50 50" dur="16s" repeatCount="indefinite" additive="sum" />}
        </ellipse>
        <circle cx="50" cy="50" r="24" fill="#05060f" />
        <circle cx="50" cy="50" r="24" fill="none" stroke={`url(#${core})`} strokeWidth="3" opacity="0.9" />
        <circle cx="50" cy="50" r="27" fill="none" stroke={c3} strokeWidth="1" opacity="0.5" />
        <ellipse cx="50" cy="50" rx="46" ry="15" fill="none" stroke={darken(c2, 0.2)} strokeWidth="2.4" opacity="0.5" transform="rotate(-20 50 50) translate(0 6)" />
      </g>
    );
  }
  if (shape === "galaxy" || shape === "spiral") {
    const arms = shape === "galaxy" ? 3 : 2;
    return (
      <g>
        <g>
          {animate && <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur={shape === "galaxy" ? "26s" : "18s"} repeatCount="indefinite" />}
          {Array.from({ length: arms }, (_, i) => (
            <path key={i} d="M50,50 C60,44 74,46 80,58 C86,70 76,84 62,84 C44,84 32,70 32,54 C32,34 50,20 70,22"
                  fill="none" stroke={`url(#${body})`} strokeWidth="6" strokeLinecap="round" opacity="0.7"
                  transform={`rotate(${(i * 360) / arms} 50 50)`} />
          ))}
          {Array.from({ length: arms }, (_, i) => (
            <path key={`h${i}`} d="M50,50 C60,44 74,46 80,58 C86,70 76,84 62,84 C44,84 32,70 32,54 C32,34 50,20 70,22"
                  fill="none" stroke={lighten(c3, 0.45)} strokeWidth="1.4" strokeLinecap="round" opacity="0.5"
                  transform={`rotate(${(i * 360) / arms} 50 50)`} />
          ))}
        </g>
        <circle cx="50" cy="50" r="13" fill={`url(#${core})`} />
        <circle cx="50" cy="50" r="6" fill="#fff" opacity="0.85" />
        {ring(50, 50, 36, 7, 0.4).map(([x, y], i) => <circle key={i} cx={r2(x)} cy={r2(y)} r="1.2" fill={c3} opacity="0.55" />)}
      </g>
    );
  }
  if (shape === "comet") {
    return (
      <g>
        <path d="M78,22 C56,32 34,52 16,84 C48,70 68,50 78,28 Z" fill={`url(#${body})`} opacity="0.55" />
        <path d="M76,26 C58,36 40,54 26,78 C50,66 66,50 76,30 Z" fill={lighten(c1, 0.4)} opacity="0.45" />
        <circle cx="74" cy="26" r="13" fill={`url(#${core})`} />
        <circle cx="74" cy="26" r="7" fill="#fff" opacity="0.85" />
        <circle cx="74" cy="26" r="17" fill="none" stroke={c3} strokeWidth="0.9" opacity="0.4" />
        {[[52, 48], [38, 62], [26, 74]].map(([x, y], i) => (
          <circle key={i} cx={x} cy={y} r={2.6 - i * 0.6} fill={c3} opacity={0.7 - i * 0.15} />
        ))}
      </g>
    );
  }
  if (shape === "atom") {
    return (
      <g>
        {[0, 60, 120].map((a, i) => (
          <ellipse key={i} cx="50" cy="50" rx="38" ry="14" fill="none" stroke={`url(#${body})`} strokeWidth="2.4"
                   opacity="0.8" transform={`rotate(${a} 50 50)`} />
        ))}
        {[0, 60, 120].map((a, i) => (
          <ellipse key={`g${i}`} cx="50" cy="50" rx="38" ry="14" fill="none" stroke={lighten(c3, 0.5)} strokeWidth="0.8"
                   opacity="0.45" transform={`rotate(${a} 50 50)`} />
        ))}
        {[0, 60, 120].map((a, i) => (
          <g key={`e${i}`} transform={`rotate(${a} 50 50)`}>
            <circle cx="88" cy="50" r="3.4" fill={c3}>
              {animate && (
                <animateMotion dur={`${3 + i}s`} repeatCount="indefinite"
                               path="M38,0 A38,14 0 1 1 -38,0 A38,14 0 1 1 38,0" />
              )}
            </circle>
          </g>
        ))}
        <circle cx="50" cy="50" r="11" fill={`url(#${core})`} />
        <circle cx="50" cy="50" r="5" fill="#fff" opacity="0.8" />
      </g>
    );
  }
  if (shape === "bolt") {
    const d = "M58,8 L28,54 L46,54 L40,92 L74,42 L54,42 Z";
    return (
      <g>
        <path d={d} fill={`url(#${body})`} />
        <path d="M58,8 L28,54 L46,54 L44,66 Z" fill={lighten(c1, 0.4)} opacity="0.5" />
        <path d={d} fill="none" stroke={lighten(c3, 0.6)} strokeWidth="1.5" strokeLinejoin="round" opacity="0.9" />
        <path d="M54,18 L36,50 L48,50" fill="none" stroke="#fff" strokeWidth="1.6" opacity="0.5" strokeLinecap="round" />
        <circle cx="50" cy="50" r="30" fill={`url(#${core})`} opacity="0.18" />
      </g>
    );
  }
  if (shape === "dust") {
    const motes = [
      [50, 34, 7], [34, 52, 5], [66, 54, 5.5], [44, 68, 4], [62, 34, 3.4],
      [28, 36, 2.6], [72, 70, 3], [52, 80, 2.4], [38, 24, 2],
    ];
    return (
      <g>
        <circle cx="50" cy="50" r="34" fill={`url(#${core})`} opacity="0.18" />
        {motes.map(([x, y, r], i) => (
          <g key={i}>
            <circle cx={x} cy={y} r={r} fill={`url(#${body})`} />
            <circle cx={x - r * 0.3} cy={y - r * 0.3} r={r * 0.4} fill="#fff" opacity="0.55" />
            {animate && <animate attributeName="opacity" values="0.55;1;0.55" dur={`${2 + (i % 4) * 0.7}s`} repeatCount="indefinite" />}
          </g>
        ))}
      </g>
    );
  }
  // star
  return (
    <g>
      <circle cx="50" cy="50" r="34" fill={`url(#${core})`} opacity="0.22" />
      <polygon points={starPts(50, 50, 40, 15)} fill={`url(#${body})`} />
      <polygon points={starPts(50, 50, 40, 15)} fill="none" stroke={lighten(c3, 0.6)} strokeWidth="1.2" strokeLinejoin="round" opacity="0.85" />
      <polygon points={starPts(50, 50, 22, 8)} fill={lighten(c1, 0.5)} opacity="0.65" />
      <polygon points={starPts(50, 50, 40, 15)} fill={darken(c2, 0.35)} opacity="0.45"
               clipPath="none" transform="scale(1 1)" style={{ clipPath: "polygon(50% 50%, 100% 50%, 100% 100%, 0 100%)" }} />
      <circle cx="50" cy="50" r="7" fill="#fff" opacity="0.8" />
      <Sheen cx={42} cy={40} rx={4} ry={2.6} o={0.6} />
    </g>
  );
}

function Artwork({ shape, p, animate }: { shape: string; p: Paint; animate: boolean }) {
  switch (MATERIAL[shape] ?? "sphere") {
    case "faceted": return <Faceted shape={shape} p={p} />;
    case "metal": return <Metal shape={shape} p={p} animate={animate} />;
    case "organic": return <Organic shape={shape} p={p} animate={animate} />;
    case "cosmic": return <Cosmic shape={shape} p={p} animate={animate} />;
    default: return <Sphere shape={shape} p={p} animate={animate} />;
  }
}

/** Every shape the renderer knows, grouped by material — the admin item editor
 *  builds its shape picker from this, so the panel can never offer a shape that
 *  would fall through to the default sphere. */
export const SHAPES_BY_MATERIAL: Record<Mat, string[]> = (() => {
  const out: Record<Mat, string[]> = { faceted: [], sphere: [], metal: [], organic: [], cosmic: [] };
  for (const [shape, mat] of Object.entries(MATERIAL)) out[mat].push(shape);
  return out;
})();

const FX_CLASS: Record<string, string> = {
  pulse: "fx-pulse", sparkle: "fx-pulse", spin: "fx-spin", orbit: "fx-spin",
  rainbow: "fx-rainbow", glitch: "fx-glitch", flame: "fx-pulse", void: "fx-pulse", artifact: "fx-pulse",
};

export const FX_KEYS = ["none", ...Object.keys(FX_CLASS)];

/** Frame, aura and orbits: the rarity should be legible before the name is read. */
function RarityFrame({ tier, c1, c3, glow, animate }: { tier: number; c1: string; c3: string; glow: string; animate: boolean }) {
  if (tier < 4) return null;
  return (
    <g pointerEvents="none">
      {tier >= 4 && <circle cx="50" cy="50" r="47" fill="none" stroke={glow} strokeWidth="0.9" opacity={0.18 + tier * 0.04} />}
      {tier >= 6 && (
        <circle cx="50" cy="50" r="45" fill="none" stroke={c3} strokeWidth="0.9" opacity="0.5" strokeDasharray="3 7">
          {animate && <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="15s" repeatCount="indefinite" />}
        </circle>
      )}
      {tier >= 7 && (
        <>
          <circle cx="50" cy="50" r="40" fill="none" stroke={c1} strokeWidth="1.3" opacity="0.6" strokeDasharray="1 8">
            {animate && <animateTransform attributeName="transform" type="rotate" from="360 50 50" to="0 50 50" dur="9s" repeatCount="indefinite" />}
          </circle>
          {ring(50, 50, 47, 4, Math.PI / 4).map(([x, y], i) => (
            <circle key={i} cx={r2(x)} cy={r2(y)} r="1.6" fill={c3} opacity="0.85" />
          ))}
        </>
      )}
      {tier >= 8 && (
        <g>
          {ring(50, 50, 49, 6).map(([x, y], i) => (
            <path key={i} d={`M${r2(x)},${r2(y)} l0,-4`} stroke={glow} strokeWidth="2" strokeLinecap="round" opacity="0.9"
                  transform={`rotate(${(i * 360) / 6} 50 50)`} />
          ))}
          <circle cx="50" cy="50" r="49" fill="none" stroke={glow} strokeWidth="1.6" opacity="0.55">
            {animate && <animate attributeName="opacity" values="0.35;0.85;0.35" dur="2.4s" repeatCount="indefinite" />}
          </circle>
        </g>
      )}
    </g>
  );
}

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
    const p: Paint = { c1: "#2b3252", c2: "#171c30", c3: "#39415f", gid: uid,
                       body: `${uid}-b`, metal: `${uid}-m`, sheen: `${uid}-s`, depth: `${uid}-d`, core: `${uid}-c` };
    return (
      <svg width={size} height={size} viewBox="0 0 100 100" className={className} aria-hidden="true">
        <defs>
          <linearGradient id={p.body} x1="0.15" y1="0" x2="0.85" y2="1">
            <stop offset="0" stopColor="#2b3252" /><stop offset="1" stopColor="#141a2e" />
          </linearGradient>
          <linearGradient id={p.metal} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stopColor="#333b5c" /><stop offset="1" stopColor="#151a2c" />
          </linearGradient>
          <radialGradient id={p.sheen} cx="0.35" cy="0.3" r="0.6">
            <stop offset="0" stopColor="#fff" stopOpacity="0.05" /><stop offset="1" stopColor="#fff" stopOpacity="0" />
          </radialGradient>
          <radialGradient id={p.depth} cx="0.5" cy="0.45" r="0.62">
            <stop offset="0.5" stopColor="#000" stopOpacity="0" /><stop offset="1" stopColor="#000" stopOpacity="0.5" />
          </radialGradient>
          <radialGradient id={p.core} cx="0.5" cy="0.5" r="0.5">
            <stop offset="0" stopColor="#3b4468" /><stop offset="1" stopColor="#222842" />
          </radialGradient>
        </defs>
        <g opacity="0.9"><Artwork shape={shape} p={p} animate={false} /></g>
        <text x="50" y="62" textAnchor="middle" fontSize="34" fill="#5b658c" fontWeight="bold" opacity="0.9">?</text>
      </svg>
    );
  }

  const p: Paint = { c1, c2, c3, gid: uid,
                     body: `${uid}-b`, metal: `${uid}-m`, sheen: `${uid}-s`, depth: `${uid}-d`, core: `${uid}-c` };

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      className={`${fxClass} ${className}`}
      style={{ color: glow, overflow: "visible", filter: tier >= 5 ? `drop-shadow(0 0 ${tier * 1.5}px ${glow}aa)` : undefined }}
      aria-hidden="true"
    >
      <defs>
        {/* body: lit face through to the shadow side */}
        <linearGradient id={p.body} x1="0.12" y1="0.02" x2="0.88" y2="1">
          <stop offset="0" stopColor={lighten(c1, 0.32)} />
          <stop offset="0.42" stopColor={c1} />
          <stop offset="1" stopColor={darken(c2, 0.18)} />
        </linearGradient>
        {/* metal: a hard bright band is what separates metal from plastic */}
        <linearGradient id={p.metal} x1="0" y1="0" x2="0.9" y2="1">
          <stop offset="0" stopColor={darken(c2, 0.35)} />
          <stop offset="0.28" stopColor={lighten(c1, 0.55)} />
          <stop offset="0.42" stopColor={c1} />
          <stop offset="0.68" stopColor={darken(c2, 0.3)} />
          <stop offset="0.86" stopColor={lighten(c1, 0.25)} />
          <stop offset="1" stopColor={darken(c2, 0.45)} />
        </linearGradient>
        <radialGradient id={p.sheen} cx="0.34" cy="0.28" r="0.55">
          <stop offset="0" stopColor="#fff" stopOpacity="0.5" />
          <stop offset="0.45" stopColor="#fff" stopOpacity="0.08" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </radialGradient>
        <radialGradient id={p.depth} cx="0.46" cy="0.42" r="0.62">
          <stop offset="0.45" stopColor="#000" stopOpacity="0" />
          <stop offset="1" stopColor="#000" stopOpacity="0.55" />
        </radialGradient>
        <radialGradient id={p.core} cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor={lighten(c3, 0.55)} />
          <stop offset="0.55" stopColor={c3} />
          <stop offset="1" stopColor={mix(c3, c2, 0.75)} />
        </radialGradient>
        <radialGradient id={`${uid}-aura`} cx="0.5" cy="0.5" r="0.5">
          <stop offset="0.35" stopColor={glow} stopOpacity={tier >= 7 ? 0.5 : tier >= 5 ? 0.34 : tier >= 3 ? 0.2 : 0.12} />
          <stop offset="1" stopColor={glow} stopOpacity="0" />
        </radialGradient>
      </defs>
      <circle cx="50" cy="50" r="49" fill={`url(#${uid}-aura)`} />
      <Artwork shape={shape} p={p} animate={animate} />
      <RarityFrame tier={tier} c1={c1} c3={c3} glow={glow} animate={animate} />
    </svg>
  );
}

export const ItemIcon = memo(ItemIconBase);
