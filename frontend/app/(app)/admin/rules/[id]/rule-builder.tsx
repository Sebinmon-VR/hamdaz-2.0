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
  EmptyState,
  Field,
  Input,
  Select,
  StatusDot,
  cn,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import { summarise } from "@/lib/format";
import type {
  ConditionLeaf,
  DecisionPoint,
  Rule,
  RuleSet,
  RuleSetVersion,
  SimulationResult,
} from "@/lib/types";

type Combinator = "all" | "any" | "always";

interface DraftRule {
  name: string;
  position: number;
  combinator: Combinator;
  leaves: ConditionLeaf[];
  actions: Array<Record<string, unknown>>;
  enabled: boolean;
}

/**
 * The rule builder.
 *
 * Everything selectable here comes from the decision point's declared facts and actions, so
 * a rule that cannot be evaluated cannot be authored. The backend validates again on save —
 * this is convenience, not enforcement.
 */
export function RuleBuilder({
  ruleSet,
  decisionPoint,
  versions,
  canEdit,
  canPublish,
  canSimulate,
}: {
  ruleSet: RuleSet;
  decisionPoint: DecisionPoint;
  versions: RuleSetVersion[];
  canEdit: boolean;
  canPublish: boolean;
  canSimulate: boolean;
}): ReactNode {
  const router = useRouter();
  const [rules, setRules] = useState<DraftRule[]>(() => ruleSet.rules.map(toDraft));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [simulation, setSimulation] = useState<SimulationResult[] | null>(null);
  const [factInput, setFactInput] = useState("{\n  \n}");

  function update(next: DraftRule[]): void {
    setRules(next);
    setDirty(true);
    setSimulation(null);
    setNotice(null);
  }

  function addRule(): void {
    update([
      ...rules,
      {
        name: `Rule ${rules.length + 1}`,
        position: rules.length,
        combinator: "all",
        leaves: [
          {
            fact: decisionPoint.facts[0]?.key ?? "",
            op: decisionPoint.facts[0]?.operators[0] ?? "=",
            value: "",
          },
        ],
        actions: [{ type: decisionPoint.actions[0]?.type ?? "" }],
        enabled: true,
      },
    ]);
  }

  async function save(): Promise<void> {
    setBusy("save");
    setError(null);
    try {
      await api.put(`/rules/sets/${ruleSet.id}/rules`, {
        rules: rules.map((r, index) => ({
          name: r.name,
          position: index,
          conditions: toConditions(r),
          actions: r.actions,
          enabled: r.enabled,
        })),
      });
      setDirty(false);
      setNotice("Saved as a draft. Publish to make it live.");
      router.refresh();
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(null);
    }
  }

  async function simulate(): Promise<void> {
    setBusy("simulate");
    setError(null);
    try {
      const parsed: unknown = JSON.parse(factInput);
      const factSets = Array.isArray(parsed) ? parsed : [parsed];
      const result = await api.post<{ results: SimulationResult[] }>(
        `/rules/sets/${ruleSet.id}/simulate`,
        { fact_sets: factSets },
      );
      setSimulation(result.results);
    } catch (err) {
      setError(
        err instanceof SyntaxError
          ? "The sample facts are not valid JSON."
          : describe(err),
      );
    } finally {
      setBusy(null);
    }
  }

  async function publish(): Promise<void> {
    setBusy("publish");
    setError(null);
    try {
      const result = await api.post<{ message: string }>(
        `/rules/sets/${ruleSet.id}/publish`,
        { enable: true },
      );
      setNotice(result.message);
      router.refresh();
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(null);
    }
  }

  async function revert(version: number): Promise<void> {
    setBusy(`revert-${version}`);
    setError(null);
    try {
      const result = await api.post<{ message: string }>(
        `/rules/sets/${ruleSet.id}/revert/${version}`,
      );
      setNotice(result.message);
      router.refresh();
    } catch (err) {
      setError(describe(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,400px)]">
      <div className="flex flex-col gap-5">
        <Card>
          <CardHeader
            title="Rules"
            description="Evaluated top to bottom. The first match wins unless the set evaluates all."
            action={
              canEdit ? (
                <Button variant="secondary" size="sm" onClick={addRule}>
                  Add rule
                </Button>
              ) : null
            }
          />
          {rules.length === 0 ? (
            <EmptyState
              title="No rules yet"
              description="Add one to describe when this decision point should do something."
              action={canEdit ? <Button onClick={addRule}>Add the first rule</Button> : null}
            />
          ) : (
            <div className="divide-y divide-line-2">
              {rules.map((rule, index) => (
                <RuleEditor
                  key={index}
                  rule={rule}
                  index={index}
                  decisionPoint={decisionPoint}
                  canEdit={canEdit}
                  onChange={(next) =>
                    update(rules.map((r, i) => (i === index ? next : r)))
                  }
                  onRemove={() => update(rules.filter((_, i) => i !== index))}
                  onMove={(direction) => {
                    const target = index + direction;
                    if (target < 0 || target >= rules.length) return;
                    const next = [...rules];
                    const a = next[index]!;
                    const b = next[target]!;
                    next[index] = b;
                    next[target] = a;
                    update(next);
                  }}
                />
              ))}
            </div>
          )}
        </Card>

        {error ? <Callout tone="danger" title="Failed">{error}</Callout> : null}
        {notice ? <Callout tone="good">{notice}</Callout> : null}

        <div className="flex flex-wrap gap-2">
          {canEdit ? (
            <Button variant="primary" onClick={save} disabled={busy !== null || !dirty}>
              {busy === "save" ? "Saving…" : "Save draft"}
            </Button>
          ) : null}
          {canPublish ? (
            <Button
              variant="secondary"
              onClick={publish}
              disabled={busy !== null || dirty || rules.length === 0}
              title={dirty ? "Save your changes first" : undefined}
            >
              {busy === "publish" ? "Publishing…" : "Publish"}
            </Button>
          ) : null}
        </div>
      </div>

      <div className="flex flex-col gap-5 xl:sticky xl:top-20 xl:self-start">
        {canSimulate ? (
          <Card>
            <CardHeader
              title="Simulate"
              description="Runs the same evaluator the live path uses."
            />
            <CardBody className="flex flex-col gap-3">
              <Field
                label="Sample facts"
                hint="A JSON object, or an array of objects to test several cases at once."
              >
                <textarea
                  value={factInput}
                  onChange={(e) => setFactInput(e.target.value)}
                  spellCheck={false}
                  className="min-h-32 w-full rounded-[--radius-sm] border border-line bg-surface p-2.5 font-mono text-[12px] text-ink focus:border-accent"
                />
              </Field>

              <div className="flex flex-wrap gap-1.5">
                {decisionPoint.facts.slice(0, 8).map((fact) => (
                  <button
                    key={fact.key}
                    type="button"
                    title={fact.description}
                    onClick={() =>
                      setFactInput((current) => insertFact(current, fact.key, fact.type))
                    }
                    className="rounded-[--radius-xs] border border-line px-1.5 py-0.5 font-mono text-[10px] text-ink-3 hover:border-accent hover:text-accent"
                  >
                    + {fact.key}
                  </button>
                ))}
              </div>

              <Button variant="primary" onClick={simulate} disabled={busy !== null}>
                {busy === "simulate" ? "Running…" : "Run simulation"}
              </Button>
            </CardBody>
          </Card>
        ) : null}

        {simulation ? <SimulationPanel results={simulation} /> : null}

        {versions.length > 0 ? (
          <Card>
            <CardHeader title="Versions" description="Any version can be restored." />
            <div className="divide-y divide-line-2">
              {versions.map((version) => (
                <div key={version.version} className="flex items-center gap-3 px-5 py-2.5">
                  <span className="tabular font-mono text-[11px] text-ink-3">
                    v{version.version}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[12.5px] text-ink-2">
                      {version.note ?? `${version.rule_count} rules`}
                    </div>
                  </div>
                  {canPublish && version.version !== ruleSet.version ? (
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={busy !== null}
                      onClick={() => revert(version.version)}
                    >
                      Restore
                    </Button>
                  ) : (
                    <Badge tone="good">current</Badge>
                  )}
                </div>
              ))}
            </div>
          </Card>
        ) : null}
      </div>
    </div>
  );
}

// ── one rule ────────────────────────────────────────────────────────────

function RuleEditor({
  rule,
  index,
  decisionPoint,
  canEdit,
  onChange,
  onRemove,
  onMove,
}: {
  rule: DraftRule;
  index: number;
  decisionPoint: DecisionPoint;
  canEdit: boolean;
  onChange: (next: DraftRule) => void;
  onRemove: () => void;
  onMove: (direction: -1 | 1) => void;
}): ReactNode {
  return (
    <div className="p-5">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="tabular font-mono text-[11px] text-ink-3">
          {String(index + 1).padStart(2, "0")}
        </span>
        <input
          value={rule.name}
          disabled={!canEdit}
          onChange={(e) => onChange({ ...rule, name: e.target.value })}
          className="min-w-0 flex-1 border-b border-transparent bg-transparent text-[14px] font-medium text-ink hover:border-line focus:border-accent focus:outline-none"
        />
        {!rule.enabled ? <Badge tone="neutral">disabled</Badge> : null}
        {canEdit ? (
          <div className="flex gap-1">
            <Button variant="ghost" size="sm" onClick={() => onMove(-1)} aria-label="Move up">
              ↑
            </Button>
            <Button variant="ghost" size="sm" onClick={() => onMove(1)} aria-label="Move down">
              ↓
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => onChange({ ...rule, enabled: !rule.enabled })}
            >
              {rule.enabled ? "Disable" : "Enable"}
            </Button>
            <Button variant="ghost" size="sm" onClick={onRemove} aria-label="Remove rule">
              ✕
            </Button>
          </div>
        ) : null}
      </div>

      {/* conditions */}
      <div className="mb-3 rounded-[--radius-sm] border border-line bg-surface-2 p-3">
        <div className="mb-2 flex items-center gap-2">
          <span className="eyebrow">When</span>
          <Select
            value={rule.combinator}
            disabled={!canEdit}
            onChange={(e) => onChange({ ...rule, combinator: e.target.value as Combinator })}
            className="h-7 w-auto text-[12px]"
          >
            <option value="all">all of these are true</option>
            <option value="any">any of these is true</option>
            <option value="always">always (a catch-all)</option>
          </Select>
        </div>

        {rule.combinator !== "always" ? (
          <div className="flex flex-col gap-2">
            {rule.leaves.map((leaf, leafIndex) => {
              const fact = decisionPoint.facts.find((f) => f.key === leaf.fact);
              return (
                <div key={leafIndex} className="flex flex-wrap items-center gap-1.5">
                  <Select
                    value={leaf.fact}
                    disabled={!canEdit}
                    onChange={(e) => {
                      const nextFact = decisionPoint.facts.find((f) => f.key === e.target.value);
                      onChange({
                        ...rule,
                        leaves: rule.leaves.map((l, i) =>
                          i === leafIndex
                            ? { fact: e.target.value, op: nextFact?.operators[0] ?? "=", value: "" }
                            : l,
                        ),
                      });
                    }}
                    className="h-8 w-auto min-w-44 text-[12px]"
                  >
                    {decisionPoint.facts.map((f) => (
                      <option key={f.key} value={f.key}>
                        {f.key}
                      </option>
                    ))}
                  </Select>

                  <Select
                    value={leaf.op}
                    disabled={!canEdit}
                    onChange={(e) =>
                      onChange({
                        ...rule,
                        leaves: rule.leaves.map((l, i) =>
                          i === leafIndex ? { ...l, op: e.target.value } : l,
                        ),
                      })
                    }
                    className="h-8 w-auto text-[12px]"
                  >
                    {(fact?.operators ?? ["="]).map((op) => (
                      <option key={op} value={op}>
                        {op}
                      </option>
                    ))}
                  </Select>

                  {needsValue(leaf.op) ? (
                    fact?.choices.length ? (
                      <Select
                        value={String(leaf.value ?? "")}
                        disabled={!canEdit}
                        onChange={(e) =>
                          onChange({
                            ...rule,
                            leaves: rule.leaves.map((l, i) =>
                              i === leafIndex ? { ...l, value: e.target.value } : l,
                            ),
                          })
                        }
                        className="h-8 w-auto text-[12px]"
                      >
                        {fact.choices.map((choice) => (
                          <option key={choice} value={choice}>
                            {choice}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <Input
                        value={String(leaf.value ?? "")}
                        disabled={!canEdit}
                        placeholder={fact?.type === "number" ? "0" : "value"}
                        onChange={(e) =>
                          onChange({
                            ...rule,
                            leaves: rule.leaves.map((l, i) =>
                              i === leafIndex
                                ? {
                                    ...l,
                                    value:
                                      fact?.type === "number" && e.target.value !== ""
                                        ? Number(e.target.value)
                                        : e.target.value,
                                  }
                                : l,
                            ),
                          })
                        }
                        className="h-8 w-32 text-[12px]"
                      />
                    )
                  ) : null}

                  {canEdit && rule.leaves.length > 1 ? (
                    <button
                      type="button"
                      onClick={() =>
                        onChange({
                          ...rule,
                          leaves: rule.leaves.filter((_, i) => i !== leafIndex),
                        })
                      }
                      className="px-1 text-[13px] text-ink-3 hover:text-danger"
                      aria-label="Remove condition"
                    >
                      ✕
                    </button>
                  ) : null}
                </div>
              );
            })}

            {canEdit ? (
              <Button
                variant="ghost"
                size="sm"
                className="self-start"
                onClick={() =>
                  onChange({
                    ...rule,
                    leaves: [
                      ...rule.leaves,
                      {
                        fact: decisionPoint.facts[0]?.key ?? "",
                        op: decisionPoint.facts[0]?.operators[0] ?? "=",
                        value: "",
                      },
                    ],
                  })
                }
              >
                + condition
              </Button>
            ) : null}
          </div>
        ) : (
          <p className="text-[12px] text-ink-3">
            Matches every time. Put this last, as the default.
          </p>
        )}
      </div>

      {/* actions */}
      <div className="rounded-[--radius-sm] border border-line bg-surface-2 p-3">
        <div className="eyebrow mb-2">Then</div>
        <div className="flex flex-col gap-2">
          {rule.actions.map((action, actionIndex) => {
            const definition = decisionPoint.actions.find((a) => a.type === action.type);
            return (
              <div key={actionIndex} className="flex flex-wrap items-center gap-1.5">
                <Select
                  value={String(action.type ?? "")}
                  disabled={!canEdit}
                  onChange={(e) =>
                    onChange({
                      ...rule,
                      actions: rule.actions.map((a, i) =>
                        i === actionIndex ? { type: e.target.value } : a,
                      ),
                    })
                  }
                  className="h-8 w-auto min-w-44 text-[12px]"
                >
                  {decisionPoint.actions.map((a) => (
                    <option key={a.type} value={a.type}>
                      {a.type}
                    </option>
                  ))}
                </Select>

                {definition?.params.map((param) => (
                  <span key={param.key} className="flex items-center gap-1">
                    <span className="font-mono text-[10.5px] text-ink-3">{param.key}</span>
                    {param.choices.length > 0 ? (
                      <Select
                        value={String(action[param.key] ?? "")}
                        disabled={!canEdit}
                        onChange={(e) =>
                          onChange({
                            ...rule,
                            actions: rule.actions.map((a, i) =>
                              i === actionIndex ? { ...a, [param.key]: e.target.value } : a,
                            ),
                          })
                        }
                        className="h-8 w-auto text-[12px]"
                      >
                        <option value="">—</option>
                        {param.choices.map((choice) => (
                          <option key={choice} value={choice}>
                            {choice}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <Input
                        value={String(action[param.key] ?? "")}
                        disabled={!canEdit}
                        placeholder={param.required ? "required" : "optional"}
                        onChange={(e) =>
                          onChange({
                            ...rule,
                            actions: rule.actions.map((a, i) =>
                              i === actionIndex
                                ? {
                                    ...a,
                                    [param.key]:
                                      param.type === "number" && e.target.value !== ""
                                        ? Number(e.target.value)
                                        : e.target.value,
                                  }
                                : a,
                            ),
                          })
                        }
                        className="h-8 w-28 text-[12px]"
                      />
                    )}
                  </span>
                ))}

                {canEdit && rule.actions.length > 1 ? (
                  <button
                    type="button"
                    onClick={() =>
                      onChange({
                        ...rule,
                        actions: rule.actions.filter((_, i) => i !== actionIndex),
                      })
                    }
                    className="px-1 text-[13px] text-ink-3 hover:text-danger"
                    aria-label="Remove action"
                  >
                    ✕
                  </button>
                ) : null}
              </div>
            );
          })}

          {canEdit ? (
            <Button
              variant="ghost"
              size="sm"
              className="self-start"
              onClick={() =>
                onChange({
                  ...rule,
                  actions: [
                    ...rule.actions,
                    { type: decisionPoint.actions[0]?.type ?? "" },
                  ],
                })
              }
            >
              + action
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// ── simulation output ───────────────────────────────────────────────────

function SimulationPanel({ results }: { results: SimulationResult[] }): ReactNode {
  return (
    <Card>
      <CardHeader
        title="Simulation"
        description="Nothing was applied. Every condition is shown, including the ones that failed."
      />
      <div className="divide-y divide-line-2">
        {results.map((result, index) => (
          <div key={index} className="px-5 py-4">
            <div className="mb-2 flex items-center gap-2">
              <StatusDot tone={result.matched ? "good" : "neutral"} />
              <span className="text-[13px] font-medium text-ink">
                {result.matched
                  ? result.matched_rule_names.join(", ") || "matched"
                  : "No rule matched"}
              </span>
            </div>

            {result.unrecognised_facts.length > 0 ? (
              <div className="mb-2">
                <Callout tone="warn" title="Unrecognised facts">
                  {result.unrecognised_facts.join(", ")} — these are ignored by the engine. A
                  typo here is the most common reason a rule &ldquo;does not work&rdquo;.
                </Callout>
              </div>
            ) : null}

            {result.actions.length > 0 ? (
              <div className="mb-2 flex flex-wrap gap-1">
                {result.actions.map((action, i) => (
                  <Badge key={i} tone="accent">
                    {String(action.type)}
                  </Badge>
                ))}
              </div>
            ) : null}

            <details className="mt-1">
              <summary className="cursor-pointer font-mono text-[11px] text-ink-3 hover:text-accent">
                trace ({result.trace.length} rules)
              </summary>
              <div className="mt-2 flex flex-col gap-2">
                {result.trace.map((trace) => (
                  <div
                    key={trace.rule_id}
                    className={cn(
                      "rounded-[--radius-sm] border px-2.5 py-2",
                      trace.matched ? "border-good bg-good-soft" : "border-line bg-surface-2",
                    )}
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <span className="text-[12px] font-medium text-ink">{trace.name}</span>
                      {trace.skipped_reason ? (
                        <span className="font-mono text-[10px] text-ink-3">
                          {trace.skipped_reason}
                        </span>
                      ) : null}
                    </div>
                    {trace.conditions.map((condition, i) => (
                      <div key={i} className="mt-1 font-mono text-[10.5px] leading-relaxed">
                        <span className={condition.passed ? "text-good" : "text-danger"}>
                          {condition.passed ? "✓" : "✕"}
                        </span>{" "}
                        <span className="text-ink-2">
                          {condition.fact} {condition.op} {summarise(condition.expected, 24)}
                        </span>{" "}
                        <span className="text-ink-3">
                          (actual: {summarise(condition.actual, 24)})
                        </span>
                        {condition.note ? (
                          <span className="text-warn"> — {condition.note}</span>
                        ) : null}
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </details>
          </div>
        ))}
      </div>
    </Card>
  );
}

// ── helpers ─────────────────────────────────────────────────────────────

function toDraft(rule: Rule): DraftRule {
  const conditions = rule.conditions as Record<string, unknown>;
  let combinator: Combinator = "all";
  let leaves: ConditionLeaf[] = [];

  if (conditions.always === true) {
    combinator = "always";
  } else if (Array.isArray(conditions.any)) {
    combinator = "any";
    leaves = conditions.any as ConditionLeaf[];
  } else if (Array.isArray(conditions.all)) {
    combinator = "all";
    leaves = conditions.all as ConditionLeaf[];
  } else if (typeof conditions.fact === "string") {
    leaves = [conditions as unknown as ConditionLeaf];
  }

  return {
    name: rule.name,
    position: rule.position,
    combinator,
    // Only flat leaves are editable here; nested trees round-trip through the API untouched.
    leaves: leaves.filter((l) => typeof l?.fact === "string"),
    actions: rule.actions,
    enabled: rule.enabled,
  };
}

function toConditions(rule: DraftRule): Record<string, unknown> {
  if (rule.combinator === "always") return { always: true };
  return { [rule.combinator]: rule.leaves };
}

function needsValue(op: string): boolean {
  return op !== "is_empty" && op !== "is_not_empty";
}

function insertFact(current: string, key: string, type: string): string {
  const sample = type === "number" ? "0" : type === "boolean" ? "false" : '""';
  const trimmed = current.trim();
  if (trimmed === "{}" || trimmed === "{\n  \n}" || trimmed === "") {
    return `{\n  "${key}": ${sample}\n}`;
  }
  const closing = current.lastIndexOf("}");
  if (closing === -1) return current;
  const head = current.slice(0, closing).trimEnd();
  const separator = head.endsWith("{") ? "" : ",";
  return `${head}${separator}\n  "${key}": ${sample}\n}`;
}

function describe(error: unknown): string {
  if (error instanceof ApiError) {
    return `${error.message}${error.correlationId ? ` (ref ${error.correlationId})` : ""}`;
  }
  return error instanceof Error ? error.message : "Something went wrong.";
}
