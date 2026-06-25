// Thin API client for the FastAPI backend. Phase 1 surface: login + portfolio P&L.

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export interface MonthlyPnL {
  month: string;
  gross_rent: number;
  operating_expenses: number;
  noi: number;
  capex: number;
  debt_service: number;
  other_below_line: number;
  below_noi: number;
  cash_flow: number;
}

export async function login(email: string, password: string): Promise<string> {
  // OAuth2 password flow expects form-encoded `username`/`password`.
  const body = new URLSearchParams({ username: email, password });
  const res = await fetch(`${API_URL}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) throw new Error("Login failed");
  const data = await res.json();
  return data.access_token as string;
}

export async function getPortfolioMonthly(token: string): Promise<MonthlyPnL[]> {
  const res = await fetch(`${API_URL}/portfolio/monthly`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new Error("Failed to load portfolio P&L");
  return res.json();
}
