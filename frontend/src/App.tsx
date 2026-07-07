import { useEffect, useState } from "react";
import { login } from "./api";
import Dashboard from "./views/Dashboard";
import Entry from "./views/Entry";
import ImportView from "./views/Import";
import Investments from "./views/Investments";
import Manage from "./views/Manage";
import PropertyDetail from "./views/PropertyDetail";
import Reclassify from "./views/Reclassify";

type Tab = "dashboard" | "property" | "investments" | "entry" | "import" | "manage" | "reclassify";

const TABS: { id: Tab; label: string; icon: string; title: string; sub: string }[] = [
  { id: "dashboard", label: "Dashboard", icon: "▤", title: "Portfolio Dashboard", sub: "Consolidated monthly P&L across every property." },
  { id: "property", label: "Property", icon: "▦", title: "Property Detail", sub: "Drill into a single property and its units." },
  { id: "investments", label: "Investments", icon: "◈", title: "Investment Insights", sub: "Cap rate, cash-on-cash and DSCR per property." },
  { id: "entry", label: "Data Entry", icon: "✎", title: "Data Entry", sub: "Record and post monthly line items." },
  { id: "import", label: "Import", icon: "⤓", title: "Bulk Import", sub: "Load CSV or Excel statements." },
  { id: "manage", label: "Manage", icon: "⚙", title: "Manage", sub: "Properties, units and categories." },
  { id: "reclassify", label: "Reclassify", icon: "⇄", title: "Reclassify", sub: "Re-bucket a category and watch NOI recompute." },
];

const STORAGE_TOKEN_KEY = "re_token";
const STORAGE_EMAIL_KEY = "re_email";

export default function App() {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(STORAGE_TOKEN_KEY));
  const [tab, setTab] = useState<Tab>("dashboard");
  const [email, setEmail] = useState(() => localStorage.getItem(STORAGE_EMAIL_KEY) || "admin@example.com");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (token) localStorage.setItem(STORAGE_TOKEN_KEY, token);
    else localStorage.removeItem(STORAGE_TOKEN_KEY);
  }, [token]);

  // Validate stored token on mount; clear it if expired/invalid.
  useEffect(() => {
    const stored = localStorage.getItem(STORAGE_TOKEN_KEY);
    if (!stored) return;
    fetch(`${import.meta.env.VITE_API_URL ?? "http://localhost:8000"}/auth/me`, {
      headers: { Authorization: `Bearer ${stored}` },
    }).then((r) => { if (!r.ok) handleLogout(); });
  }, []);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const t = await login(email, password);
      localStorage.setItem(STORAGE_EMAIL_KEY, email);
      setToken(t);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  function handleLogout() {
    setToken(null);
    setPassword("");
  }

  if (!token) {
    return (
      <div className="auth">
        <div className="auth__card">
          <div className="auth__brand">
            <span className="sidebar__logo">RE</span>
            <div>
              <div className="sidebar__brand-name" style={{ color: "var(--ink)" }}>
                RE Portfolio Manager
              </div>
              <div className="sidebar__brand-sub" style={{ color: "var(--ink-3)" }}>
                Commercial real estate operations
              </div>
            </div>
          </div>

          <h2 className="auth__title">Welcome back</h2>
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>
            Sign in to your portfolio workspace.
          </p>

          <form onSubmit={handleLogin} className="auth__form">
            <div className="auth__field">
              <label>Email</label>
              <input className="input" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" />
            </div>
            <div className="auth__field">
              <label>Password</label>
              <input
                className="input"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
              />
            </div>
            <button className="btn btn-primary" type="submit" style={{ justifyContent: "center", marginTop: 4 }}>
              Sign in
            </button>
            {error && <p className="auth__error">{error}</p>}
          </form>
        </div>
      </div>
    );
  }

  const active = TABS.find((t) => t.id === tab)!;
  const userEmail = email || "admin@example.com";
  const initials = userEmail.slice(0, 2).toUpperCase();

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar__brand">
          <span className="sidebar__logo">RE</span>
          <div>
            <div className="sidebar__brand-name">RE Manager</div>
            <div className="sidebar__brand-sub">Portfolio Operations</div>
          </div>
        </div>

        <nav className="sidebar__nav">
          <div className="sidebar__label">Workspace</div>
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`nav-item${t.id === tab ? " is-active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              <span className="nav-item__icon">{t.icon}</span>
              {t.label}
            </button>
          ))}
        </nav>

        <div className="sidebar__footer">
          <div className="sidebar__user">
            <span className="avatar">{initials}</span>
            <span className="sidebar__user-name">{userEmail}</span>
          </div>
          <button className="nav-item" onClick={handleLogout}>
            <span className="nav-item__icon">⏻</span>
            Log out
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div>
            <div className="topbar__title">{active.title}</div>
            <div className="topbar__sub">{active.sub}</div>
          </div>
        </header>

        <div className="content">
          {tab === "dashboard" && <Dashboard token={token} />}
          {tab === "property" && <PropertyDetail token={token} />}
          {tab === "investments" && <Investments token={token} />}
          {tab === "entry" && <Entry token={token} />}
          {tab === "import" && <ImportView token={token} />}
          {tab === "manage" && <Manage token={token} />}
          {tab === "reclassify" && <Reclassify token={token} />}
        </div>
      </div>
    </div>
  );
}
