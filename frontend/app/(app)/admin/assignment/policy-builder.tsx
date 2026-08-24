"use client";

import { useCallback, useMemo, useState, type ReactNode } from "react";

import {
  Badge,
  Bar,
  Button,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Checkbox,
  EmptyState,
  Field,
  Input,
  Select,
  Table,
  Td,
  Th,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { AssignmentPolicy, AssignmentPreview, Candidate, Label } from "@/lib/types";

const MODES = [
  { value: "weighted_least_loaded", label: "Weighted least loaded", hint: "Balances load, capacity and idle time. The usual choice." },
  { value: "least_loaded", label: "Least loaded", hint: "Purely by open task count. Ignores capacity." },
  { value: "round_robin", label: "Round robin", hint: "Whoever has waited longest." },
  { value: "ratio", label: "Ratio", hint: "Distribute by label share, correcting drift." },
  { value: "manual", label: "Manual", hint: "Nothing is assigned automatically." },
] as const;

const TIE_BREAKS = ["longest_idle", "lowest_load", "highest_capacity"] as const;
const FALLBACKS = ["notify_manager", "leave_unassigned"] as const;

interface Props {
  teamId: string;
  teamName: string;
  initialPolicy: AssignmentPolicy;
  labels: Label[];
  candidates: Candidate[];
  canPublish: boolean;
}

/**
 * The assignment policy builder (§5.4).
 *
 * Structured deliberately as: edit → **preview** → publish. Preview is not optional
 * decoration. An assignment policy nobody can predict is one nobody will trust, and the
 * legacy system's rotation was exactly that — a `swp()` function whose behaviour you could
 * only discover by watching who got work.
 */
export function PolicyBuilder({
  teamId,
  teamName,
  initialPolicy,
  labels,
  candidates,
  canPublish,
}: Props): ReactNode {
  const [capacity, setCapacity] = useState<Record<string, number>>(
    initialPolicy.capacity.by_label ?? {},
  );
  const [defaultCapacity, setDefaultCapacity] = useState(initialPolicy.capacity.default ?? 1);
  const [maxOpenDefault, setMaxOpenDefault] = useState(
    initialPolicy.eligibility.max_open_tasks?.default ?? 8,
  );
  const [maxOpenByLabel, setMaxOpenByLabel] = useState<Record<string, number>>(
    initialPolicy.eligibility.max_open_tasks?.by_label ?? {},
  );
  const [excluded, setExcluded] = useState<string[]>(
    initialPolicy.eligibility.not_labelled ?? [],
  );
  const [respectLeave, setRespectLeave] = useState(
    initialPolicy.eligibility.not_on_leave ?? true,
  );
  const [mode, setMode] = useState(initialPolicy.distribution.mode ?? "weighted_least_loaded");
  const [ratioTargets, setRatioTargets] = useState<Record<string, number>>(
    initialPolicy.distribution.ratio?.targets ?? {},
  );
  const [tieBreak, setTieBreak] = useState(initialPolicy.tie_break);
  const [fallback, setFallback] = useState(initialPolicy.fallback);
  const [allowOverride, setAllowOverride] = useState(initialPolicy.allow_manual_override);

  const [preview, setPreview] = useState<AssignmentPreview | null>(null);
  const [busy, setBusy] = useState<"preview" | "publish" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [published, setPublished] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  const categoryLabels = useMemo(
    () => labels.filter((l) => l.kind === "category"),
    [labels],
  );
  const statusLabels = useMemo(() => labels.filter((l) => l.kind === "status"), [labels]);

  const body = useMemo(
    () => ({
      name: initialPolicy.name === "Built-in default" ? "default" : initialPolicy.name,
      eligibility: {
        not_on_leave: respectLeave,
        not_labelled: excluded,
        requires_labels: initialPolicy.eligibility.requires_labels ?? [],
        max_open_tasks: { default: maxOpenDefault, by_label: maxOpenByLabel },
      },
      capacity: { default: defaultCapacity, by_label: capacity },
      distribution: {
        mode,
        factors: initialPolicy.distribution.factors,
        ratio: { by: "label", targets: ratioTargets, window: "rolling_30d" },
      },
      tie_break: tieBreak,
      fallback,
      allow_manual_override: allowOverride,
    }),
    [
      initialPolicy,
      respectLeave,
      excluded,
      maxOpenDefault,
      maxOpenByLabel,
      defaultCapacity,
      capacity,
      mode,
      ratioTargets,
      tieBreak,
      fallback,
      allowOverride,
    ],
  );

  const touch = useCallback(() => {
    setDirty(true);
    // A preview of a policy you have since edited is worse than none — it looks authoritative
    // and is wrong. Clear it the moment anything changes.
    setPreview(null);
    setPublished(null);
  }, []);

  async function runPreview(): Promise<void> {
    setBusy("preview");
    setError(null);
    try {
      const result = await api.post<AssignmentPreview>(
        `/rules/assignment/${teamId}/preview?count=10`,
        body,
      );
      setPreview(result);
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(null);
    }
  }

  async function publish(): Promise<void> {
    setBusy("publish");
    setError(null);
    try {
      const result = await api.post<{ message: string }>(
        `/rules/assignment/${teamId}/publish`,
        body,
      );
      setPublished(result.message);
      setDirty(false);
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,420px)]">
      <div className="flex flex-col gap-5">
        {initialPolicy.is_default ? (
          <Callout tone="warn" title="Using the built-in default">
            {teamName} has never published its own policy, so the shipped default is in effect.
            Adjust anything below and publish to make it this team&rsquo;s own.
          </Callout>
        ) : null}

        {/* ── capacity ─────────────────────────────────────────────── */}
        <Card>
          <CardHeader
            title="Capacity by label"
            description="A multiplier on how much work someone carries. 0.4 means 40% of a normal load."
          />
          <CardBody className="flex flex-col gap-4">
            <Callout tone="accent" title="How this works">
              Effective load is <strong>open tasks ÷ capacity</strong>. A new joiner on 0.4
              holding 2 proposals scores like someone holding 5, so the engine stops feeding
              them work sooner. Nothing in the code special-cases a new joiner.
            </Callout>

            <Field label="Default capacity" hint="Applied to anyone with no matching label.">
              <Input
                type="number"
                step="0.1"
                min="0.1"
                max="2"
                value={defaultCapacity}
                onChange={(e) => {
                  setDefaultCapacity(Number(e.target.value));
                  touch();
                }}
                className="max-w-28"
              />
            </Field>

            <div className="flex flex-col gap-2">
              {categoryLabels.map((label) => (
                <div key={label.key} className="flex items-center gap-3">
                  <div className="w-40 shrink-0">
                    <div className="text-[13px] text-ink">{label.name}</div>
                    <div className="font-mono text-[10.5px] text-ink-3">{label.key}</div>
                  </div>
                  <Input
                    type="number"
                    step="0.1"
                    min="0.1"
                    max="2"
                    value={capacity[label.key] ?? defaultCapacity}
                    onChange={(e) => {
                      setCapacity({ ...capacity, [label.key]: Number(e.target.value) });
                      touch();
                    }}
                    className="max-w-24"
                    aria-label={`Capacity for ${label.name}`}
                  />
                  <div className="min-w-0 flex-1">
                    <Bar value={capacity[label.key] ?? defaultCapacity} max={1.5} />
                  </div>
                  <span className="tabular w-16 shrink-0 text-right font-mono text-[11px] text-ink-3">
                    max {maxOpenByLabel[label.key] ?? maxOpenDefault}
                  </span>
                </div>
              ))}
              {categoryLabels.length === 0 ? (
                <p className="text-[13px] text-ink-3">
                  No category labels exist yet. Create some under Labels first.
                </p>
              ) : null}
            </div>
          </CardBody>
        </Card>

        {/* ── eligibility ──────────────────────────────────────────── */}
        <Card>
          <CardHeader
            title="Eligibility"
            description="Hard filters, applied before anyone is scored."
          />
          <CardBody className="flex flex-col gap-4">
            <Checkbox
              label="Skip anyone currently on approved leave"
              checked={respectLeave}
              onChange={(e) => {
                setRespectLeave(e.target.checked);
                touch();
              }}
            />

            <Field
              label="Maximum open proposals"
              hint="A hard ceiling. Nobody is assigned past this, whatever their score."
            >
              <Input
                type="number"
                min="1"
                max="50"
                value={maxOpenDefault}
                onChange={(e) => {
                  setMaxOpenDefault(Number(e.target.value));
                  touch();
                }}
                className="max-w-28"
              />
            </Field>

            <div className="flex flex-col gap-2">
              <span className="text-[12px] font-medium text-ink">Per-label ceilings</span>
              {categoryLabels.map((label) => (
                <div key={label.key} className="flex items-center gap-3">
                  <span className="w-40 shrink-0 text-[13px] text-ink-2">{label.name}</span>
                  <Input
                    type="number"
                    min="1"
                    max="50"
                    value={maxOpenByLabel[label.key] ?? maxOpenDefault}
                    onChange={(e) => {
                      setMaxOpenByLabel({ ...maxOpenByLabel, [label.key]: Number(e.target.value) });
                      touch();
                    }}
                    className="max-w-24"
                    aria-label={`Maximum open proposals for ${label.name}`}
                  />
                </div>
              ))}
            </div>

            <Field
              label="Never assign to"
              hint="Holders of these labels are excluded entirely. Replaces the legacy exclude list."
            >
              <div className="flex flex-wrap gap-2">
                {statusLabels.map((label) => {
                  const on = excluded.includes(label.key);
                  return (
                    <button
                      key={label.key}
                      type="button"
                      onClick={() => {
                        setExcluded(
                          on ? excluded.filter((k) => k !== label.key) : [...excluded, label.key],
                        );
                        touch();
                      }}
                      aria-pressed={on}
                      className={
                        on
                          ? "rounded-[--radius-xs] border border-danger bg-danger-soft px-2 py-1 font-mono text-[11px] text-danger"
                          : "rounded-[--radius-xs] border border-line bg-surface px-2 py-1 font-mono text-[11px] text-ink-3 hover:border-ink-3"
                      }
                    >
                      {label.key}
                    </button>
                  );
                })}
              </div>
            </Field>
          </CardBody>
        </Card>

        {/* ── distribution ─────────────────────────────────────────── */}
        <Card>
          <CardHeader title="Distribution" description="How the winner is chosen." />
          <CardBody className="flex flex-col gap-4">
            <Field label="Mode">
              <Select
                value={mode}
                onChange={(e) => {
                  setMode(e.target.value);
                  touch();
                }}
              >
                {MODES.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </Select>
              <p className="text-[11.5px] text-ink-3">
                {MODES.find((m) => m.value === mode)?.hint}
              </p>
            </Field>

            {mode === "ratio" ? (
              <Field
                label="Target ratio"
                hint="Relative shares, measured over a rolling 30 days so short-term luck corrects itself."
              >
                <div className="flex flex-col gap-2">
                  {categoryLabels.map((label) => (
                    <div key={label.key} className="flex items-center gap-3">
                      <span className="w-40 shrink-0 text-[13px] text-ink-2">{label.name}</span>
                      <Input
                        type="number"
                        min="0"
                        max="100"
                        value={ratioTargets[label.key] ?? 0}
                        onChange={(e) => {
                          setRatioTargets({ ...ratioTargets, [label.key]: Number(e.target.value) });
                          touch();
                        }}
                        className="max-w-24"
                        aria-label={`Ratio target for ${label.name}`}
                      />
                    </div>
                  ))}
                </div>
              </Field>
            ) : null}

            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Tie-break" hint="When two candidates score identically.">
                <Select
                  value={tieBreak}
                  onChange={(e) => {
                    setTieBreak(e.target.value);
                    touch();
                  }}
                >
                  {TIE_BREAKS.map((t) => (
                    <option key={t} value={t}>
                      {t.replace(/_/g, " ")}
                    </option>
                  ))}
                </Select>
              </Field>

              <Field label="When nobody is eligible" hint="Work is never silently dropped.">
                <Select
                  value={fallback}
                  onChange={(e) => {
                    setFallback(e.target.value);
                    touch();
                  }}
                >
                  {FALLBACKS.map((f) => (
                    <option key={f} value={f}>
                      {f.replace(/_/g, " ")}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>

            <Checkbox
              label="Allow managers to override the engine (reason required, always audited)"
              checked={allowOverride}
              onChange={(e) => {
                setAllowOverride(e.target.checked);
                touch();
              }}
            />
          </CardBody>
        </Card>
      </div>

      {/* ── preview rail ───────────────────────────────────────────── */}
      <div className="flex flex-col gap-4 xl:sticky xl:top-20 xl:self-start">
        <Card>
          <CardHeader
            title="Preview"
            description="Who would get the next 10 proposals under these settings."
          />
          <CardBody className="flex flex-col gap-3">
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" onClick={runPreview} disabled={busy !== null}>
                {busy === "preview" ? "Simulating…" : "Simulate"}
              </Button>
              <Button
                variant="secondary"
                onClick={publish}
                disabled={busy !== null || !canPublish || !preview}
                title={
                  !canPublish
                    ? "You need rules.publish for this team"
                    : !preview
                      ? "Simulate first, so you can see what will change"
                      : undefined
                }
              >
                {busy === "publish" ? "Publishing…" : "Publish"}
              </Button>
            </div>

            {!preview && !error ? (
              <p className="text-[12.5px] leading-relaxed text-ink-3">
                Publishing is deliberately gated behind a simulation. Run one to see the effect
                before it becomes real.
              </p>
            ) : null}

            {dirty && preview ? (
              <Callout tone="warn">Settings changed since this preview. Simulate again.</Callout>
            ) : null}

            {error ? <Callout tone="danger" title="Failed">{error}</Callout> : null}
            {published ? <Callout tone="good" title="Published">{published}</Callout> : null}
          </CardBody>
        </Card>

        {preview ? <PreviewPanel preview={preview} /> : <CurrentPanel candidates={candidates} />}
      </div>
    </div>
  );
}

function PreviewPanel({ preview }: { preview: AssignmentPreview }): ReactNode {
  const distribution = Object.entries(preview.distribution ?? {}).sort((a, b) => b[1] - a[1]);
  const maxShare = Math.max(...distribution.map(([, n]) => n), 1);

  return (
    <>
      {preview.warning ? (
        <Callout tone="warn" title="Nothing to assign">{preview.warning}</Callout>
      ) : null}

      {distribution.length > 0 ? (
        <Card>
          <CardHeader title="Spread over 10" description="How the next ten would land." />
          <CardBody className="flex flex-col gap-2.5">
            {distribution.map(([name, count]) => (
              <div key={name}>
                <div className="mb-1 flex items-baseline justify-between gap-2">
                  <span className="truncate text-[13px] text-ink">{name}</span>
                  <span className="tabular shrink-0 font-mono text-[11px] text-ink-2">{count}</span>
                </div>
                <Bar value={count} max={maxShare} />
              </div>
            ))}
          </CardBody>
        </Card>
      ) : null}

      <Card>
        <CardHeader title="Order" description="Each row explains itself." />
        {preview.assignments.length === 0 ? (
          <EmptyState title="Nobody eligible" />
        ) : (
          <ol className="flex flex-col">
            {preview.assignments.map((a) => (
              <li
                key={a.position}
                className="border-b border-line-2 px-5 py-2.5 last:border-b-0"
              >
                <div className="flex items-baseline gap-2">
                  <span className="tabular font-mono text-[10.5px] text-ink-3">
                    {String(a.position).padStart(2, "0")}
                  </span>
                  <span className="text-[13px] font-medium text-ink">
                    {a.assignee_name ?? "— unassigned —"}
                  </span>
                  {a.fallback ? <Badge tone="warn">{a.fallback}</Badge> : null}
                </div>
                <p className="mt-0.5 pl-6 text-[11.5px] leading-relaxed text-ink-3">
                  {a.explanation}
                </p>
              </li>
            ))}
          </ol>
        )}
      </Card>

      {preview.excluded && preview.excluded.length > 0 ? (
        <Card>
          <CardHeader
            title="Excluded"
            description="Why someone was not considered — as common a question as why someone was."
          />
          <Table>
            <tbody>
              {preview.excluded.map((e) => (
                <tr key={e.user_id}>
                  <Td className="text-ink">{e.display_name}</Td>
                  <Td className="text-ink-3">{e.reason}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      ) : null}
    </>
  );
}

function CurrentPanel({ candidates }: { candidates: Candidate[] }): ReactNode {
  if (candidates.length === 0) {
    return (
      <Card>
        <EmptyState
          title="No active members"
          description="Add people to this team before configuring how work is distributed."
        />
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader title="Current load" description="What the engine sees right now." />
      <Table>
        <thead>
          <tr>
            <Th>Member</Th>
            <Th className="num text-right">Open</Th>
            <Th className="num text-right">Cap</Th>
            <Th className="num text-right">Load</Th>
          </tr>
        </thead>
        <tbody>
          {candidates.map((c) => (
            <tr key={c.user_id}>
              <Td>
                <div className="text-ink">{c.display_name}</div>
                {c.labels.length > 0 ? (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {c.labels.map((l) => (
                      <Badge key={l}>{l}</Badge>
                    ))}
                  </div>
                ) : null}
              </Td>
              <Td className="num text-right">{c.open_task_count}</Td>
              <Td className="num text-right">{c.capacity}</Td>
              <Td className="num text-right font-medium text-ink">
                {c.effective_load.toFixed(1)}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    const suffix = error.correlationId ? ` (ref ${error.correlationId})` : "";
    return `${error.message}${suffix}`;
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}
