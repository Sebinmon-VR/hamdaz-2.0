import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
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
import { ApiError, api, qs } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { can, forwardedHeaders, requireMe } from "@/lib/session";
import type { Label, Member, Page as ApiPage, SystemRole, Team } from "@/lib/types";

import { MemberControls } from "./member-controls";

export const metadata: Metadata = { title: "Team members" };

export default async function TeamDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<ReactNode> {
  const me = await requireMe();
  const headers = await forwardedHeaders();
  const { id } = await params;

  let team: Team;
  try {
    team = await api.get<Team>(`/admin/teams/${id}`, { headers });
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.isForbidden)) notFound();
    throw error;
  }

  const [members, roles, labels] = await Promise.all([
    api.get<ApiPage<Member>>(`/admin/teams/${id}/members?limit=200`, { headers }),
    api.get<SystemRole[]>("/meta/roles", { headers }).catch(() => [] as SystemRole[]),
    api.get<Label[]>(`/admin/labels${qs({ team_id: id })}`, { headers }).catch(() => [] as Label[]),
  ]);

  const canManageUsers = can(me, "admin.users.manage", "all");
  const canAssignLabels = can(me, "labels.assign", "team", id);

  return (
    <>
      <PageHeader
        eyebrow="Admin · Teams"
        title={team.name}
        description={team.description ?? undefined}
        action={
          <Link href="/admin/teams" className="text-[12.5px] text-accent hover:underline">
            ← All teams
          </Link>
        }
      />

      <div className="mb-5 flex flex-wrap gap-2">
        <Badge tone="neutral">{team.slug}</Badge>
        {team.enabled_modules.map((module) => (
          <Badge key={module} tone="accent">
            {module}
          </Badge>
        ))}
        {team.archived ? <Badge tone="danger">archived</Badge> : null}
      </div>

      <Card>
        <CardHeader
          title="Members"
          description="Role grants permission. Labels drive how the assignment policy treats someone — two separate things."
        />
        {members.items.length === 0 ? (
          <EmptyState
            title="No members yet"
            description="Add someone below. Until an admin does, a user who signs in sees nothing."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Member</Th>
                <Th>Role</Th>
                <Th>Labels</Th>
                <Th>Joined</Th>
                {canManageUsers || canAssignLabels ? <Th /> : null}
              </tr>
            </thead>
            <tbody>
              {members.items.map((member) => (
                <tr key={member.user_id}>
                  <Td>
                    <div className="font-medium text-ink">{member.display_name}</div>
                    <div className="font-mono text-[10.5px] text-ink-3">{member.email}</div>
                    {member.status !== "active" ? (
                      <Badge tone="warn" className="mt-1">
                        {member.status}
                      </Badge>
                    ) : null}
                  </Td>
                  <Td>
                    <Badge tone={member.role === "super_admin" ? "danger" : "neutral"}>
                      {member.role}
                    </Badge>
                  </Td>
                  <Td>
                    {member.labels.length > 0 ? (
                      <div className="flex flex-wrap gap-1">
                        {member.labels.map((label) => (
                          <Badge key={label} tone="accent">
                            {label}
                          </Badge>
                        ))}
                      </div>
                    ) : (
                      <span className="text-ink-3">—</span>
                    )}
                  </Td>
                  <Td className="whitespace-nowrap">{formatDate(member.joined_at)}</Td>
                  {canManageUsers || canAssignLabels ? (
                    <Td>
                      <MemberControls
                        teamId={id}
                        member={member}
                        roles={roles}
                        labels={labels}
                        canManageUsers={canManageUsers}
                        canAssignLabels={canAssignLabels}
                      />
                    </Td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </>
  );
}
