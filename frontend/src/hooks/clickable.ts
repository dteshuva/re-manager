import type { KeyboardEvent } from "react";

// Shared a11y helper for the "clickable row" pattern used across the drill-down tables
// (unit roster, monthly P&L, breakdown table, attention feed): a bare `onClick` on a
// <tr>/<li> has no keyboard path, no role, and no focus style for a keyboard-only user.
// Spread the returned props onto the element; pass `undefined` when the row isn't
// actually interactive this render (e.g. a single-unit property row) to leave it as a
// plain, non-focusable row.
//
// NOTE: deliberately NOT `use`-prefixed — it's a pure prop factory with no React state,
// and it's called inside `.map()` loops, which the rules-of-hooks lint forbids for real
// hooks.
export function clickableProps(onActivate?: () => void): {
  tabIndex?: number;
  role?: "button";
  onClick?: () => void;
  onKeyDown?: (e: KeyboardEvent) => void;
} {
  if (!onActivate) return {};
  return {
    tabIndex: 0,
    role: "button",
    onClick: onActivate,
    onKeyDown: (e: KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onActivate();
      }
    },
  };
}
