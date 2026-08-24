import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  PageHeader,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api } from "@/lib/api";
import { can, forwardedHeaders, requireMe } from "@/lib/session";
import type { Page as ApiPage, Team } from "@/lib/types";

import { CreateTeam } from "./create-team";

export const metadata: Metadata = { title: "Teams" };

export default async function TeamsPage(): Promise<ReactNode> {
  const me = await requireMe();
  const headers = await forwardedHeaders();

  const [teams, modules] = await Promise.all([
    api.get<ApiPage<Team>>("/admin/teams?include_archived=true", { headers }),
    api.get<string[]>("/admin/teams/modules", { headers }).catch(() => [] as string[]),
  ]);

  const canManage = can(me, "admin.teams.manage", "all");

  return (
    <>
      <PageHeader
        eyebrow="Admin"
        title="Teams"
        description="Each team enables its own modules and carries its own roles, labels and policies."
        action={canManage ? <CreateTeam modules={modules} /> : null}
      />

      <Card>
        <CardHeader title="All teams" description={`${teams.total} total.`} />
        {teams.items.length === 0 ? (
          <EmptyState
            title="No teams yet"
            description="Create the first team, then add people to it. A user who signs in without a team sees nothing."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Team</Th>
                <Th>Modules</Th>
                <Th className="num text-right">Members</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {teams.items.map((team) => (
                <tr key={team.id} className={team.archived ? "opacity-55" : undefined}>
                  <Td>
                    <div className="flex items-center gap-2">
                      <Link
                        href={`/admin/teams/${team.id}`}
                        className="font-medium text-ink hover:text-accent"
                      >
                        {team.name}
                      </Link>
                      {team.archived ? <Badge tone="neutral">archived</Badge> : null}
                    </div>
                    <div className="font-mono text-[10.5px] text-ink-3">{team.slug}</div>
                    {team.description ? (
                      <p className="mt-0.5 max-w-md text-[12px] text-ink-3">{team.description}</p>
                    ) : null}
                  </Td>
                  <Td>
                    {team.enabled_modules.length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {team.enabled_modules.map((module) => (
                          <Badge key={module} tone="accent">
                            {module}
                          </Badge>
                        ))}
                      </div>
                    ) : (
                      <span className="text-ink-3">none enabled</span>
                    )}
                  </Td>
                  <Td className="num text-right">{team.member_count ?? "—"}</Td>
                  <Td className="text-right">
                    <Link
                      href={`/admin/teams/${team.id}`}
                      className="whitespace-nowrap text-[12.5px] text-accent hover:underline"
                    >
                      Members →
                    </Link>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
