"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState, type ReactNode } from "react";

import { cn } from "@/components/ui";
import type { Me } from "@/lib/types";

export interface NavItem {
  href: string;
  label: string;
  /** Marks routes that are only visible to some roles, for the section legend. */
  restricted?: boolean;
}

export interface NavSection {
  title: string;
  items: NavItem[];
}

/**
 * The application shell.
 *
 * Navigation is built from what the *server* decided this user may see, passed in as
 * `sections`. A link that is not rendered is not a security measure — the backend enforces
 * every route — but showing someone a panel they cannot open is a worse experience than
 * hiding it.
 */
export function Shell({
  me,
  sections,
  children,
}: {
  me: Me;
  sections: NavSection[];
  children: ReactNode;
}): ReactNode {
  const [mobileOpen, setMobileOpen] = useState(false);
  const pathname = usePathname();

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[224px_minmax(0,1fr)]">
      <Sidebar
        me={me}
        sections={sections}
        pathname={pathname}
        mobileOpen={mobileOpen}
        onNavigate={() => setMobileOpen(false)}
      />

      <div className="flex min-w-0 flex-col">
        <TopBar me={me} onToggleMenu={() => setMobileOpen((v) => !v)} />
        <main className="mx-auto w-full max-w-[1180px] flex-1 px-5 py-7 sm:px-7">{children}</main>
      </div>
    </div>
  );
}

function Sidebar({
  me,
  sections,
  pathname,
  mobileOpen,
  onNavigate,
}: {
  me: Me;
  sections: NavSection[];
  pathname: string;
  mobileOpen: boolean;
  onNavigate: () => void;
}): ReactNode {
  return (
    <aside
      className={cn(
        "border-r border-line bg-surface lg:sticky lg:top-0 lg:block lg:h-screen lg:overflow-y-auto",
        mobileOpen ? "block" : "hidden",
      )}
    >
      <div className="flex h-14 items-center gap-2 border-b border-line px-5">
        <span className="inline-block size-2 rounded-full bg-accent" />
        <span className="font-mono text-[13px] font-bold tracking-tight text-ink">HAMDAZ</span>
        <span className="font-mono text-[10px] text-ink-3">2.0</span>
      </div>

      <nav className="px-3 py-4">
        {sections.map((section) => (
          <div key={section.title} className="mb-5">
            <div className="eyebrow px-2 pb-1.5">{section.title}</div>
            <ul className="flex flex-col gap-px">
              {section.items.map((item) => {
                const active =
                  pathname === item.href ||
                  (item.href !== "/dashboard" && pathname.startsWith(`${item.href}/`));
                return (
                  <li key={item.href}>
                    <Link
                      href={item.href}
                      onClick={onNavigate}
                      aria-current={active ? "page" : undefined}
                      className={cn(
                        "flex items-center justify-between rounded-[--radius-sm] px-2 py-1.5 text-[13px] transition-colors",
                        active
                          ? "bg-accent-soft font-medium text-accent-ink"
                          : "text-ink-2 hover:bg-surface-2 hover:text-ink",
                      )}
                    >
                      {item.label}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      <div className="border-t border-line px-5 py-3">
        <div className="truncate text-[12px] font-medium text-ink">{me.display_name}</div>
        <div className="truncate font-mono text-[10.5px] text-ink-3">{me.email}</div>
      </div>
    </aside>
  );
}

function TopBar({ me, onToggleMenu }: { me: Me; onToggleMenu: () => void }): ReactNode {
  return (
    <header className="sticky top-0 z-20 flex h-14 items-center gap-3 border-b border-line bg-paper/85 px-5 backdrop-blur sm:px-7">
      <button
        type="button"
        onClick={onToggleMenu}
        aria-label="Toggle navigation"
        className="rounded-[--radius-sm] border border-line px-2 py-1 text-[12px] text-ink-2 lg:hidden"
      >
        Menu
      </button>

      <div className="flex min-w-0 flex-1 items-center gap-2">
        {me.is_super_admin ? (
          <span className="rounded-[--radius-xs] border border-accent bg-accent-soft px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.06em] text-accent-ink">
            Super admin
          </span>
        ) : null}
        <div className="scroll-x flex min-w-0 items-center gap-1.5">
          {me.teams.map((team) => (
            <span
              key={team.team_id}
              title={`Role: ${team.role}${team.labels.length ? ` · Labels: ${team.labels.join(", ")}` : ""}`}
              className="whitespace-nowrap rounded-[--radius-xs] border border-line bg-surface px-1.5 py-0.5 font-mono text-[10px] text-ink-3"
            >
              {team.slug}
              <span className="ml-1 text-ink-2">{team.role}</span>
            </span>
          ))}
        </div>
      </div>

      <form action="/api/v1/auth/logout" method="post">
        <button
          type="submit"
          className="rounded-[--radius-sm] px-2 py-1 text-[12px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink"
        >
          Sign out
        </button>
      </form>
    </header>
  );
}
