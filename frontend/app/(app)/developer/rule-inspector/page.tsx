import type { Metadata } from "next";
import type { ReactNode } from "react";

import {
  Badge,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  PageHeader,
  StatusDot,
  cn,
} from "@/components/ui";
import { api, qs } from "@/lib/api";
import { formatDateTime, summarise } from "@/lib/format";
import { forwardedHeaders, requireMe } from "@/lib/session";
import type { DecisionPoint, Page as ApiPage, RuleEvaluation } from "@/lib/types";

export const metadata: Metadata = { title: "Rule inspector" };

export default async function RuleInspectorPage({
  searchParams,
}: {
  searchParams: Promise<{ dp?: string; entity?: string; simulated?: string }>;
}): Promise<ReactNode> {
  await requireMe();
  const headers = await forwardedHeaders();
  const params = await searchParams;

  const [evaluations, decisionPoints] = await Promise.all([
    api.get<ApiPage<RuleEvaluation>>(
      `/developer/rule-evaluations${qs({
        decision_point: params.dp,
        entity_id: params.entity,
        include_simulated: params.simulated === "1" ? true : undefined,
        limit: 50,
      })}`,
      { headers },
    ),
    api.get<DecisionPoint[]>("/rules/decision-points", { headers }).catch(() => []),
  ]);

  return (
    <>
      <PageHeader
        eyebrow="Developer"
        title="Rule inspector"
        description="Why the system decided what it decided — the facts it saw, which rules matched, and what followed."
      />

      <div className="mb-5">
        <Callout tone="accent" title="The question this answers">
          &ldquo;Why did Rahul get this proposal?&rdquo; Filter by entity to get the decision
          trace for one record. The legacy system cannot answer this at all — it keeps no
          record of its own reasoning.
        </Callout>
      </div>

      <Card className="mb-5">
        <form className="flex flex-wrap items-end gap-3 p-4" method="get">
          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Decision point</span>
            <select
              name="dp"
              defaultValue={params.dp ?? ""}
              className="h-9 rounded-[--radius-sm] border border-line bg-surface px-2.5 pr-8 text-[13px] text-ink focus:border-accent"
            >
              <option value="">All</option>
              {decisionPoints.map((dp) => (
                <option key={dp.key} value={dp.key}>
                  {dp.name}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1.5">
            <span className="eyebrow">Entity ID</span>
            <input
              name="entity"
              defaultValue={params.entity ?? ""}
              placeholder="proposal UUID"
              className="h-9 w-72 rounded-[--radius-sm] border border-line bg-surface px-2.5 font-mono text-[12px] text-ink placeholder:text-ink-3 focus:border-accent"
            />
          </label>

          <label className="flex h-9 cursor-pointer items-center gap-2 text-[13px] text-ink">
            <input
              type="checkbox"
              name="simulated"
              value="1"
              defaultChecked={params.simulated === "1"}
              className="size-3.5 accent-[--color-accent]"
            />
            Include simulations
          </label>

          <button
            type="submit"
            className="h-9 rounded-[--radius-sm] bg-accent px-3.5 text-[13px] font-medium text-white hover:bg-accent-hover"
          >
            Filter
          </button>
        </form>
      </Card>

      {evaluations.items.length === 0 ? (
        <Card>
          <EmptyState
            title="No evaluations recorded"
            description="Evaluations appear once rules run. Simulations are hidden by default — tick the box to include them."
          />
        </Card>
      ) : (
        <div className="flex flex-col gap-4">
          {evaluations.items.map((evaluation) => (
            <Card key={evaluation.id}>
              <CardHeader
                title={
                  <span className="flex flex-wrap items-center gap-2">
                    <StatusDot tone={evaluation.matched_rule_ids.length > 0 ? "good" : "neutral"} />
                    <span className="font-mono text-[12.5px]">{evaluation.decision_point}</span>
                    {evaluation.simulated ? <Badge tone="warn">simulated</Badge> : null}
                    {evaluation.rule_set_version ? (
                      <Badge>v{evaluation.rule_set_version}</Badge>
                    ) : (
                      <Badge tone="neutral">no rule set</Badge>
                    )}
                  </span>
                }
                description={
                  <span className="font-mono text-[11px]">
                    {formatDateTime(evaluation.created_at)}
                    {evaluation.duration_ms !== null ? ` · ${evaluation.duration_ms}ms` : ""}
                    {evaluation.entity_id ? ` · ${evaluation.entity_type} ${evaluation.entity_id}` : ""}
                  </span>
                }
              />

              <div className="grid gap-0 md:grid-cols-2">
                <div className="border-b border-line p-4 md:border-b-0 md:border-r">
                  <div className="eyebrow mb-2">Facts the engine saw</div>
                  <dl className="flex flex-col gap-1">
                    {Object.entries(evaluation.facts).map(([key, value]) => (
                      <div key={key} className="flex gap-2 font-mono text-[11px]">
                        <dt className="shrink-0 text-ink-3">{key}</dt>
                        <dd className="min-w-0 truncate text-ink-2">{summarise(value, 40)}</dd>
                      </div>
                    ))}
                    {Object.keys(evaluation.facts).length === 0 ? (
                      <span className="text-[12px] text-ink-3">no facts recorded</span>
                    ) : null}
                  </dl>
                </div>

                <div className="p-4">
                  <div className="eyebrow mb-2">Outcome</div>
                  {evaluation.matched_rule_ids.length === 0 ? (
                    <p className="text-[12.5px] text-ink-3">
                      Nothing matched, so the caller fell back to its own default. This is a
                      legitimate state, not a failure.
                    </p>
                  ) : null}

                  {Array.isArray(evaluation.outcome.actions) &&
                  evaluation.outcome.actions.length > 0 ? (
                    <div className="mb-2 flex flex-wrap gap-1">
                      {(evaluation.outcome.actions as Array<Record<string, unknown>>).map(
                        (action, i) => (
                          <Badge key={i} tone="accent">
                            {String(action.type)}
                          </Badge>
                        ),
                      )}
                    </div>
                  ) : null}

                  {evaluation.trace && evaluation.trace.length > 0 ? (
                    <details>
                      <summary className="cursor-pointer font-mono text-[11px] text-ink-3 hover:text-accent">
                        trace ({evaluation.trace.length} rules)
                      </summary>
                      <div className="mt-2 flex flex-col gap-1.5">
                        {evaluation.trace.map((trace) => (
                          <div
                            key={trace.rule_id}
                            className={cn(
                              "rounded-[--radius-sm] border px-2 py-1.5",
                              trace.matched
                                ? "border-good bg-good-soft"
                                : "border-line bg-surface-2",
                            )}
                          >
                            <div className="text-[12px] text-ink">{trace.name}</div>
                            {trace.conditions.map((condition, i) => (
                              <div key={i} className="font-mono text-[10.5px] text-ink-2">
                                <span className={condition.passed ? "text-good" : "text-danger"}>
                                  {condition.passed ? "✓" : "✕"}
                                </span>{" "}
                                {condition.fact} {condition.op}{" "}
                                {summarise(condition.expected, 20)}
                                <span className="text-ink-3">
                                  {" "}
                                  (was {summarise(condition.actual, 20)})
                                </span>
                              </div>
                            ))}
                            {trace.skipped_reason ? (
                              <div className="font-mono text-[10px] text-ink-3">
                                {trace.skipped_reason}
                              </div>
                            ) : null}
                          </div>
                        ))}
                      </div>
                    </details>
                  ) : null}

                  {evaluation.correlation_id ? (
                    <div className="mt-2 font-mono text-[10px] text-ink-3">
                      ref {evaluation.correlation_id}
                    </div>
                  ) : null}
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </>
  );
}
