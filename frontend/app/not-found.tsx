import Link from "next/link";
import type { ReactNode } from "react";

export default function NotFound(): ReactNode {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-3 px-6 text-center">
      <p className="font-mono text-[11px] uppercase tracking-[0.11em] text-ink-3">404</p>
      <h1 className="text-[22px] font-semibold text-ink">Not found</h1>
      <p className="max-w-sm text-[13.5px] leading-relaxed text-ink-2">
        That page does not exist, or you do not have access to it. Those two cases look the
        same deliberately — a 404 should not confirm that something exists.
      </p>
      <Link
        href="/dashboard"
        className="mt-2 rounded-[--radius-sm] bg-accent px-3.5 py-2 text-[13px] font-medium text-white hover:bg-accent-hover"
      >
        Back to dashboard
      </Link>
    </main>
  );
}
