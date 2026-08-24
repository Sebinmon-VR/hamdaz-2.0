import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import {
  Badge,
  Bar,
  Card,
  CardHeader,
  EmptyState,
  Metric,
  MetricGrid,
  PageHeader,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { ApiError, api, qs } from "@/lib/api";
import { can, forwardedHeaders, requireMe, teamsWith } from "@/lib/session";
import type { Page, Proposal, WorkloadRow } from "@/lib/types";
import { statusTone, formatDate, daysUntil } from "@/lib/format";

export const metadata: Metadata = { title: "Dashboard" };

export default async function DashboardPage(): Promise<ReactNode> {
  const me = await requireMe();
  const headers = await forwardedHeaders();

  // Workload needs reports.read_team, which a plain member does not hold. Absence is a
  // normal state here, not an error.
  const workloadTeam = teamsWith(me, "reports.read_team")[0];

  // Awaited together, not in sequence. None of these three depends on another, and each is a
  // separate HTTP call to the backend — chaining them made the page cost their sum. Every
  // .catch() is per-request, so one failure still cannot take the others down with it.
  const [mine, teamProposals, workload] = await Promise.all([
    api
      .get<Page<Proposal>>(
        `/proposals/${qs({ assigned_to: me.user_id, open_only: true, limit: 50 })}`,
        { headers },
      )
      .catch(() => null),
    api
      .get<Page<Proposal>>(`/proposals/${qs({ open_only: true, limit: 100 })}`, { headers })
      .catch(() => null),
    workloadTeam && can(me, "reports.read_team", "team", workloadTeam)
      ? api
          .get<WorkloadRow[]>(`/proposals/workload/${workloadTeam}`, { headers })
          .catch((error: unknown) => (error instanceof ApiError ? null : null))
      : Promise.resolve(null),
  ]);

  const open = mine?.items ?? [];
  const dueSoon = open.filter((p) => {
    const days = daysUntil(p.bcd);
    return days !== null && days <= 3;
  });
  const overdue = open.filter((p) => {
    const days = daysUntil(p.bcd);
    return days !== null && days < 0;
  });

  return (
    <>
      <PageHeader
        eyebrow={greeting()}
        title={me.display_name}
        description={
          me.teams.length > 0
            ? `You are in ${me.teams.length} team${me.teams.length === 1 ? "" : "s"}.`
            : "You are not in a team yet."
        }
      />

      <div className="mb-6">
        <MetricGrid>
          <Metric label="Open, assigned to you" value={open.length} />
          <Metric
            label="Due within 3 days"
            value={dueSoon.length}
            tone={dueSoon.length > 0 ? "warn" : "neutral"}
          />
          <Metric
            label="Past bid closing date"
            value={overdue.length}
            tone={overdue.length > 0 ? "danger" : "neutral"}
          />
          <Metric label="Open across your teams" value={teamProposals?.total ?? 0} />
        </MetricGrid>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <Card>
          <CardHeader
            title="Your open proposals"
            description="Soonest bid closing date first."
            action={
              <Link href="/proposals" className="text-[12.5px] text-accent hover:underline">
                All proposals →
              </Link>
            }
          />
          {open.length === 0 ? (
            <EmptyState
              title="Nothing assigned to you"
              description="When the assignment policy gives you a proposal, it will appear here."
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Proposal</Th>
                  <Th>Customer</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Closing</Th>
                </tr>
              </thead>
              <tbody>
                {open.slice(0, 12).map((proposal) => {
                  const days = daysUntil(proposal.bcd);
                  return (
                    <tr key={proposal.id}>
                      <Td>
                        <Link
                          href={`/proposals/${proposal.id}`}
                          className="font-medium text-ink hover:text-accent"
                        >
                          {proposal.title}
                        </Link>
                        {proposal.external_ref ? (
                          <div className="font-mono text-[10.5px] text-ink-3">
                            {proposal.external_ref}
                          </div>
                        ) : null}
                      </Td>
                      <Td>{proposal.customer_name ?? "—"}</Td>
                      <Td>
                        <Badge tone={statusTone(proposal.status)}>
                          {proposal.status.replace("_", " ")}
                        </Badge>
                      </Td>
                      <Td className="num text-right whitespace-nowrap">
                        {proposal.bcd ? (
                          <span
                            className={
                              days !== null && days < 0
                                ? "text-danger"
                                : days !== null && days <= 3
                                  ? "text-warn"
                                  : "text-ink-2"
                            }
                          >
                            {formatDate(proposal.bcd)}
                            {days !== null ? (
                              <span className="ml-1.5 font-mono text-[10.5px]">
                                {days < 0 ? `${Math.abs(days)}d over` : `${days}d`}
                              </span>
                            ) : null}
                          </span>
                        ) : (
                          "—"
                        )}
                      </Td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          )}
        </Card>

        {workload ? (
          <Card>
            <CardHeader
              title="Team workload"
              description="Effective load — open work divided by capacity."
            />
            {workload.length === 0 ? (
              <EmptyState title="No active members" />
            ) : (
              <div className="flex flex-col gap-3 p-5">
                {workload.map((row) => {
                  const max = Math.max(...workload.map((r) => r.effective_load), 1);
                  return (
                    <div key={row.user_id}>
                      <div className="mb-1 flex items-baseline justify-between gap-2">
                        <span className="truncate text-[13px] text-ink">{row.display_name}</span>
                        <span className="tabular shrink-0 font-mono text-[11px] text-ink-3">
                          {row.open_task_count} ÷ {row.capacity} ={" "}
                          <span className="text-ink-2">{row.effective_load.toFixed(1)}</span>
                        </span>
                      </div>
                      <Bar
                        value={row.effective_load}
                        max={max}
                        tone={row.on_leave ? "neutral" : row.effective_load > 6 ? "danger" : "accent"}
                      />
                      {row.labels.length > 0 || row.on_leave ? (
                        <div className="mt-1.5 flex flex-wrap gap-1">
                          {row.on_leave ? <Badge tone="warn">on leave</Badge> : null}
                          {row.labels.map((label) => (
                            <Badge key={label}>{label}</Badge>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        ) : null}
      </div>
    </>
  );
}

function greeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 17) return "Good afternoon";
  return "Good evening";
}
