/**
 * UI primitives.
 *
 * Written here rather than pulled in from a component library: the whole point of the
 * rebuild is one CSS system, and every one of these reads its colours from the tokens in
 * globals.css. Nothing hardcodes a hex value, so light and dark flip together.
 */

import { clsx, type ClassValue } from "clsx";
import Link from "next/link";
import type { ComponentProps, ReactNode } from "react";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

// ── surfaces ────────────────────────────────────────────────────────────

export function Card({
  className,
  children,
  ...props
}: ComponentProps<"div">): ReactNode {
  return (
    <div
      className={cn(
        "rounded-[--radius-md] border border-line bg-surface shadow-[--shadow-card]",
        className,
      )}
      {...props}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  description,
  action,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}): ReactNode {
  return (
    <div
      className={cn(
        "flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-4",
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="text-[15px] font-semibold text-ink">{title}</h2>
        {description ? (
          <p className="mt-0.5 max-w-prose text-[13px] text-ink-2">{description}</p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  );
}

export function CardBody({ className, children }: ComponentProps<"div">): ReactNode {
  return <div className={cn("p-5", className)}>{children}</div>;
}

// ── buttons ─────────────────────────────────────────────────────────────

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
type ButtonSize = "sm" | "md";

const BUTTON_BASE =
  "inline-flex items-center justify-center gap-1.5 rounded-[--radius-sm] font-medium " +
  "transition-colors disabled:pointer-events-none disabled:opacity-50 whitespace-nowrap";

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-accent text-white hover:bg-accent-hover",
  secondary: "border border-line bg-surface text-ink hover:bg-surface-2",
  ghost: "text-ink-2 hover:bg-surface-2 hover:text-ink",
  danger: "border border-danger bg-danger-soft text-danger hover:bg-danger hover:text-white",
};

const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-[12px]",
  md: "h-9 px-3.5 text-[13px]",
};

export function Button({
  variant = "secondary",
  size = "md",
  className,
  ...props
}: ComponentProps<"button"> & { variant?: ButtonVariant; size?: ButtonSize }): ReactNode {
  return (
    <button
      className={cn(BUTTON_BASE, BUTTON_VARIANTS[variant], BUTTON_SIZES[size], className)}
      {...props}
    />
  );
}

export function ButtonLink({
  variant = "secondary",
  size = "md",
  className,
  ...props
}: ComponentProps<typeof Link> & { variant?: ButtonVariant; size?: ButtonSize }): ReactNode {
  return (
    <Link
      className={cn(BUTTON_BASE, BUTTON_VARIANTS[variant], BUTTON_SIZES[size], className)}
      {...props}
    />
  );
}

// ── form controls ───────────────────────────────────────────────────────

const FIELD_BASE =
  "w-full rounded-[--radius-sm] border border-line bg-surface px-2.5 py-1.5 text-[13px] " +
  "text-ink placeholder:text-ink-3 focus:border-accent";

export function Input({ className, ...props }: ComponentProps<"input">): ReactNode {
  return <input className={cn(FIELD_BASE, "h-9", className)} {...props} />;
}

export function Textarea({ className, ...props }: ComponentProps<"textarea">): ReactNode {
  return <textarea className={cn(FIELD_BASE, "min-h-24 font-mono text-[12px]", className)} {...props} />;
}

export function Select({ className, children, ...props }: ComponentProps<"select">): ReactNode {
  return (
    <select className={cn(FIELD_BASE, "h-9 pr-8", className)} {...props}>
      {children}
    </select>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  children,
  className,
}: {
  label: string;
  hint?: ReactNode;
  htmlFor?: string;
  children: ReactNode;
  className?: string;
}): ReactNode {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      <label htmlFor={htmlFor} className="text-[12px] font-medium text-ink">
        {label}
      </label>
      {children}
      {hint ? <p className="text-[11.5px] leading-snug text-ink-3">{hint}</p> : null}
    </div>
  );
}

export function Checkbox({
  label,
  className,
  ...props
}: ComponentProps<"input"> & { label: ReactNode }): ReactNode {
  return (
    <label className={cn("flex cursor-pointer items-center gap-2 text-[13px] text-ink", className)}>
      <input
        type="checkbox"
        className="size-3.5 rounded-[2px] border-line accent-[--color-accent]"
        {...props}
      />
      {label}
    </label>
  );
}

// ── data display ────────────────────────────────────────────────────────

export function Table({ className, children, ...props }: ComponentProps<"table">): ReactNode {
  return (
    <div className="scroll-x">
      <table className={cn("w-full border-collapse text-[13px]", className)} {...props}>
        {children}
      </table>
    </div>
  );
}

export function Th({ className, children, ...props }: ComponentProps<"th">): ReactNode {
  return (
    <th
      className={cn(
        "eyebrow whitespace-nowrap border-b border-line bg-surface-2 px-3.5 py-2.5 text-left",
        className,
      )}
      {...props}
    >
      {children}
    </th>
  );
}

export function Td({ className, children, ...props }: ComponentProps<"td">): ReactNode {
  return (
    <td className={cn("border-b border-line-2 px-3.5 py-2.5 align-top text-ink-2", className)} {...props}>
      {children}
    </td>
  );
}

type Tone = "neutral" | "accent" | "good" | "warn" | "danger" | "legacy";

const TONES: Record<Tone, string> = {
  neutral: "border-line bg-surface-2 text-ink-3",
  accent: "border-accent bg-accent-soft text-accent-ink",
  good: "border-good bg-good-soft text-good",
  warn: "border-warn bg-warn-soft text-warn",
  danger: "border-danger bg-danger-soft text-danger",
  legacy: "border-legacy bg-legacy-soft text-legacy",
};

export function Badge({
  tone = "neutral",
  className,
  children,
}: {
  tone?: Tone;
  className?: string;
  children: ReactNode;
}): ReactNode {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-[--radius-xs] border px-1.5 py-0.5",
        "font-mono text-[10px] font-medium uppercase tracking-[0.06em]",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/** A state read at a glance: colour *and* a dot, never colour alone. */
export function StatusDot({ tone = "neutral" }: { tone?: Tone }): ReactNode {
  const colour: Record<Tone, string> = {
    neutral: "bg-ink-3",
    accent: "bg-accent",
    good: "bg-good",
    warn: "bg-warn",
    danger: "bg-danger",
    legacy: "bg-legacy",
  };
  return <span className={cn("inline-block size-1.5 shrink-0 rounded-full", colour[tone])} />;
}

export function Metric({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: Tone;
}): ReactNode {
  const valueTone =
    tone === "danger" ? "text-danger" : tone === "good" ? "text-good" : "text-ink";
  return (
    <div className="bg-surface p-4">
      <div className={cn("tabular text-2xl font-bold leading-none tracking-tight", valueTone)}>
        {value}
      </div>
      <div className="eyebrow mt-2">{label}</div>
      {hint ? <div className="mt-1 text-[11.5px] text-ink-3">{hint}</div> : null}
    </div>
  );
}

export function MetricGrid({ children }: { children: ReactNode }): ReactNode {
  return (
    <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-px overflow-hidden rounded-[--radius-md] border border-line bg-line">
      {children}
    </div>
  );
}

// ── feedback ────────────────────────────────────────────────────────────

export function Callout({
  tone = "accent",
  title,
  children,
}: {
  tone?: Tone;
  title?: ReactNode;
  children: ReactNode;
}): ReactNode {
  const border: Record<Tone, string> = {
    neutral: "border-l-ink-3",
    accent: "border-l-accent",
    good: "border-l-good",
    warn: "border-l-warn",
    danger: "border-l-danger",
    legacy: "border-l-legacy",
  };
  return (
    <div
      className={cn(
        "rounded-r-[--radius-sm] border border-l-2 border-line bg-surface px-4 py-3",
        border[tone],
      )}
    >
      {title ? <div className="eyebrow mb-1.5">{title}</div> : null}
      <div className="text-[13px] leading-relaxed text-ink-2">{children}</div>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}): ReactNode {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-14 text-center">
      <p className="text-[14px] font-medium text-ink">{title}</p>
      {description ? (
        <p className="max-w-sm text-[13px] leading-relaxed text-ink-2">{description}</p>
      ) : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function PageHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow?: string;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
}): ReactNode {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4 border-b border-line pb-5">
      <div className="min-w-0">
        {eyebrow ? <div className="eyebrow mb-1.5">{eyebrow}</div> : null}
        <h1 className="text-[22px] font-semibold leading-tight text-ink">{title}</h1>
        {description ? (
          <p className="mt-1.5 max-w-prose text-[13.5px] leading-relaxed text-ink-2">
            {description}
          </p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </header>
  );
}

/** A horizontal proportion bar. Used for workload and ratio drift. */
export function Bar({
  value,
  max,
  tone = "accent",
}: {
  value: number;
  max: number;
  tone?: Tone;
}): ReactNode {
  const pct = max > 0 ? Math.min(100, Math.max(0, (value / max) * 100)) : 0;
  const fill: Record<Tone, string> = {
    neutral: "bg-ink-3",
    accent: "bg-accent",
    good: "bg-good",
    warn: "bg-warn",
    danger: "bg-danger",
    legacy: "bg-legacy",
  };
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
      <div className={cn("h-full rounded-full", fill[tone])} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Mono({ className, children }: { className?: string; children: ReactNode }): ReactNode {
  return <span className={cn("font-mono text-[12px] text-ink-2", className)}>{children}</span>;
}
