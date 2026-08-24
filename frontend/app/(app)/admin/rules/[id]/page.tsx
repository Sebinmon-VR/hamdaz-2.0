import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { Badge, Callout, PageHeader } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { can, forwardedHeaders, requireMe } from "@/lib/session";
import type { DecisionPoint, RuleSet, RuleSetVersion } from "@/lib/types";

import { RuleBuilder } from "./rule-builder";

export const metadata: Metadata = { title: "Rule set" };

export default async function RuleSetPage({
  params,
}: {
  params: Promise<{ id: string }>;
}): Promise<ReactNode> {
  const me = await requireMe();
  const headers = await forwardedHeaders();
  const { id } = await params;

  let ruleSet: RuleSet;
  try {
    ruleSet = await api.get<RuleSet>(`/rules/sets/${id}`, { headers });
  } catch (error) {
    if (error instanceof ApiError && (error.status === 404 || error.isForbidden)) notFound();
    throw error;
  }

  const [decisionPoints, versions] = await Promise.all([
    api.get<DecisionPoint[]>("/rules/decision-points", { headers }),
    api
      .get<RuleSetVersion[]>(`/rules/sets/${id}/versions`, { headers })
      .catch(() => [] as RuleSetVersion[]),
  ]);

  const decisionPoint = decisionPoints.find((d) => d.key === ruleSet.decision_point);
  if (!decisionPoint) notFound();

  const teamId = ruleSet.team_id ?? undefined;

  return (
    <>
      <PageHeader
        eyebrow={`Admin · Rules · ${decisionPoint.name}`}
        title={ruleSet.name}
        description={decisionPoint.description}
        action={
          <Link href="/admin/rules" className="text-[12.5px] text-accent hover:underline">
            ← All rules
          </Link>
        }
      />

      <div className="mb-5 flex flex-wrap items-center gap-2">
        <Badge tone={ruleSet.enabled ? "good" : "neutral"}>
          {ruleSet.enabled ? `live · v${ruleSet.version}` : "draft"}
        </Badge>
        <Badge>{ruleSet.decision_point}</Badge>
        <Badge tone={ruleSet.team_id ? "accent" : "legacy"}>
          {ruleSet.team_id ? "team scoped" : "organisation-wide"}
        </Badge>
        <span className="font-mono text-[11px] text-ink-3">
          fires when: {decisionPoint.fires_when}
        </span>
      </div>

      {!ruleSet.enabled ? (
        <div className="mb-5">
          <Callout tone="warn" title="Not live">
            This rule set is a draft. It has no effect until it is published, and the decision
            point keeps using whatever else is configured, or its shipped default.
          </Callout>
        </div>
      ) : null}

      <RuleBuilder
        ruleSet={ruleSet}
        decisionPoint={decisionPoint}
        versions={versions}
        canEdit={can(me, "rules.edit_team", "team", teamId)}
        canPublish={can(me, "rules.publish", "team", teamId)}
        canSimulate={can(me, "rules.simulate", "team", teamId)}
      />
    </>
  );
}
