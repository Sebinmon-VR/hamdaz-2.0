import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Badge, Callout, Card, CardBody, CardHeader, PageHeader, StatusDot } from "@/components/ui";
import { api } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import { forwardedHeaders, requireMe } from "@/lib/session";
import type { Connector } from "@/lib/types";

export const metadata: Metadata = { title: "Connectors" };

export default async function ConnectorsPage(): Promise<ReactNode> {
  await requireMe();
  const headers = await forwardedHeaders();
  const connectors = await api.get<Connector[]>("/developer/connectors", { headers });

  return (
    <>
      <PageHeader
        eyebrow="Developer"
        title="Connectors"
        description="External systems, their health, and their sync cursors."
      />

      <div className="mb-5">
        <Callout tone="danger" title="Constraint C2">
          Live SharePoint is read-only. The connector has no write methods to call, a runtime
          guard checks the site ID on every non-GET request, and CI fails the build if a write
          call appears outside the sandbox module. Three independent guards, because one is not
          enough when the target is production data.
        </Callout>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        {connectors.map((connector) => (
          <Card key={connector.name}>
            <CardHeader
              title={
                <span className="flex items-center gap-2">
                  <StatusDot tone={connector.healthy ? "good" : "danger"} />
                  <span className="font-mono text-[13px]">{connector.name}</span>
                </span>
              }
              action={
                <Badge tone={connector.mode === "read_only" ? "danger" : "neutral"}>
                  {connector.mode.replace(/_/g, " ")}
                </Badge>
              }
            />
            <CardBody className="flex flex-col gap-3">
              <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
                <div>
                  <dt className="eyebrow">Last success</dt>
                  <dd className="text-[13px] text-ink-2">
                    {formatRelative(connector.last_success_at)}
                  </dd>
                </div>
                <div>
                  <dt className="eyebrow">Latency</dt>
                  <dd className="tabular text-[13px] text-ink-2">
                    {connector.latency_ms !== null ? `${connector.latency_ms}ms` : "—"}
                  </dd>
                </div>
              </dl>

              {connector.last_error ? (
                <div>
                  <div className="eyebrow mb-1">
                    Last error · {formatDateTime(connector.last_error_at)}
                  </div>
                  <p className="font-mono text-[11px] leading-relaxed text-danger">
                    {connector.last_error}
                  </p>
                </div>
              ) : null}

              {connector.detail ? (
                <div>
                  <div className="eyebrow mb-1">Detail</div>
                  <pre className="scroll-x rounded-[--radius-sm] border border-line bg-surface-2 p-2.5 font-mono text-[10.5px] leading-relaxed text-ink-2">
                    {JSON.stringify(connector.detail, null, 2)}
                  </pre>
                </div>
              ) : null}
            </CardBody>
          </Card>
        ))}
      </div>
    </>
  );
}
