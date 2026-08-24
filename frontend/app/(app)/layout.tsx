import type { ReactNode } from "react";

import { Shell, type NavSection } from "@/components/shell";
import { Callout } from "@/components/ui";
import { can, isAwaitingAccess, requireMe } from "@/lib/session";

export default async function AppLayout({ children }: { children: ReactNode }): Promise<ReactNode> {
  const me = await requireMe();

  const sections: NavSection[] = [
    {
      title: "Work",
      items: [
        { href: "/dashboard", label: "Dashboard" },
        { href: "/proposals", label: "Proposals" },
      ],
    },
  ];

  // Only surface a panel the user can actually open. The backend enforces this regardless;
  // this is about not showing someone a door that will not open.
  const adminItems = [];
  if (can(me, "admin.teams.manage", "all")) {
    adminItems.push({ href: "/admin/teams", label: "Teams & members" });
  }
  if (can(me, "admin.roles.manage", "all")) {
    adminItems.push({ href: "/admin/roles", label: "Roles" });
  }
  if (can(me, "labels.read", "team")) {
    adminItems.push({ href: "/admin/labels", label: "Labels" });
  }
  if (can(me, "rules.read", "team")) {
    adminItems.push({ href: "/admin/rules", label: "Rules" });
    adminItems.push({ href: "/admin/assignment", label: "Assignment policy" });
  }
  if (adminItems.length > 0) {
    sections.push({ title: "Admin", items: adminItems });
  }

  if (can(me, "dev.panel.view", "all")) {
    sections.push({
      title: "Developer",
      items: [
        { href: "/developer", label: "Overview" },
        { href: "/developer/jobs", label: "Jobs" },
        { href: "/developer/connectors", label: "Connectors" },
        { href: "/developer/rule-inspector", label: "Rule inspector" },
        { href: "/developer/audit", label: "Audit log" },
      ],
    });
  }

  return (
    <Shell me={me} sections={sections}>
      {isAwaitingAccess(me) ? (
        <div className="mb-6">
          <Callout tone="warn" title="Waiting for access">
            Your account exists, but you are not in a team yet. An administrator needs to add
            you before you can see any work. Signing in does not grant access on its own.
          </Callout>
        </div>
      ) : null}
      {children}
    </Shell>
  );
}
