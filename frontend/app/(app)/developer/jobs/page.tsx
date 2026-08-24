import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Badge, Card, CardHeader, EmptyState, PageHeader, Table, Td, Th } from "@/components/ui";
import { api, qs } from "@/lib/api";
import { formatDateTime, formatDuration, statusTone } from "@/lib/format";
import { forwardedHeaders, requireMe } from "@/lib/session";
import type { JobRun, Page as ApiPage } from "@/lib/types";

export const metadata: Metadata = { title: "Jobs" };

const STATUSES = ["", "pending", "running", "success", "failed", "retrying", "cancelled"] as const;

export default async function JobsPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string; task?: string }>;
}): Promise<ReactNode> {
  await requireMe();
  const headers = await forwardedHeaders();
  const params = await searchParams;

  const jobs = await api.get<ApiPage<JobRun>>(
    `/developer/jobs${qs({ status: params.status, task_name: params.task, limit: 100 })}`,
    { headers },
  );

  return (
    <>
      <PageHeader
        eyebrow="Developer"
        title="Job runs"
        description="Every background execution, with its arguments, duration and traceback. The legacy system ran three loops inside the web process with none of this."
      />

      <Card className="mb-5">
        <form className="flex flex-wrap items-end gap-3 p-4" method="get">
          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Status</span>
            <select
              name="status"
              defaultValue={params.status ?? ""}
              className="h-9 rounded-[--radius-sm] border border-line bg-surface px-2.5 pr-8 text-[13px] text-ink focus:border-accent"
            >
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s === "" ? "Any" : s}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Task</span>
            <input
              name="task"
              defaultValue={params.task ?? ""}
              placeholder="app.workers.tasks..."
              className="h-9 w-72 rounded-[--radius-sm] border border-line bg-surface px-2.5 font-mono text-[12px] text-ink placeholder:text-ink-3 focus:border-accent"
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
        <CardHeader title="Runs" description={`${jobs.total} recorded.`} />
        {jobs.items.length === 0 ? (
          <EmptyState
            title="No job runs"
            description="Either the workers are not running, or nothing has been scheduled yet."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Task</Th>
                <Th>Status</Th>
                <Th>Started</Th>
                <Th className="num text-right">Duration</Th>
                <Th className="num text-right">Retries</Th>
                <Th>Reference</Th>
              </tr>
            </thead>
            <tbody>
              {jobs.items.map((job) => (
                <tr key={job.id}>
                  <Td>
                    <div className="font-mono text-[11.5px] text-ink">
                      {job.task_name.split(".").pop()}
                    </div>
                    {job.error ? (
                      <div className="mt-1 max-w-lg font-mono text-[10.5px] leading-relaxed text-danger">
                        {job.error}
                      </div>
                    ) : null}
                  </Td>
                  <Td>
                    <Badge tone={statusTone(job.status)}>{job.status}</Badge>
                  </Td>
                  <Td className="whitespace-nowrap">{formatDateTime(job.started_at)}</Td>
                  <Td className="num text-right">{formatDuration(job.duration_ms)}</Td>
                  <Td className="num text-right">{job.retries}</Td>
                  <Td>
                    <span className="font-mono text-[10.5px] text-ink-3">
                      {job.correlation_id?.slice(0, 12) ?? "—"}
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
