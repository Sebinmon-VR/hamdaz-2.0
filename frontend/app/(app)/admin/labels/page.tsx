import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Badge, Callout, Card, CardHeader, EmptyState, PageHeader, Table, Td, Th } from "@/components/ui";
import { api } from "@/lib/api";
import { can, forwardedHeaders, requireMe } from "@/lib/session";
import type { Label, LabelKind } from "@/lib/types";

import { CreateLabel } from "./create-label";

export const metadata: Metadata = { title: "Labels" };

const KIND_COPY: Record<LabelKind, { title: string; blurb: string }> = {
  category: {
    title: "Category",
    blurb: "Seniority and contract shape. Drives capacity, ratios and approval routing.",
  },
  skill: {
    title: "Skill",
    blurb: "Competencies. Gates eligibility — only tag-matching people get certain work.",
  },
  status: {
    title: "Status",
    blurb: "Temporary states. Drives eligibility and capacity.",
  },
};

export default async function LabelsPage(): Promise<ReactNode> {
  const me = await requireMe();
  const headers = await forwardedHeaders();

  const labels = await api.get<Label[]>("/admin/labels", { headers });
  const canManage = can(me, "labels.manage", "all");

  const byKind = (kind: LabelKind): Label[] => labels.filter((l) => l.kind === kind);

  return (
    <>
      <PageHeader
        eyebrow="Admin"
        title="Labels"
        description="The vocabulary the rules engine speaks in."
        action={canManage ? <CreateLabel /> : null}
      />

      <div className="mb-6">
        <Callout tone="accent" title="Roles grant permission — labels drive policy">
          Two separate axes, deliberately. A Senior and a New Joiner can both be
          <span className="font-mono text-[12px]"> team_member</span> with identical
          permissions and still receive very different workloads, because the assignment
          policy reads their labels rather than their role.
        </Callout>
      </div>

      <div className="flex flex-col gap-5">
        {(["category", "skill", "status"] as LabelKind[]).map((kind) => {
          const items = byKind(kind);
          return (
            <Card key={kind}>
              <CardHeader
                title={KIND_COPY[kind].title}
                description={KIND_COPY[kind].blurb}
                action={<Badge>{items.length}</Badge>}
              />
              {items.length === 0 ? (
                <EmptyState title={`No ${kind} labels yet`} />
              ) : (
                <Table>
                  <thead>
                    <tr>
                      <Th>Label</Th>
                      <Th>Key</Th>
                      <Th>Description</Th>
                      <Th>Scope</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((label) => (
                      <tr key={label.id}>
                        <Td className="font-medium text-ink">{label.name}</Td>
                        <Td>
                          <span className="font-mono text-[11.5px]">{label.key}</span>
                        </Td>
                        <Td>{label.description ?? "—"}</Td>
                        <Td>
                          <Badge tone={label.team_id ? "accent" : "neutral"}>
                            {label.team_id ? "team" : "organisation"}
                          </Badge>
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              )}
            </Card>
          );
        })}
      </div>
    </>
  );
}
