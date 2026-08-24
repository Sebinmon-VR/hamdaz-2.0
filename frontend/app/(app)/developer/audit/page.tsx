import type { Metadata } from "next";
import type { ReactNode } from "react";

import {
  Badge,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  PageHeader,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api, qs } from "@/lib/api";
import { formatDateTime, summarise } from "@/lib/format";
import { forwardedHeaders, requireMe } from "@/lib/session";
import type { AuditEntry, Page as ApiPage } from "@/lib/types";

export const metadata: Metadata = { title: "Audit log" };

export default async function AuditPage({
  searchParams,
}: {
  searchParams: Promise<{ action?: string; entity?: string; ref?: string }>;
}): Promise<ReactNode> {
  await requireMe();
  const headers = await forwardedHeaders();
  const params = await searchParams;

  const entries = await api.get<ApiPage<AuditEntry>>(
    `/developer/audit${qs({
      action: params.action,
      entity_type: params.entity,
      correlation_id: params.ref,
      limit: 100,
    })}`,
    { headers },
  );

  return (
    <>
      <PageHeader
        eyebrow="Developer"
        title="Audit log"
        description="Every mutating action, with what changed. Append-only."
      />

      <div className="mb-5">
        <Callout tone="accent" title="Who did this, and why">
          This log answers <em>who</em>. The rule inspector answers <em>why</em>. Between them
          any decision can be reconstructed — something the legacy system cannot do, because it
          keeps no action log at all.
        </Callout>
      </div>

      <Card className="mb-5">
        <form className="flex flex-wrap items-end gap-3 p-4" method="get">
          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Action</span>
            <input
              name="action"
              defaultValue={params.action ?? ""}
              placeholder="assign, publish, update"
              className="h-9 w-48 rounded-[--radius-sm] border border-line bg-surface px-2.5 text-[13px] text-ink placeholder:text-ink-3 focus:border-accent"
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Entity type</span>
            <input
              name="entity"
              defaultValue={params.entity ?? ""}
              placeholder="proposal, team, role"
              className="h-9 w-48 rounded-[--radius-sm] border border-line bg-surface px-2.5 text-[13px] text-ink placeholder:text-ink-3 focus:border-accent"
            />
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Correlation ID</span>
            <input
              name="ref"
              defaultValue={params.ref ?? ""}
              placeholder="from an error message"
              className="h-9 w-56 rounded-[--radius-sm] border border-line bg-surface px-2.5 font-mono text-[12px] text-ink placeholder:text-ink-3 focus:border-accent"
            />
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
        <CardHeader title="Entries" description={`${entries.total} recorded.`} />
        {entries.items.length === 0 ? (
          <EmptyState title="Nothing recorded yet" />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>When</Th>
                <Th>Action</Th>
                <Th>Entity</Th>
                <Th>Change</Th>
                <Th>Reference</Th>
              </tr>
            </thead>
            <tbody>
              {entries.items.map((entry) => (
                <tr key={entry.id}>
                  <Td className="whitespace-nowrap">{formatDateTime(entry.created_at)}</Td>
                  <Td>
                    <Badge tone={entry.action === "delete" ? "danger" : "neutral"}>
                      {entry.action}
                    </Badge>
                  </Td>
                  <Td>
                    <div className="text-ink">{entry.entity_type}</div>
                    {entry.entity_id ? (
                      <div className="font-mono text-[10px] text-ink-3">
                        {entry.entity_id.slice(0, 8)}
                      </div>
                    ) : null}
                  </Td>
                  <Td>
                    {entry.after ? (
                      <span className="font-mono text-[10.5px]">
                        {summarise(entry.after, 110)}
                      </span>
                    ) : (
                      "—"
                    )}
                  </Td>
                  <Td>
                    <span className="font-mono text-[10px] text-ink-3">
                      {entry.correlation_id?.slice(0, 12) ?? "—"}
                    </span>
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
