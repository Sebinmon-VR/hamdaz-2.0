"use client";

import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { Button, Callout, Card, CardBody, CardHeader, Field, Input, Select } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { DecisionPoint, RuleSet, Team } from "@/lib/types";

export function CreateRuleSet({
  decisionPoints,
  teams,
}: {
  decisionPoints: DecisionPoint[];
  teams: Team[];
}): ReactNode {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [decisionPoint, setDecisionPoint] = useState(decisionPoints[0]?.key ?? "");
  const [teamId, setTeamId] = useState(teams[0]?.id ?? "");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const selected = decisionPoints.find((d) => d.key === decisionPoint);

  async function submit(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const created = await api.post<RuleSet>("/rules/sets", {
        decision_point: decisionPoint,
        name: name.trim(),
        team_id: teamId || null,
      });
      router.push(`/admin/rules/${created.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the rule set.");
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <Button variant="primary" onClick={() => setOpen(true)}>
        New rule set
      </Button>
    );
  }

  return (
    <Card className="w-full max-w-md">
      <CardHeader
        title="New rule set"
        action={
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        }
      />
      <CardBody className="flex flex-col gap-4">
        <Field label="Decision point" hint={selected?.description}>
          <Select value={decisionPoint} onChange={(e) => setDecisionPoint(e.target.value)}>
            {decisionPoints.map((dp) => (
              <option key={dp.key} value={dp.key}>
                {dp.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Team"
          hint="A team's own rule set takes precedence over the organisation-wide default."
        >
          <Select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
            {teams.map((team) => (
              <option key={team.id} value={team.id}>
                {team.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Name">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Large deal approvals"
          />
        </Field>

        {error ? <Callout tone="danger">{error}</Callout> : null}

        <div>
          <Button variant="primary" onClick={submit} disabled={busy || name.trim().length < 2}>
            {busy ? "Creating…" : "Create and add rules"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
