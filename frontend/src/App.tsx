import { useEffect, useState } from "react";
import { getMe, login, setUnauthorizedHandler, signup } from "./api";
import type { Me } from "./api";
import { setActiveCurrency } from "./ui";
import Compliance from "./views/Compliance";
import Dashboard from "./views/Dashboard";
import Entry from "./views/Entry";
import ImportView from "./views/Import";
import Investments from "./views/Investments";
import Manage from "./views/Manage";
import PropertyDetail from "./views/PropertyDetail";
import Reclassify from "./views/Reclassify";
import RentRoll from "./views/RentRoll";

type Tab = "dashboard" | "property" | "investments" | "rentroll" | "compliance" | "entry" | "import" | "manage" | "reclassify";

const TABS: { id: Tab; label: string; icon: string; title: string; sub: string }[] = [
  { id: "dashboard", label: "Dashboard", icon: "▤", title: "Portfolio Dashboard", sub: "Consolidated monthly P&L across every property." },
  { id: "property", label: "Property", icon: "▦", title: "Property Detail", sub: "Drill into a single property and its units." },
  { id: "investments", label: "Investments", icon: "◈", title: "Investment Insights", sub: "Cap rate, cash-on-cash and DSCR per property." },
  { id: "rentroll", label: "Rent Roll", icon: "⌂", title: "Rent Roll", sub: "Leases, tenants, contract vs. actual rent, arrears, and rollover risk." },
  { id: "compliance", label: "Compliance", icon: "✔", title: "Compliance", sub: "Track licensing and safety certificates (EICR, Gas/CP12, EPC…) and their expiry." },
  { id: "entry", label: "Data Entry", icon: "✎", title: "Data Entry", sub: "Record and post monthly line items, and split shared bills across properties." },
  { id: "import", label: "Import", icon: "⤓", title: "Bulk Import", sub: "Load CSV or Excel statements." },
  { id: "manage", label: "Manage", icon: "⚙", title: "Manage", sub: "Properties, units and categories." },
  { id: "reclassify", label: "Reclassify", icon: "⇄", title: "Reclassify", sub: "Re-bucket a category and watch NOI recompute." },
];

const STORAGE_TOKEN_KEY = "re_token";
const STORAGE_EMAIL_KEY = "re_email";

