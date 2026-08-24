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
import { api, qs } from "@/lib/api";
import { daysUntil, formatDate, statusTone } from "@/lib/format";
import { forwardedHeaders, requireMe } from "@/lib/session";
import type { Page as ApiPage, Proposal, Team } from "@/lib/types";

export const metadata: Metadata = { title: "Proposals" };

const STATUSES = [
  "",
  "new",
  "assigned",
  "in_progress",
  "submitted",
  "won",
  "lost",
  "cancelled",
] as const;

export default async function ProposalsPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string; team?: string; q?: string; open?: string }>;
}): Promise<ReactNode> {
  await requireMe();
  const headers = await forwardedHeaders();
  const params = await searchParams;

  const query = qs({
    status: params.status,
    team_id: params.team,
    search: params.q,
    open_only: params.open === "1" ? true : undefined,
    limit: 100,
  });

  const [page, teams] = await Promise.all([
    api.get<ApiPage<Proposal>>(`/proposals/${query}`, { headers }),
    api
      .get<ApiPage<Team>>("/admin/teams", { headers })
      .then((p) => p.items)
      .catch(() => [] as Team[]),
  ]);

  const teamName = (id: string): string => teams.find((t) => t.id === id)?.name ?? "—";

  return (
    <>
      <PageHeader
        eyebrow="Work"
        title="Proposals"
        description={`${page.total} matching. You only ever see teams you have access to.`}
      />

      <Card className="mb-5">
        <form className="flex flex-wrap items-end gap-3 p-4" method="get">
          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Search</span>
            <input
              name="q"
              defaultValue={params.q ?? ""}
              placeholder="Title, customer or reference"
              className="h-9 w-56 rounded-[--radius-sm] border border-line bg-surface px-2.5 text-[13px] text-ink placeholder:text-ink-3 focus:border-accent"
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Status</span>
            <select
              name="status"
              defaultValue={params.status ?? ""}
              className="h-9 rounded-[--radius-sm] border border-line bg-surface px-2.5 pr-8 text-[13px] text-ink focus:border-accent"
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s === "" ? "Any" : s.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>

          {teams.length > 1 ? (
            <label className="flex flex-col gap-1.5">
              <span className="eyebrow">Team</span>
              <select
                name="team"
                defaultValue={params.team ?? ""}
                className="h-9 rounded-[--radius-sm] border border-line bg-surface px-2.5 pr-8 text-[13px] text-ink focus:border-accent"
              >
                <option value="">All my teams</option>
                {teams.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}

          <label className="flex h-9 cursor-pointer items-center gap-2 text-[13px] text-ink">
            <input
              type="checkbox"
              name="open"
              value="1"
              defaultChecked={params.open === "1"}
              className="size-3.5 accent-[--color-accent]"
            />
            Open only
          </label>

          <button
            type="submit"
            className="h-9 rounded-[--radius-sm] bg-accent px-3.5 text-[13px] font-medium text-white hover:bg-accent-hover"
          >
            Filter
          </button>
        </form>
      </Card>

      <Card>
        <CardHeader title="Results" description="Soonest bid closing date first." />
        {page.items.length === 0 ? (
          <EmptyState
            title="No proposals match"
            description="Try widening the filters, or check you are looking at the right team."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Proposal</Th>
                <Th>Customer</Th>
                <Th>Team</Th>
                <Th>Status</Th>
                <Th>Source</Th>
                <Th className="text-right">Closing</Th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((proposal) => {
                const days = daysUntil(proposal.bcd);
                return (
                  <tr key={proposal.id} className="hover:bg-surface-2">
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
                      {proposal.required_labels.length > 0 ? (
                        <div className="mt-1 flex flex-wrap gap-1">
                          {proposal.required_labels.map((label) => (
                            <Badge key={label} tone="accent">
                              needs {label}
                            </Badge>
                          ))}
                        </div>
                      ) : null}
                    </Td>
                    <Td>{proposal.customer_name ?? "—"}</Td>
                    <Td className="whitespace-nowrap">{teamName(proposal.team_id)}</Td>
                    <Td>
                      <Badge tone={statusTone(proposal.status)}>
                        {proposal.status.replace("_", " ")}
                      </Badge>
                    </Td>
                    <Td>
                      {/* Brass marks anything originating in a legacy or external system. */}
                      <Badge tone={proposal.source === "sharepoint" ? "legacy" : "neutral"}>
                        {proposal.source}
                      </Badge>
                    </Td>
                    <Td className="num whitespace-nowrap text-right">
                      {proposal.bcd ? (
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
    </>
  );
}
