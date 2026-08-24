import type { Metadata } from "next";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { getMe } from "@/lib/session";

export const metadata: Metadata = { title: "Sign in" };

export default async function LoginPage(): Promise<ReactNode> {
  if (await getMe()) redirect("/dashboard");

  return (
    <main className="flex min-h-screen items-center justify-center px-6 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex items-center gap-2">
          <span className="inline-block size-2 rounded-full bg-accent" />
          <span className="font-mono text-[15px] font-bold tracking-tight text-ink">HAMDAZ</span>
          <span className="font-mono text-[11px] text-ink-3">2.0</span>
        </div>

        <h1 className="text-[26px] font-semibold leading-tight text-ink">Sign in</h1>
        <p className="mt-2 text-[13.5px] leading-relaxed text-ink-2">
          Use your Hamdaz Microsoft account. Access to teams and modules is granted by an
          administrator after your first sign-in.
        </p>

        {/* A plain link, not fetch: this is a full-page OIDC redirect to Microsoft, and the
            backend needs to set the PKCE state cookies on the way out. */}
        <a
          href="/api/v1/auth/login"
          className="mt-7 flex h-10 w-full items-center justify-center gap-2 rounded-[--radius-sm] bg-accent px-4 text-[13.5px] font-medium text-white transition-colors hover:bg-accent-hover"
        >
          Continue with Microsoft
        </a>

        <p className="mt-6 border-t border-line pt-4 text-[11.5px] leading-relaxed text-ink-3">
          Trouble signing in? Your account may not be active yet. Ask an administrator to add
          you to a team.
        </p>
      </div>
    </main>
  );
}