export default function App() {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(STORAGE_TOKEN_KEY));
  const [tab, setTab] = useState<Tab>("dashboard");
  // Set by the Investments "missing acquisition data" nudge (or any future cross-tab link):
  // switch to the Property tab pre-selected to a specific property. PropertyDetail consumes
  // and clears it so a later manual property switch isn't overridden.
  const [pendingPropertyId, setPendingPropertyId] = useState<string | null>(null);
  const goToProperty = (propertyId: string) => {
    setPendingPropertyId(propertyId);
    setTab("property");
  };
  const [email, setEmail] = useState(() => localStorage.getItem(STORAGE_EMAIL_KEY) || "");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  // "signin" | "signup" — signing up creates a brand-new, empty ACCOUNT (its own
  // properties/categories/thresholds), not another user inside an existing one.
  const [mode, setMode] = useState<"signin" | "signup">("signin");
  const [accountName, setAccountName] = useState("");
  const [busy, setBusy] = useState(false);
  // Whose portfolio is on screen. Shown in the sidebar so a user with access to more
  // than one login can tell at a glance which account's data they're looking at.
  const [me, setMe] = useState<Me | null>(null);
  // Whether /auth/me has come back yet (success OR failure). Gates the first render — see below.
  const [meResolved, setMeResolved] = useState(false);

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

  // Resolve which account this token acts for. This drives the sidebar's account label AND —
  // more importantly — the account's currency, which is what selects the display locale (UK
  // dates, £) and the UK/US vocabulary in terms.ts.
  useEffect(() => {
    if (!token) {
      setMe(null);
      setMeResolved(false);
      return;
    }
    getMe(token)
      .then((m) => {
        setActiveCurrency(m.account_currency);
        setMe(m);
      })
      .catch(() => setMe(null))
      // Resolved either way: a failed /auth/me must not wedge the app behind a spinner. The
      // currency then stays at its default, which is the behaviour before this gate existed.
      .finally(() => setMeResolved(true));
  }, [token]);

  // Central 401 handling: if the token expires mid-session (a tab left open past expiry),
  // route the user back to login instead of leaving every view to show its own generic
  // error banner. Registered once; the api module calls this on any 401 response.
  useEffect(() => {
    setUnauthorizedHandler(() => handleLogout());
    return () => setUnauthorizedHandler(null);
  }, []);

  async function handleAuth(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const t =
        mode === "signup"
          ? await signup(email, password, accountName)
          : await login(email, password);
      localStorage.setItem(STORAGE_EMAIL_KEY, email);
      setToken(t);
      setPassword("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function switchMode(next: "signin" | "signup") {
    setMode(next);
    setError(null);
    setPassword("");
  }

  function handleLogout() {
    setToken(null);
    setPassword("");
    setMe(null);
  }

  // Hold the shell until /auth/me has answered.
  //
  // Not cosmetic: `setActiveCurrency` is what makes dates render day-first and money render in £
  // (see ui.ts), and it can only run once that request resolves. Rendering the views first meant
  // every figure and every date on the first paint was formatted with the DEFAULT locale —
  // US month-first dates and dollar signs on a British account — and a date like "09/08/2026"
  // formatted under the wrong locale isn't merely ugly, it names a different day. The old code
  // carried a comment claiming the currency was set "before any view formats a figure"; it
  // wasn't, because nothing waited for it. One short wait at startup is the fix.
  if (token && !meResolved) {
    return (
      <div className="auth">
        <div className="auth__card">
          <p className="hint" style={{ margin: 0 }}>Loading…</p>
        </div>
      </div>
    );
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

          <div className="auth__tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "signin"}
              className={`auth__tab${mode === "signin" ? " is-active" : ""}`}
              onClick={() => switchMode("signin")}
            >
              Sign in
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "signup"}
              className={`auth__tab${mode === "signup" ? " is-active" : ""}`}
              onClick={() => switchMode("signup")}
            >
              Create account
            </button>
          </div>

          <h2 className="auth__title">
            {mode === "signup" ? "Create your account" : "Welcome back"}
          </h2>
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>
            {mode === "signup"
              ? "Your account starts empty and is private to you — its own properties, categories and settings."
              : "Sign in to your portfolio workspace."}
          </p>

          <form onSubmit={handleAuth} className="auth__form">
            {mode === "signup" && (
              <label className="auth__field">
                <span>Account name</span>
                <input
                  className="input"
                  id="signup-account"
                  value={accountName}
                  onChange={(e) => setAccountName(e.target.value)}
                  placeholder="Acme Property Group (optional)"
                />
              </label>
            )}
            <label className="auth__field">
              <span>Email</span>
              <input
                className="input"
                id="login-email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
              />
            </label>
            <label className="auth__field">
              <span>Password</span>
              <input
                className="input"
                id="login-password"
                type="password"
                autoComplete={mode === "signup" ? "new-password" : "current-password"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
              />
              {mode === "signup" && (
                <span className="auth__hint">At least 8 characters.</span>
              )}
            </label>
            <button
              className="btn btn-primary"
              type="submit"
              disabled={busy}
              style={{ justifyContent: "center", marginTop: 4 }}
            >
              {busy
                ? mode === "signup"
                  ? "Creating account…"
                  : "Signing in…"
                : mode === "signup"
                  ? "Create account"
                  : "Sign in"}
            </button>
            {error && <p className="auth__error">{error}</p>}
          </form>
        </div>
      </div>
    );
  }

  const active = TABS.find((t) => t.id === tab)!;
  const userEmail = me?.email || email;
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
              aria-current={t.id === tab ? "page" : undefined}
            >
              <span className="nav-item__icon" aria-hidden="true">{t.icon}</span>
              {t.label}
            </button>
          ))}
        </nav>

        <div className="sidebar__footer">
          <div className="sidebar__user">
            <span className="avatar">{initials}</span>
            <span className="sidebar__user-meta">
              <span className="sidebar__user-name">{userEmail}</span>
              {/* Which account's data is on screen — the tenancy boundary made visible. */}
              {me?.account_name && (
                <span className="sidebar__user-account" title={me.account_name}>
                  {me.account_name}
                </span>
              )}
            </span>
          </div>
          <button className="nav-item" onClick={handleLogout}>
            <span className="nav-item__icon" aria-hidden="true">⏻</span>
            Log out
          </button>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div>
            <h1 className="topbar__title">{active.title}</h1>
            <div className="topbar__sub">{active.sub}</div>
          </div>
        </header>

        <div className="content">
          {tab === "dashboard" && <Dashboard token={token} />}
          {tab === "property" && (
            <PropertyDetail
              token={token}
              initialPropertyId={pendingPropertyId}
              onConsumeInitial={() => setPendingPropertyId(null)}
            />
          )}
          {tab === "investments" && <Investments token={token} onGoToProperty={goToProperty} />}
          {tab === "rentroll" && <RentRoll token={token} />}
          {tab === "compliance" && <Compliance token={token} />}
          {tab === "entry" && <Entry token={token} />}
          {tab === "import" && <ImportView token={token} />}
          {tab === "manage" && <Manage token={token} />}
          {tab === "reclassify" && <Reclassify token={token} />}
        </div>
      </div>
    </div>
  );
}
