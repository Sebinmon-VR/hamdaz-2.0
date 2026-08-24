"use client";

import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import {
  Badge,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Field,
  Input,
  Select,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { AssignResult, Candidate } from "@/lib/types";

/**
 * Assignment control.
 *
 * Two paths, and the difference is deliberate: letting the policy decide is one click, while
 * overriding it asks for a reason. The override is allowed — a manager knows things the
 * engine does not — but it is never silent, and the reason lands in the audit log.
 */
export function AssignPanel({
  proposalId,
  candidates,
  currentAssignee,
  canOverride,
}: {
  proposalId: string;
  candidates: Candidate[];
  currentAssignee: string | null;
  canOverride: boolean;
}): ReactNode {
  const router = useRouter();
  const [result, setResult] = useState<AssignResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [overriding, setOverriding] = useState(false);
  const [userId, setUserId] = useState("");
  const [reason, setReason] = useState("");

  async function run(body: Record<string, unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const outcome = await api.post<AssignResult>(`/proposals/${proposalId}/assign`, body);
      setResult(outcome);
      if (outcome.assigned) router.refresh();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.message}${err.correlationId ? ` (ref ${err.correlationId})` : ""}`
          : "Assignment failed.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader
        title={currentAssignee ? "Reassign" : "Assign"}
        description="Let the team's policy choose, or override it."
      />
      <CardBody className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" onClick={() => run({})} disabled={busy}>
            {busy ? "Working…" : "Run assignment policy"}
          </Button>
          {canOverride ? (
            <Button variant="ghost" onClick={() => setOverriding((v) => !v)} disabled={busy}>
              {overriding ? "Cancel override" : "Choose manually"}
            </Button>
          ) : null}
        </div>

        {overriding ? (
          <div className="flex flex-col gap-3 rounded-[--radius-sm] border border-line bg-surface-2 p-4">
            <Field label="Assign to">
              <Select value={userId} onChange={(e) => setUserId(e.target.value)}>
                <option value="">Select a member…</option>
                {candidates.map((c) => (
                  <option key={c.user_id} value={c.user_id}>
                    {c.display_name} — {c.open_task_count} open
                    {c.on_leave ? " (on leave)" : ""}
                  </option>
                ))}
              </Select>
            </Field>
            <Field
              label="Reason"
              hint="Recorded in the audit log. Overriding the policy without a reason is how policy drift starts."
            >
              <Input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="e.g. customer relationship, specialist knowledge"
              />
            </Field>
            <div>
              <Button
                variant="primary"
                size="sm"
                disabled={busy || !userId || reason.trim().length < 3}
                onClick={() => run({ user_id: userId, reason: reason.trim() })}
              >
                Confirm override
              </Button>
            </div>
          </div>
        ) : null}

        {error ? <Callout tone="danger" title="Failed">{error}</Callout> : null}

        {result ? (
          <div className="flex flex-col gap-3">
            <Callout tone={result.assigned ? "good" : "warn"} title={result.assigned ? "Assigned" : "Not assigned"}>
              {result.explanation}
              {result.fallback ? (
                <div className="mt-1 font-mono text-[11px] text-ink-3">
                  fallback: {result.fallback}
                </div>
              ) : null}
            </Callout>

            {result.ranked.length > 0 ? (
              <div>
                <div className="eyebrow mb-2">Ranking</div>
                <Table>
                  <thead>
                    <tr>
                      <Th>Candidate</Th>
                      <Th className="num text-right">Score</Th>
                      <Th className="num text-right">Load</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.ranked.map((c, index) => (
                      <tr key={c.user_id}>
                        <Td>
                          <span className={index === 0 ? "font-medium text-ink" : undefined}>
                            {c.display_name}
                          </span>
                          {index === 0 && result.assigned ? (
                            <Badge tone="good" className="ml-2">
                              chosen
                            </Badge>
                          ) : null}
                        </Td>
                        <Td className="num text-right">{c.score.toFixed(3)}</Td>
                        <Td className="num text-right">{c.effective_load.toFixed(1)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </div>
            ) : null}

            {result.excluded.length > 0 ? (
              <div>
                <div className="eyebrow mb-2">Not considered</div>
                <ul className="flex flex-col gap-1">
                  {result.excluded.map((e) => (
                    <li key={e.user_id} className="text-[12.5px] text-ink-3">
                      <span className="text-ink-2">{e.display_name}</span> — {e.reason}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}
