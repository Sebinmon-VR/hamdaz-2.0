import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  PageHeader,
  StatusDot,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { daysUntil, formatDate, formatDateTime, statusTone, titleise } from "@/lib/format";
import { can, forwardedHeaders, requireMe } from "@/lib/session";
import type { Candidate, Member, Page as ApiPage, Proposal, ProposalEvent } from "@/lib/types";

import { AssignPanel } from "./assign-panel";

export const metadata: Metadata = { title: "Proposal" };

export default async function ProposalPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<ReactNode> {
  const me = await requireMe();
  const headers = await forwardedHeaders();
  const { id } = await params;

  let proposal: Proposal;
  try {
    proposal = await api.get<Proposal>(`/proposals/${id}`, { headers });
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.isForbidden)) notFound();
    throw error;
  }

  const [timeline, candidateData, members] = await Promise.all([
    api.get<ProposalEvent[]>(`/proposals/${id}/timeline`, { headers }).catch(() => []),
    api
      .get<{ candidates: Candidate[] }>(`/rules/assignment/${proposal.team_id}/candidates`, {
        headers,
      })
      .catch(() => ({ candidates: [] as Candidate[] })),
    api
      .get<ApiPage<Member>>(`/admin/teams/${proposal.team_id}/members`, { headers })
      .then((p) => p.items)
      .catch(() => [] as Member[]),
  ]);

  const nameOf = (userId: string | null): string =>
    userId ? (members.find((m) => m.user_id === userId)?.display_name ?? "Unknown") : "Unassigned";

  const canAssign = can(
    me,
    proposal.assigned_to ? "proposals.reassign" : "proposals.assign",
    "team",
    proposal.team_id,
  );
  const days = daysUntil(proposal.bcd);

  return (
    <>
      <PageHeader
        eyebrow={
          proposal.external_ref ? `Proposal · ${proposal.external_ref}` : "Proposal"
        }
        title={proposal.title}
        description={proposal.customer_name ?? undefined}
        action={
          <Link href="/proposals" className="text-[12.5px] text-accent hover:underline">
            ← All proposals
          </Link>
        }
      />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,360px)]">
        <div className="flex flex-col gap-6">
          <Card>
            <CardHeader title="Details" />
            <CardBody>
              <dl className="grid gap-x-6 gap-y-4 sm:grid-cols-2">
                <Detail label="Status">
                  <Badge tone={statusTone(proposal.status)}>
                    {proposal.status.replace("_", " ")}
                  </Badge>
                </Detail>
                <Detail label="Assigned to">{nameOf(proposal.assigned_to)}</Detail>
                <Detail label="Bid closing date">
                  <span
                    className={
                      days !== null && days < 0
                        ? "text-danger"
                        : days !== null && days <= 3
                          ? "text-warn"
                          : undefined
                    }
                  >
                    {formatDate(proposal.bcd)}
                    {days !== null ? (
                      <span className="ml-2 font-mono text-[11px]">
                        {days < 0 ? `${Math.abs(days)} days over` : `${days} days left`}
                      </span>
                    ) : null}
                  </span>
                </Detail>
                <Detail label="Estimated value">
                  {proposal.estimated_value !== null
                    ? `${proposal.currency ?? ""} ${proposal.estimated_value.toLocaleString()}`.trim()
                    : "—"}
                </Detail>
                <Detail label="Source">
                  <Badge tone={proposal.source === "sharepoint" ? "legacy" : "neutral"}>
                    {proposal.source}
                  </Badge>
                  {proposal.source === "sharepoint" ? (
                    <p className="mt-1 text-[11.5px] text-ink-3">
                      Ingested read-only. Hamdaz never writes back to SharePoint.
                    </p>
                  ) : null}
                </Detail>
                <Detail label="Required skills">
                  {proposal.required_labels.length > 0 ? (
                    <div className="flex flex-wrap gap-1">
                      {proposal.required_labels.map((label) => (
                        <Badge key={label} tone="accent">
                          {label}
                        </Badge>
                      ))}
                    </div>
                  ) : (
                    "—"
                  )}
                </Detail>
              </dl>
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title="Timeline"
              description="Automatic actions link back to the decision that caused them."
            />
            {timeline.length === 0 ? (
              <EmptyState title="Nothing recorded yet" />
            ) : (
              <ol className="flex flex-col">
                {timeline.map((event) => (
                  <li
                    key={event.id}
                    className="flex gap-3 border-b border-line-2 px-5 py-3 last:border-b-0"
                  >
                    <div className="pt-1.5">
                      <StatusDot tone={eventTone(event.type)} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline justify-between gap-2">
                        <span className="text-[13px] font-medium text-ink">
                          {titleise(event.type)}
                        </span>
                        <span className="font-mono text-[10.5px] text-ink-3">
                          {formatDateTime(event.created_at)}
                        </span>
                      </div>
                      {typeof event.payload?.explanation === "string" ? (
                        <p className="mt-0.5 text-[12.5px] leading-relaxed text-ink-2">
                          {event.payload.explanation}
                        </p>
                      ) : null}
                      {typeof event.payload?.reason === "string" ? (
                        <p className="mt-0.5 text-[12.5px] italic text-ink-3">
                          Reason: {event.payload.reason}
                        </p>
                      ) : null}
                      {typeof event.payload?.from === "string" &&
                      typeof event.payload?.to === "string" ? (
                        <p className="mt-0.5 font-mono text-[11.5px] text-ink-3">
                          {event.payload.from} → {event.payload.to}
                        </p>
                      ) : null}
                      {event.rule_evaluation_id ? (
                        <Link
                          href={`/developer/rule-inspector?entity=${proposal.id}`}
                          className="mt-1 inline-block font-mono text-[10.5px] text-accent hover:underline"
                        >
                          why did this happen? →
                        </Link>
                      ) : null}
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </Card>
        </div>

        <div className="flex flex-col gap-6">
          {canAssign ? (
            <AssignPanel
              proposalId={proposal.id}
              candidates={candidateData.candidates}
              currentAssignee={proposal.assigned_to}
              canOverride={canAssign}
            />
          ) : (
            <Card>
              <CardBody>
                <p className="text-[13px] text-ink-3">
                  You do not have permission to assign proposals in this team.
                </p>
              </CardBody>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }): ReactNode {
  return (
    <div>
      <dt className="eyebrow mb-1">{label}</dt>
      <dd className="text-[13.5px] text-ink-2">{children}</dd>
    </div>
  );
}

function eventTone(type: string): "accent" | "good" | "warn" | "neutral" {
  if (type === "assigned" || type === "created") return "accent";
  if (type === "status_changed") return "good";
  if (type === "escalated" || type === "reassigned") return "warn";
  return "neutral";
}
