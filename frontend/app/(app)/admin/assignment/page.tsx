import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import { Callout, Card, EmptyState, PageHeader } from "@/components/ui";
import { api, qs } from "@/lib/api";
import { can, forwardedHeaders, requireMe, teamsWith } from "@/lib/session";
import type { AssignmentPolicy, Candidate, Label, Page as ApiPage, Team } from "@/lib/types";

import { PolicyBuilder } from "./policy-builder";

export const metadata: Metadata = { title: "Assignment policy" };

export default async function AssignmentPage({
  searchParams,
}: {
  searchParams: Promise<{ team?: string }>;
}): Promise<ReactNode> {
  const me = await requireMe();
  const headers = await forwardedHeaders();
  const params = await searchParams;

  const editable = teamsWith(me, "rules.read");
  if (editable.length === 0) {
    return (
      <>
        <PageHeader title="Assignment policy" />
        <Card>
          <EmptyState
            title="No teams available"
            description="You need rules.read in at least one team to configure how work is distributed."
          />
        </Card>
      </>
    );
  }

  const teamId = params.team && editable.includes(params.team) ? params.team : editable[0]!;

  const teams = await api
    .get<ApiPage<Team>>("/admin/teams", { headers })
    .then((page) => page.items)
    .catch(() => []);
  const team = teams.find((t) => t.id === teamId);

  const [policy, labels, candidateData] = await Promise.all([
    api.get<AssignmentPolicy>(`/rules/assignment/${teamId}`, { headers }),
    api.get<Label[]>(`/admin/labels${qs({ team_id: teamId })}`, { headers }).catch(() => []),
    api
      .get<{ candidates: Candidate[] }>(`/rules/assignment/${teamId}/candidates`, { headers })
      .catch(() => ({ candidates: [] })),
  ]);

  return (
    <>
      <PageHeader
        eyebrow="Admin · Rules"
        title="Assignment policy"
        description="How new proposals are distributed. Edit, simulate, then publish — every version is revertible."
        action={
          editable.length > 1 ? (
            <div className="flex flex-wrap gap-1.5">
              {editable.map((id) => {
                const t = teams.find((x) => x.id === id);
                return (
                  <Link
                    key={id}
                    href={`/admin/assignment?team=${id}`}
                    className={
                      id === teamId
                        ? "rounded-[--radius-sm] border border-accent bg-accent-soft px-2.5 py-1 text-[12px] font-medium text-accent-ink"
                        : "rounded-[--radius-sm] border border-line bg-surface px-2.5 py-1 text-[12px] text-ink-2 hover:bg-surface-2"
                    }
                  >
                    {t?.name ?? id.slice(0, 8)}
                  </Link>
                );
              })}
            </div>
          ) : null
        }
      />

      {policy.version > 0 ? (
        <div className="mb-5">
          <Callout tone="accent" title={`Version ${policy.version} is live`}>
            Published policy for {team?.name ?? "this team"}. Changes below take effect only
            when you publish, and any earlier version can be restored.
          </Callout>
        </div>
      ) : null}

      <PolicyBuilder
        teamId={teamId}
        teamName={team?.name ?? "this team"}
        initialPolicy={policy}
        labels={labels}
        candidates={candidateData.candidates}
        canPublish={can(me, "rules.publish", "team", teamId)}
      />
    </>
  );
}
