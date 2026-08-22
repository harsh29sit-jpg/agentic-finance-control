import { NavLink, useNavigate } from "react-router-dom";
import {
  LayoutDashboard, Layers, GitCompareArrows, AlertTriangle, FileBarChart2,
  ScrollText, Sparkles, ShieldCheck, Gauge, Moon, Sun, LogOut, CircleUser,
} from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import { useTheme } from "@/context/ThemeContext";
import { cn } from "@/lib/utils";
import AgentChatWidget from "@/components/AgentChatWidget";

const NAV = [
  { to: "/copilot", label: "Copilot · Agents", icon: Sparkles },
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/batches", label: "Batches", icon: Layers },
  { to: "/workbench", label: "Reconciliation Workbench", icon: GitCompareArrows },
  { to: "/exceptions", label: "Exceptions", icon: AlertTriangle },
  { to: "/reports", label: "Reports", icon: FileBarChart2 },
  { to: "/evaluation", label: "Evaluation", icon: Gauge },
  { to: "/audit", label: "Audit Logs", icon: ScrollText },
  { to: "/admin", label: "Admin / Policies", icon: ShieldCheck, roles: ["admin", "controller"] },
];

export const AppShell = ({ children }) => {
  const { user, logout, meta } = useAuth();
  const { theme, toggle } = useTheme();
  const navigate = useNavigate();

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Sidebar */}
      <aside className="flex w-60 shrink-0 flex-col bg-sidebar text-white" data-testid="app-sidebar">
        <div className="flex items-center gap-2.5 border-b border-white/[0.08] px-4 py-4">
          <div className="flex h-8 w-8 items-center justify-center rounded bg-gradient-to-br from-[#0d94fb] to-[#0768b3] font-bold shadow-[0_2px_8px_rgba(13,148,251,0.35)]">R</div>
          <div className="leading-tight">
            <div className="text-sm font-bold">Recon Control Tower</div>
            <div className="text-[10px] uppercase tracking-widest text-white/40">Settlement Ops</div>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto px-2 py-3">
          <div className="mb-1 px-3 text-[9px] font-bold uppercase tracking-[0.14em] text-white/30">Operations</div>
          {NAV.filter((item) => !item.roles || item.roles.includes(user?.role)).map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                data-testid={`nav-${item.label.toLowerCase().replace(/[^a-z]+/g, "-").replace(/^-|-$/g, "")}`}
                className={({ isActive }) =>
                  cn(
                    "mb-0.5 flex items-center gap-2.5 rounded px-3 py-[7px] text-[13px] font-semibold transition-colors",
                    isActive
                      ? "bg-[#0d94fb] text-white"
                      : "text-white/60 hover:bg-white/[0.06] hover:text-white/90"
                  )
                }
              >
                <Icon size={16} strokeWidth={2} />
                <span className="truncate">{item.label}</span>
              </NavLink>
            );
          })}
        </nav>

        <div className="border-t border-white/[0.08] p-3">
          <div className="flex items-center gap-2 rounded bg-white/[0.05] px-2.5 py-2">
            <CircleUser size={26} className="text-white/60" />
            <div className="min-w-0 flex-1 leading-tight">
              <div className="truncate text-xs font-semibold">{user?.name}</div>
              <div className="truncate text-[10px] uppercase tracking-wide text-[#5cb8ff]">
                {meta.labels?.[user?.role] || user?.role}
              </div>
            </div>
            <button
              data-testid="logout-btn"
              onClick={async () => { await logout(); navigate("/login"); }}
              className="rounded p-1.5 text-white/50 transition-colors hover:bg-white/[0.08] hover:text-white"
              title="Sign out"
            >
              <LogOut size={15} />
            </button>
          </div>
        </div>
      </aside>

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-background px-5">
          <div className="text-xs text-muted-foreground">
            Deterministic-first reconciliation · <span className="font-mono">Source A · B · C</span>
          </div>
          <button
            data-testid="theme-toggle"
            onClick={toggle}
            className="flex items-center gap-1.5 rounded border border-border bg-card px-2.5 py-1 text-xs font-semibold text-muted-foreground transition-colors hover:border-brand hover:text-brand"
          >
            {theme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
            {theme === "dark" ? "Light" : "Dark"}
          </button>
        </header>
        <main className="flex-1 overflow-y-auto">{children}</main>
      </div>

      {/* global agentic assistant */}
      <AgentChatWidget />
    </div>
  );
};

export default AppShell;
