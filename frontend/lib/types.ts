/**
 * Response shapes from the FastAPI backend.
 *
 * These mirror the Pydantic models. They are hand-written for now; once the backend is
 * running, `openapi-typescript` against /openapi.json should generate them so the two
 * cannot drift.
 */

export type Scope = "own" | "team" | "all";

export interface Permission {
  key: string;
  module: string;
  description: string;
  scopes: Scope[];
}

export interface PermissionModule {
  module: string;
  permissions: Permission[];
}

export interface SystemRole {
  key: string;
  name: string;
  description: string;
  is_team_scoped: boolean;
  grants: Record<string, Scope>;
}

export interface TeamGrant {
  team_id: string;
  slug: string;
  role: string;
  labels: string[];
  permissions: Record<string, Scope>;
}

export interface Me {
  user_id: string;
  email: string;
  display_name: string;
  is_super_admin: boolean;
  org_permissions: Record<string, Scope>;
  teams: TeamGrant[];
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Team {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  lead_user_id: string | null;
  enabled_modules: string[];
  archived: boolean;
  member_count: number | null;
}

export interface Member {
  user_id: string;
  email: string;
  display_name: string;
  status: string;
  role: string;
  role_name: string;
  labels: string[];
  joined_at: string | null;
}

export type LabelKind = "category" | "skill" | "status";

export interface Label {
  id: string;
  key: string;
  name: string;
  kind: LabelKind;
  description: string | null;
  color: string | null;
  team_id: string | null;
}

// ── rules engine ────────────────────────────────────────────────────────

export type FactType = "string" | "number" | "boolean" | "date" | "string_set" | "uuid";

export interface Fact {
  key: string;
  type: FactType;
  description: string;
  operators: string[];
  choices: string[];
}

export interface ActionParam {
  key: string;
  type: FactType;
  description: string;
  required: boolean;
  choices: string[];
}

export interface RuleAction {
  type: string;
  description: string;
  params: ActionParam[];
}

export interface DecisionPoint {
  key: string;
  name: string;
  description: string;
  fires_when: string;
  entity_type: string;
  facts: Fact[];
  actions: RuleAction[];
}

/** A leaf condition. Trees nest via `all` / `any` / `none`. */
export interface ConditionLeaf {
  fact: string;
  op: string;
  value?: unknown;
}

export type ConditionNode =
  | { always: true }
  | { all: ConditionNode[] }
  | { any: ConditionNode[] }
  | { none: ConditionNode[] }
  | ConditionLeaf;

export interface Rule {
  id?: string;
  name: string;
  position: number;
  conditions: ConditionNode | Record<string, unknown>;
  actions: Array<Record<string, unknown>>;
  enabled: boolean;
}

export interface RuleSet {
  id: string;
  decision_point: string;
  team_id: string | null;
  name: string;
  description: string | null;
  enabled: boolean;
  priority: number;
  evaluate_all: boolean;
  version: number;
  published_at: string | null;
  rules: Rule[];
}

export interface RuleSetVersion {
  version: number;
  note: string | null;
  author_id: string | null;
  created_at: string;
  rule_count: number;
}

export interface ConditionTrace {
  fact: string;
  op: string;
  expected: unknown;
  actual: unknown;
  passed: boolean;
  note: string | null;
}

export interface RuleTrace {
  rule_id: string;
  name: string;
  position: number;
  matched: boolean;
  skipped_reason: string | null;
  conditions: ConditionTrace[];
  actions: Array<Record<string, unknown>>;
}

export interface SimulationResult {
  facts: Record<string, unknown>;
  matched: boolean;
  matched_rule_ids: string[];
  matched_rule_names: string[];
  actions: Array<Record<string, unknown>>;
  trace: RuleTrace[];
  unrecognised_facts: string[];
}

// ── assignment (§5.4) ───────────────────────────────────────────────────

export interface AssignmentPolicy {
  id: string | null;
  team_id: string;
  name: string;
  version: number;
  active: boolean;
  eligibility: {
    not_on_leave?: boolean;
    not_labelled?: string[];
    requires_labels?: string[];
    max_open_tasks?: { default?: number; by_label?: Record<string, number> };
  };
  capacity: { default?: number; by_label?: Record<string, number> };
  distribution: {
    mode?: string;
    factors?: Record<string, { weight: number; direction: string }>;
    ratio?: { by?: string; targets?: Record<string, number>; window?: string };
  };
  tie_break: string;
  fallback: string;
  allow_manual_override: boolean;
  is_default: boolean;
}

export interface RankedCandidate {
  user_id: string;
  display_name: string;
  score: number;
  capacity: number;
  effective_load: number;
  breakdown: Record<string, number>;
}

export interface ExcludedCandidate {
  user_id: string;
  display_name: string;
  reason: string;
}

export interface PreviewAssignment {
  position: number;
  assignee_id: string | null;
  assignee_name: string | null;
  explanation: string;
  fallback: string | null;
}

export interface AssignmentPreview {
  assignments: PreviewAssignment[];
  distribution?: Record<string, number>;
  candidates: RankedCandidate[];
  excluded?: ExcludedCandidate[];
  policy?: AssignmentPolicy;
  warning?: string;
}

export interface Candidate {
  user_id: string;
  display_name: string;
  labels: string[];
  open_task_count: number;
  capacity: number;
  effective_load: number;
  on_leave: boolean;
  recent_assignments: number;
}

// ── proposals ───────────────────────────────────────────────────────────

export type ProposalStatus =
  | "new"
  | "assigned"
  | "in_progress"
  | "submitted"
  | "won"
  | "lost"
  | "cancelled";

export interface Proposal {
  id: string;
  team_id: string;
  external_ref: string | null;
  title: string;
  customer_name: string | null;
  status: ProposalStatus;
  assigned_to: string | null;
  bcd: string | null;
  estimated_value: number | null;
  currency: string | null;
  priority_score: number;
  required_labels: string[];
  source: string;
  created_at: string;
}

export interface ProposalEvent {
  id: string;
  type: string;
  actor_id: string | null;
  payload: Record<string, unknown> | null;
  rule_evaluation_id: string | null;
  created_at: string;
}

export interface AssignResult {
  assigned: boolean;
  assignee_id: string | null;
  explanation: string;
  fallback: string | null;
  ranked: RankedCandidate[];
  excluded: ExcludedCandidate[];
}

export interface WorkloadRow {
  user_id: string;
  display_name: string;
  labels: string[];
  open_task_count: number;
  capacity: number;
  effective_load: number;
  on_leave: boolean;
}

// ── developer panel ─────────────────────────────────────────────────────

export interface SystemOverview {
  environment: string;
  sharepoint_mode: string;
  sharepoint_sandbox_site: string;
  outbound_email_enabled: boolean;
  jobs_last_hour: number;
  jobs_failed_last_hour: number;
  rule_evaluations_last_hour: number;
  audit_entries_last_hour: number;
  captured_emails: number;
}

export interface JobRun {
  id: string;
  task_name: string;
  task_id: string | null;
  status: "pending" | "running" | "success" | "failed" | "retrying" | "cancelled";
  args: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  error: string | null;
  retries: number;
  correlation_id: string | null;
}

export interface TaskSummary {
  task_name: string;
  runs: number;
  failures: number;
  avg_duration_ms: number | null;
  last_run: string | null;
}

export interface Connector {
  name: string;
  mode: string;
  healthy: boolean;
  last_success_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  latency_ms: number | null;
  detail: Record<string, unknown> | null;
}

export interface RuleEvaluation {
  id: string;
  decision_point: string;
  rule_set_id: string | null;
  rule_set_version: number | null;
  team_id: string | null;
  entity_type: string | null;
  entity_id: string | null;
  facts: Record<string, unknown>;
  matched_rule_ids: string[];
  trace: RuleTrace[] | null;
  outcome: Record<string, unknown>;
  simulated: boolean;
  duration_ms: number | null;
  correlation_id: string | null;
  created_at: string;
}

export interface AuditEntry {
  id: string;
  actor_id: string | null;
  team_id: string | null;
  action: string;
  entity_type: string;
  entity_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  correlation_id: string | null;
  created_at: string;
}

export interface OutboxEntry {
  id: string;
  to_addresses: string[];
  subject: string;
  body: string;
  status: string;
  created_at: string;
}

export interface FeatureFlag {
  key: string;
  description: string | null;
  enabled: boolean;
  rules: Record<string, unknown>;
}

/** RFC 7807 problem document — every error the backend returns. */
export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string;
  correlation_id: string | null;
  permission?: string;
  scope?: string;
  errors?: unknown[];
}
