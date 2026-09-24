import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { post } from "../../lib/api";
import { useAction, useApi } from "../../lib/useApi";
import { useGame } from "../../store/game";
import { Spinner } from "../../components/ui";
import { AdminDashboard } from "./Dashboard";
import { AdminUsers } from "./Users";
import { AdminContent } from "./Content";
import { AdminSettingsPage } from "./SettingsPanel";
import { AdminArtifacts } from "./Artifacts";
import { AdminLogs } from "./Logs";
import { AdminTools } from "./Tools";
import "./admin.css";

export interface Bootstrap {
  admin_mode: boolean;
  is_super_admin: boolean;
  content_types: {
    key: string; label: string; overridable: boolean; list_fields: string[];
    fields: { name: string; type: string; label: string; required: boolean; choices: string[]; readonly: boolean; help: string; min: number | null; max: number | null }[];
  }[];
  artifacts: any[];
  biomes: { key: string; name: string; kind: string }[];
  items: { key: string; name: string; rarity: string; odds: number | null }[];
  boosts: { key: string; name: string }[];
  effect_types: string[];
}

const TABS = [
  { to: "/admin", end: true, label: "Dashboard", icon: "📊" },
  { to: "/admin/users", label: "Users", icon: "👥" },
  { to: "/admin/content", label: "Content", icon: "📦" },
  { to: "/admin/artifacts", label: "Artifacts", icon: "✨" },
  { to: "/admin/settings", label: "Settings", icon: "⚙" },
  { to: "/admin/logs", label: "Logs", icon: "📜" },
  { to: "/admin/tools", label: "Tools", icon: "🧪" },
];

export function Admin() {
  const me = useGame((s) => s.me);
  const [adminMode, setAdminMode] = useState(!!me?.admin_mode);
  const { run, busy } = useAction();
  const { data: boot, loading, reload } = useApi<Bootstrap>("/api/admin/bootstrap");

  useEffect(() => setAdminMode(!!me?.admin_mode), [me?.admin_mode]);

  const toggle = async (on: boolean) => {
    const res = await run(() => post<{ admin_mode: boolean }>("/api/auth/admin-mode", { enabled: on }));
    if (res) {
      setAdminMode(res.admin_mode);
      useGame.setState((s) => (s.me ? { me: { ...s.me, admin_mode: res.admin_mode } } : {}));
      reload();
    }
  };

  if (!me?.is_admin) return <Navigate to="/roll" replace />;

  return (
    <div className="page admin-page">
      <div className="admin-head glass pad">
        <div className="row" style={{ flexWrap: "wrap", gap: 12 }}>
          <div style={{ flex: 1, minWidth: 200 }}>
            <h1 style={{ color: "var(--gold)" }}>ADMIN PANEL</h1>
            <div className="sub tiny muted">
              すべての操作は監査ログに記録されます · {me.is_super_admin ? "スーパー管理者" : "管理者"}
            </div>
          </div>
          <label className="switch" style={{ gap: 10 }}>
            <input type="checkbox" checked={adminMode} disabled={busy} onChange={(e) => toggle(e.target.checked)} />
            <span className="track" />
            <span style={{ fontWeight: 700, color: adminMode ? "var(--gold)" : "var(--text-dim)" }}>
              {adminMode ? "ADMIN MODE" : "PLAYER MODE"}
            </span>
          </label>
        </div>
      </div>

      <nav className="admin-tabs">
        {TABS.map((t) => (
          <NavLink key={t.to} to={t.to} end={t.end} className={({ isActive }) => (isActive ? "active" : "")}>
            <span>{t.icon}</span>{t.label}
          </NavLink>
        ))}
      </nav>

      {!adminMode ? (
        <div className="glass pad center" style={{ marginTop: 16 }}>
          <div style={{ fontSize: "2rem" }}>🔒</div>
          <h2>Admin Mode が無効です</h2>
          <p className="muted">上のスイッチで Admin Mode を有効にすると管理機能を使用できます。<br />Player Mode では通常プレイヤーとして遊べます。</p>
        </div>
      ) : loading && !boot ? <Spinner /> : boot ? (
        <div style={{ marginTop: 14 }}>
          <Routes>
            <Route index element={<AdminDashboard />} />
            <Route path="users" element={<AdminUsers boot={boot} />} />
            <Route path="content" element={<AdminContent boot={boot} />} />
            <Route path="artifacts" element={<AdminArtifacts boot={boot} />} />
            <Route path="settings" element={<AdminSettingsPage />} />
            <Route path="logs" element={<AdminLogs />} />
            <Route path="tools" element={<AdminTools boot={boot} superAdmin={boot.is_super_admin} />} />
            <Route path="*" element={<Navigate to="/admin" replace />} />
          </Routes>
        </div>
      ) : null}
    </div>
  );
}
