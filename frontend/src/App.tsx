import { useState } from "react";
import { login } from "./api";
import { btn, btnPrimary, input } from "./ui";
import Dashboard from "./views/Dashboard";
import Entry from "./views/Entry";
import ImportView from "./views/Import";
import Manage from "./views/Manage";
import PropertyDetail from "./views/PropertyDetail";
import Reclassify from "./views/Reclassify";

type Tab = "dashboard" | "property" | "entry" | "import" | "manage" | "reclassify";

const TABS: { id: Tab; label: string }[] = [
  { id: "dashboard", label: "Dashboard" },
  { id: "property", label: "Property" },
  { id: "entry", label: "Data Entry" },
  { id: "import", label: "Import" },
  { id: "manage", label: "Manage" },
  { id: "reclassify", label: "Reclassify" },
];

export default function App() {
  const [token, setToken] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("dashboard");
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("admin12345");
  const [error, setError] = useState<string | null>(null);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      setToken(await login(email, password));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", maxWidth: 960, margin: "2rem auto", padding: "0 1rem" }}>
      <h1>RE Portfolio Manager</h1>

      {!token ? (
        <form onSubmit={handleLogin} style={{ display: "grid", gap: 8, maxWidth: 320 }}>
          <h2>Sign in</h2>
          <input style={input} value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" />
          <input
            style={input}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
          />
          <button style={btnPrimary} type="submit">
            Log in
          </button>
          {error && <p style={{ color: "crimson" }}>{error}</p>}
        </form>
      ) : (
        <>
          <nav style={{ display: "flex", gap: 8, margin: "1rem 0 1.5rem", borderBottom: "1px solid #ddd", paddingBottom: 8 }}>
            {TABS.map((t) => (
              <button
                key={t.id}
                style={t.id === tab ? btnPrimary : btn}
                onClick={() => setTab(t.id)}
              >
                {t.label}
              </button>
            ))}
            <button style={{ ...btn, marginLeft: "auto" }} onClick={() => setToken(null)}>
              Log out
            </button>
          </nav>

          {tab === "dashboard" && <Dashboard token={token} />}
          {tab === "property" && <PropertyDetail token={token} />}
          {tab === "entry" && <Entry token={token} />}
          {tab === "import" && <ImportView token={token} />}
          {tab === "manage" && <Manage token={token} />}
          {tab === "reclassify" && <Reclassify token={token} />}
        </>
      )}
    </main>
  );
}
