/**
 * Navigation.
 *
 * Five destinations across the bottom of the phone, each one a section with
 * its own row of places inside it. Before this, thirteen top-level tabs shared
 * a strip that had to scroll sideways: the game asked players to remember
 * where things were instead of showing them.
 */
import { useEffect, useRef } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { useGame } from "../store/game";
import { audio } from "../audio/engine";

export interface NavItem {
  to: string;
  label: string;
  /** Progression gate key from /api/config unlocks. */
  feature?: string;
  admin?: boolean;
}

export interface NavSection {
  key: string;
  label: string;
  icon: string;
  /** Where the tab itself goes. */
  to: string;
  items: NavItem[];
}

export const SECTIONS: NavSection[] = [
  {
    key: "roll", label: "抽選", icon: "✦", to: "/roll",
    items: [
      { to: "/roll", label: "Roll" },
      { to: "/biomes", label: "Biome", feature: "biome" },
    ],
  },
  {
    key: "book", label: "図鑑", icon: "📖", to: "/collection",
    items: [
      { to: "/collection", label: "図鑑", feature: "collection" },
      { to: "/sets", label: "セット", feature: "collection" },
      { to: "/inventory", label: "所持品", feature: "inventory" },
      { to: "/equipment", label: "装備", feature: "equipment" },
    ],
  },
  {
    key: "shop", label: "商店", icon: "🛒", to: "/shop",
    items: [
      { to: "/shop", label: "商店", feature: "shop" },
      { to: "/shards", label: "星の欠片" },
      { to: "/market", label: "市場", feature: "market" },
    ],
  },
  {
    key: "social", label: "交流", icon: "🌐", to: "/friends",
    items: [
      { to: "/friends", label: "フレンド" },
      { to: "/guild", label: "ギルド" },
      { to: "/trade", label: "取引", feature: "trade" },
      { to: "/ranking", label: "順位", feature: "ranking" },
      { to: "/events", label: "イベント" },
    ],
  },
  {
    key: "me", label: "自分", icon: "👤", to: "/profile",
    items: [
      { to: "/profile", label: "戦績", feature: "profile" },
      { to: "/quests", label: "依頼", feature: "quests" },
      { to: "/achievements", label: "実績", feature: "achievements" },
      { to: "/pass", label: "パス" },
      { to: "/fortune", label: "運勢" },
      { to: "/settings", label: "設定" },
      { to: "/admin", label: "管理", admin: true },
    ],
  },
];

/** Which section a path belongs to — falls back to the first tab. */
export function sectionFor(pathname: string): NavSection {
  let best: { s: NavSection; len: number } | null = null;
  for (const s of SECTIONS) {
    for (const i of s.items) {
      if (pathname === i.to || pathname.startsWith(`${i.to}/`)) {
        if (!best || i.to.length > best.len) best = { s, len: i.to.length };
      }
    }
  }
  return best?.s ?? SECTIONS[0];
}

function useGates() {
  const unlocks = useGame((s) => s.config?.unlocks);
  const level = useGame((s) => s.hud?.level ?? s.me?.user.level ?? 1);
  const isAdmin = useGame((s) => !!s.me?.is_admin);
  return (item: NavItem) => {
    if (item.admin) return isAdmin ? { hidden: false, locked: false, need: 0 } : { hidden: true, locked: true, need: 0 };
    const need = item.feature ? unlocks?.[item.feature] ?? 1 : 1;
    return { hidden: false, locked: level < need, need };
  };
}

/** The row of places inside the current section. Hidden when there is only one. */
export function HubBar() {
  const { pathname } = useLocation();
  const gate = useGates();
  const unread = useGame((s) => s.unread);
  const section = sectionFor(pathname);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    ref.current?.querySelector<HTMLElement>("a.active")?.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
  }, [pathname]);

  const items = section.items.filter((i) => !gate(i).hidden);
  if (items.length < 2) return null;

  return (
    <div className="hub-bar" ref={ref} aria-label={`${section.label}のページ`}>
      {items.map((i) => {
        const g = gate(i);
        // The page you are already on never wears a lock: the page itself
        // explains the gate far better than a padlock on its own tab.
        const here = pathname === i.to || pathname.startsWith(`${i.to}/`);
        return (
          <NavLink key={i.to} to={i.to}
            className={({ isActive }) => `hub-link ${isActive ? "active" : ""} ${g.locked && !here ? "locked" : ""}`}
            title={g.locked ? `Lv.${g.need}で解放` : i.label}
            onClick={(e) => { if (g.locked && !here) e.preventDefault(); else audio.sfx("click"); }}>
            {g.locked && !here && <span aria-hidden>🔒</span>}
            {i.label}
            {i.to === "/profile" && unread > 0 && <span className="dot" />}
          </NavLink>
        );
      })}
    </div>
  );
}

export function BottomNav() {
  const me = useGame((s) => s.me);
  const unread = useGame((s) => s.unread);
  const { pathname } = useLocation();
  const current = sectionFor(pathname);
  if (!me) return null;

  return (
    <nav className="bottom-nav" aria-label="メインナビゲーション">
      {SECTIONS.map((s) => (
        <NavLink key={s.key} to={s.to} className={`tab ${s.key === current.key ? "active" : ""}`}
          onClick={() => audio.sfx("click")} aria-current={s.key === current.key ? "page" : undefined}>
          <span className="ic" aria-hidden>{s.icon}</span>
          <span className="lb">{s.label}</span>
          {s.key === "me" && unread > 0 && <span className="dot" />}
        </NavLink>
      ))}
    </nav>
  );
}
