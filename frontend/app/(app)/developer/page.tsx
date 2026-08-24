import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import {
  Badge,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  Metric,
  MetricGrid,
  PageHeader,
  StatusDot,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { api } from "@/lib/api";
import { formatDuration, formatRelative } from "@/lib/format";
import { forwardedHeaders, requireMe } from "@/lib/session";
import type { Connector, SystemOverview, TaskSummary } from "@/lib/types";

export const metadata: Metadata = { title: "Developer" };

export default async function DeveloperPage(): Promise<ReactNode> {
  await requireMe();
  const headers = await forwardedHeaders();

  const [overview, tasks, connectors] = await Promise.all([
    api.get<SystemOverview>("/developer/overview", { headers }),
    api.get<TaskSummary[]>("/developer/jobs/summary?hours=24", { headers }).catch(() => []),
    api.get<Connector[]>("/developer/connectors", { headers }).catch(() => []),
  ]);

  const sharepoint = connectors.find((c) => c.name === "sharepoint");

  return (
    <>
      <PageHeader
        eyebrow="Developer"
        title="System overview"
        description="What the platform is doing right now — the thing the legacy system could not show at all."
      />

      {/* Constraint state, surfaced where a developer will actually look at it. */}
      <div className="mb-6 grid gap-3 md:grid-cols-2">
        <Callout
          tone={overview.sharepoint_mode === "read_only" ? "good" : "warn"}
          title="SharePoint"
        >
          Live sites are <strong>{overview.sharepoint_mode.replace(/_/g, " ")}</strong>. The only
          writable target is <span className="font-mono text-[12px]">{overview.sharepoint_sandbox_site}</span>.
          {sharepoint?.last_error ? (
            <div className="mt-1 font-mono text-[11px] text-danger">{sharepoint.last_error}</div>
          ) : null}
        </Callout>

        <Callout tone={overview.outbound_email_enabled ? "warn" : "good"} title="Outbound email">
          {overview.outbound_email_enabled ? (
            <>
              <strong>Enabled.</strong> Mail leaves the building. Correct for production, a
              hazard anywhere else.
            </>
          ) : (
            <>
              <strong>Captured, not sent.</strong> {overview.captured_emails} message(s) are in
              the outbox. A development build cannot email a real supplier.
            </>
          )}
        </Callout>
      </div>

      <div className="mb-6">
        <MetricGrid>
          <Metric label="Environment" value={overview.environment} />
          <Metric label="Jobs · last hour" value={overview.jobs_last_hour} />
          <Metric
            label="Failed · last hour"
            value={overview.jobs_failed_last_hour}
            tone={overview.jobs_failed_last_hour > 0 ? "danger" : "good"}
          />
          <Metric label="Rule evaluations" value={overview.rule_evaluations_last_hour} />
          <Metric label="Audit entries" value={overview.audit_entries_last_hour} />
        </MetricGrid>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Tasks · 24 hours"
            description="Every background run is recorded. A job nobody can see is a job nobody can debug."
            action={
              <Link href="/developer/jobs" className="text-[12.5px] text-accent hover:underline">
                All runs →
              </Link>
            }
          />
          {tasks.length === 0 ? (
            <EmptyState
              title="No runs yet"
              description="Background workers may not be running, or nothing has been scheduled."
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Task</Th>
                  <Th className="num text-right">Runs</Th>
                  <Th className="num text-right">Failed</Th>
                  <Th className="num text-right">Avg</Th>
                  <Th className="text-right">Last</Th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr key={task.task_name}>
                    <Td>
                      <span className="font-mono text-[11.5px] text-ink">
                        {task.task_name.split(".").pop()}
                      </span>
                    </Td>
                    <Td className="num text-right">{task.runs}</Td>
                    <Td
                      className={`num text-right ${task.failures > 0 ? "text-danger" : ""}`}
                    >
                      {task.failures}
                    </Td>
                    <Td className="num text-right">{formatDuration(task.avg_duration_ms)}</Td>
                    <Td className="text-right whitespace-nowrap">
                      {formatRelative(task.last_run)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        <Card>
          <CardHeader
            title="Connectors"
            description="External systems and their sync state."
            action={
              <Link
                href="/developer/connectors"
                className="text-[12.5px] text-accent hover:underline"
              >
                Detail →
              </Link>
            }
          />
          <Table>
            <thead>
              <tr>
                <Th>Connector</Th>
                <Th>Mode</Th>
                <Th className="text-right">Last success</Th>
              </tr>
            </thead>
            <tbody>
              {connectors.map((connector) => (
                <tr key={connector.name}>
                  <Td>
                    <span className="flex items-center gap-2">
                      <StatusDot tone={connector.healthy ? "good" : "danger"} />
                      <span className="font-mono text-[11.5px] text-ink">{connector.name}</span>
                    </span>
                  </Td>
                  <Td>
                    <Badge tone={connector.mode === "read_only" ? "danger" : "neutral"}>
                      {connector.mode.replace(/_/g, " ")}
                    </Badge>
                  </Td>
                  <Td className="text-right whitespace-nowrap">
                    {formatRelative(connector.last_success_at)}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      </div>
    </>
  );
}
