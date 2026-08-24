"use client";

import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { Badge, Button, Callout, Select } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { Label, Member, SystemRole } from "@/lib/types";

/**
 * Per-member role and label controls.
 *
 * The backend refuses to remove the last super admin and explains why — that error is
 * surfaced verbatim rather than replaced with a generic failure, because it is the one
 * message that stops someone locking the whole organisation out.
 */
export function MemberControls({
  teamId,
  member,
  roles,
  labels,
  canManageUsers,
  canAssignLabels,
}: {
  teamId: string;
  member: Member;
  roles: SystemRole[];
  labels: Label[];
  canManageUsers: boolean;
  canAssignLabels: boolean;
}): ReactNode {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(fn: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await fn();
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "That did not work.");
    } finally {
      setBusy(false);
    }
  }

  const available = labels.filter((l) => !member.labels.includes(l.key));

  if (!open) {
    return (
      <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
        Manage
      </Button>
    );
  }

  return (
    <div className="flex min-w-56 flex-col gap-2.5 rounded-[--radius-sm] border border-line bg-surface-2 p-3">
      <div className="flex items-center justify-between">
        <span className="eyebrow">{member.display_name}</span>
        <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
          Close
        </Button>
      </div>

      {canManageUsers ? (
        <label className="flex flex-col gap-1">
          <span className="text-[11.5px] text-ink-3">Role</span>
          <Select
            defaultValue={member.role}
            disabled={busy}
            onChange={(e) =>
              run(() =>
                api.patch(`/admin/teams/${teamId}/members/${member.user_id}`, {
                  role_key: e.target.value,
                }),
              )
            }
          >
            {roles.map((role) => (
              <option key={role.key} value={role.key}>
                {role.name}
              </option>
            ))}
          </Select>
        </label>
      ) : null}

      {canAssignLabels ? (
        <div className="flex flex-col gap-1.5">
          <span className="text-[11.5px] text-ink-3">Labels</span>
          {member.labels.length > 0 ? (
            <div className="flex flex-wrap gap-1">
              {member.labels.map((key) => (
                <button
                  key={key}
                  type="button"
                  disabled={busy}
                  title="Remove this label"
                  onClick={() =>
                    run(() =>
                      api.delete("/admin/labels/assign", {
                        user_id: member.user_id,
                        label_key: key,
                        team_id: teamId,
                      }),
                    )
                  }
                  className="rounded-[--radius-xs] border border-accent bg-accent-soft px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.06em] text-accent-ink hover:border-danger hover:bg-danger-soft hover:text-danger"
                >
                  {key} ×
                </button>
              ))}
            </div>
          ) : (
            <Badge tone="neutral">no labels</Badge>
          )}

          {available.length > 0 ? (
            <Select
              value=""
              disabled={busy}
              onChange={(e) => {
                if (!e.target.value) return;
                void run(() =>
                  api.post("/admin/labels/assign", {
                    user_id: member.user_id,
                    label_key: e.target.value,
                    team_id: teamId,
                  }),
                );
              }}
            >
              <option value="">Add a label…</option>
              {available.map((label) => (
                <option key={label.key} value={label.key}>
                  {label.name} ({label.kind})
                </option>
              ))}
            </Select>
          ) : null}
        </div>
      ) : null}

      {canManageUsers ? (
        <Button
          variant="danger"
          size="sm"
          disabled={busy}
          onClick={() =>
            run(() => api.delete(`/admin/teams/${teamId}/members/${member.user_id}`))
          }
        >
          Remove from team
        </Button>
      ) : null}

      {error ? <Callout tone="danger">{error}</Callout> : null}
    </div>
  );
}
