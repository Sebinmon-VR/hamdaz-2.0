import type { Metadata } from "next";
import Link from "next/link";
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
import { api } from "@/lib/api";
import { formatDate } from "@/lib/format";
import { forwardedHeaders, requireMe, teamsWith } from "@/lib/session";
import type { DecisionPoint, Page as ApiPage, RuleSet, Team } from "@/lib/types";

import { CreateRuleSet } from "./create-rule-set";

export const metadata: Metadata = { title: "Rules" };

export default async function RulesPage(): Promise<ReactNode> {
  const me = await requireMe();
  const headers = await forwardedHeaders();

  const [decisionPoints, sets, teams] = await Promise.all([
    api.get<DecisionPoint[]>("/rules/decision-points", { headers }),
    api
      .get<ApiPage<RuleSet>>("/rules/sets?limit=100", { headers })
      .catch(() => ({ items: [] as RuleSet[], total: 0, limit: 0, offset: 0 })),
    api
      .get<ApiPage<Team>>("/admin/teams", { headers })
      .then((p) => p.items)
      .catch(() => [] as Team[]),
  ]);

  const editable = teamsWith(me, "rules.edit_team");
  const teamName = (id: string | null): string =>
    id === null ? "Organisation-wide" : (teams.find((t) => t.id === id)?.name ?? "—");

  return (
    <>
      <PageHeader
        eyebrow="Admin"
        title="Rules"
        description="Policy the system runs on, configured rather than coded."
        action={
          editable.length > 0 ? (
            <CreateRuleSet
              decisionPoints={decisionPoints}
              teams={teams.filter((t) => editable.includes(t.id))}
            />
          ) : null
        }
      />

      <div className="mb-6">
        <Callout tone="accent" title="Author, simulate, publish">
          A new rule set starts disabled. It only takes effect when someone publishes it, and
          every publish is a version that can be restored. Simulation runs the same evaluator
          the live path uses, so a preview is not an approximation.
        </Callout>
      </div>

      <Card className="mb-6">
        <CardHeader
          title="Rule sets"
          description={`${sets.total} configured.`}
        />
        {sets.items.length === 0 ? (
          <EmptyState
            title="No rule sets yet"
            description="Until one exists, each decision point uses its shipped default behaviour."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Rule set</Th>
                <Th>Decision point</Th>
                <Th>Scope</Th>
                <Th className="num text-right">Rules</Th>
                <Th>State</Th>
                <Th>Published</Th>
              </tr>
            </thead>
            <tbody>
              {sets.items.map((set) => (
                <tr key={set.id}>
                  <Td>
                    <Link
                      href={`/admin/rules/${set.id}`}
                      className="font-medium text-ink hover:text-accent"
                    >
                      {set.name}
                    </Link>
                  </Td>
                  <Td>
                    <span className="font-mono text-[11.5px]">{set.decision_point}</span>
                  </Td>
                  <Td>{teamName(set.team_id)}</Td>
                  <Td className="num text-right">{set.rules.length}</Td>
                  <Td>
                    {set.enabled ? (
                      <Badge tone="good">live · v{set.version}</Badge>
                    ) : (
                      <Badge tone="neutral">draft</Badge>
                    )}
                  </Td>
                  <Td className="whitespace-nowrap">{formatDate(set.published_at)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      <Card>
        <CardHeader
          title="Decision points"
          description="Every moment the system makes a choice. The rule builder offers only these facts and actions, so a rule can never ask for something the engine does not support."
        />
        <div className="divide-y divide-line-2">
          {decisionPoints.map((dp) => (
            <div key={dp.key} className="px-5 py-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div>
                  <span className="text-[14px] font-medium text-ink">{dp.name}</span>
                  <span className="ml-2 font-mono text-[11px] text-ink-3">{dp.key}</span>
                </div>
                <div className="flex gap-1.5">
                  <Badge>{dp.facts.length} facts</Badge>
                  <Badge>{dp.actions.length} actions</Badge>
                </div>
              </div>
              <p className="mt-1 max-w-prose text-[12.5px] leading-relaxed text-ink-2">
                {dp.description}
              </p>
              <p className="mt-1 font-mono text-[11px] text-ink-3">Fires when: {dp.fires_when}</p>
            </div>
          ))}
        </div>
      </Card>
    </>
  );
}
