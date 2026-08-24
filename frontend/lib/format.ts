/** Formatting helpers shared across screens. */

import type { ProposalStatus } from "./types";

type Tone = "neutral" | "accent" | "good" | "warn" | "danger" | "legacy";

export function statusTone(status: ProposalStatus | string): Tone {
  switch (status) {
    case "won":
    case "success":
      return "good";
    case "lost":
    case "cancelled":
    case "failed":
      return "danger";
    case "submitted":
      return "accent";
    case "in_progress":
    case "running":
      return "warn";
    default:
      return "neutral";
  }
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** Whole days from now until `value`. Negative means it has passed. */
export function daysUntil(value: string | null | undefined): number | null {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const diff = date.getTime() - Date.now();
  return Math.floor(diff / 86_400_000);
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "never";

  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86_400)}d ago`;
}

/** Render a JSON value compactly for a table cell. */
export function summarise(value: unknown, max = 90): string {
  if (value === null || value === undefined) return "—";
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

export function titleise(value: string): string {
  return value
    .replace(/[._]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
