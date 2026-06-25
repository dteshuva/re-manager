import { useState } from "react";
import { getPortfolioMonthly, login, type MonthlyPnL } from "./api";

// Phase 1 frontend scaffold: log in, then render the portfolio monthly P&L table
// straight from the computed views. Dashboards/charts/drill-down come in later phases.
export default function App() {
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState("admin@example.com");
  const [password, setPassword] = useState("admin12345");
  const [rows, setRows] = useState<MonthlyPnL[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const t = await login(email, password);
      setToken(t);
      setRows(await getPortfolioMonthly(t));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const fmt = (n: number) =>
    n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", maxWidth: 900, margin: "2rem auto", padding: "0 1rem" }}>
      <h1>RE Portfolio Manager</h1>

      {!token ? (
        <form onSubmit={handleLogin} style={{ display: "grid", gap: 8, maxWidth: 320 }}>
          <h2>Sign in</h2>
          <input value={email} onChange={(e) => setEmail(e.target.value)} placeholder="Email" />
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
          />
          <button type="submit">Log in</button>
          {error && <p style={{ color: "crimson" }}>{error}</p>}
        </form>
      ) : (
        <>
          <h2>Portfolio — Monthly P&amp;L</h2>
          <table cellPadding={6} style={{ borderCollapse: "collapse", width: "100%" }}>
            <thead>
              <tr style={{ textAlign: "right", borderBottom: "2px solid #333" }}>
                <th style={{ textAlign: "left" }}>Month</th>
                <th>Gross Rent</th>
                <th>Operating</th>
                <th>NOI</th>
                <th>Below-NOI</th>
                <th>Cash Flow</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.month} style={{ textAlign: "right", borderBottom: "1px solid #ddd" }}>
                  <td style={{ textAlign: "left" }}>{r.month}</td>
                  <td>{fmt(r.gross_rent)}</td>
                  <td>{fmt(r.operating_expenses)}</td>
                  <td>{fmt(r.noi)}</td>
                  <td>{fmt(r.below_noi)}</td>
                  <td>{fmt(r.cash_flow)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </main>
  );
}
