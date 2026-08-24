"use client";

import { useMemo, useState, type ReactNode } from "react";

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
  cn,
} from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { PermissionModule, Scope, SystemRole } from "@/lib/types";

const SCOPE_LABEL: Record<Scope, string> = {
  own: "Own",
  team: "Team",
  all: "All",
};

/**
 * The permission matrix.
 *
 * Rendered entirely from `/meta/permissions` — the same registry `require()` enforces
 * against. A permission that does not exist in code cannot appear here, and one that does
 * cannot be missed. That single source is the fix for roles having lived in a spreadsheet.
 */
export function RoleMatrix({
  modules,
  roles,
}: {
  modules: PermissionModule[];
  roles: SystemRole[];
}): ReactNode {
  const [creating, setCreating] = useState(false);
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [grants, setGrants] = useState<Record<string, Scope>>({});
  const [cloneFrom, setCloneFrom] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const permissionCount = useMemo(
    () => modules.reduce((sum, m) => sum + m.permissions.length, 0),
    [modules],
  );

  function clone(roleKey: string): void {
    setCloneFrom(roleKey);
    const role = roles.find((r) => r.key === roleKey);
    setGrants(role ? { ...role.grants } : {});
  }

  async function submit(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.post("/admin/roles", {
        key: key.trim().toLowerCase(),
        name: name.trim(),
        grants,
      });
      setDone(`Created ${name.trim()}.`);
      setCreating(false);
      setKey("");
      setName("");
      setGrants({});
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the role.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader
        title="Permission registry"
        description={`${permissionCount} permissions across ${modules.length} modules. Tick to build a custom role.`}
        action={
          creating ? (
            <div className="flex gap-2">
              <Select
                value={cloneFrom}
                onChange={(e) => clone(e.target.value)}
                className="h-7 text-[12px]"
              >
                <option value="">Start empty</option>
                {roles.map((r) => (
                  <option key={r.key} value={r.key}>
                    Clone {r.name}
                  </option>
                ))}
              </Select>
              <Button variant="ghost" size="sm" onClick={() => setCreating(false)}>
                Cancel
              </Button>
            </div>
          ) : (
            <Button variant="primary" size="sm" onClick={() => setCreating(true)}>
              New custom role
            </Button>
          )
        }
      />

      {creating ? (
        <CardBody className="flex flex-wrap items-end gap-4 border-b border-line bg-surface-2">
          <Field label="Key" hint="Lowercase, hyphens or underscores." className="w-44">
            <Input
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="bid_reviewer"
            />
          </Field>
          <Field label="Display name" className="w-52">
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Bid Reviewer"
            />
          </Field>
          <Button
            variant="primary"
            disabled={busy || key.trim().length < 2 || name.trim().length < 2 || !Object.keys(grants).length}
            onClick={submit}
          >
            {busy ? "Creating…" : `Create with ${Object.keys(grants).length} permissions`}
          </Button>
          {error ? (
            <div className="w-full">
              <Callout tone="danger">{error}</Callout>
            </div>
          ) : null}
        </CardBody>
      ) : null}

      {done && !creating ? (
        <div className="border-b border-line px-5 py-3">
          <Callout tone="good">{done}</Callout>
        </div>
      ) : null}

      <div className="flex flex-col">
        {modules.map((module) => (
          <section key={module.module}>
            <div className="sticky top-14 z-10 flex items-center gap-2 border-y border-line bg-surface-2 px-5 py-2">
              <span className="text-[13px] font-semibold text-ink">{module.module}</span>
              <Badge>{module.permissions.length}</Badge>
            </div>
            <Table>
              <thead>
                <tr>
                  <Th>Permission</Th>
                  <Th>Supported scopes</Th>
                  {creating ? <Th>Grant at</Th> : null}
                  {!creating
                    ? roles
                        .filter((r) => !r.is_team_scoped || r.key !== "super_admin")
                        .map((role) => (
                          <Th key={role.key} className="text-center">
                            {role.key.replace(/_/g, " ")}
                          </Th>
                        ))
                    : null}
                </tr>
              </thead>
              <tbody>
                {module.permissions.map((permission) => (
                  <tr key={permission.key}>
                    <Td>
                      <div className="font-mono text-[11.5px] text-ink">{permission.key}</div>
                      <div className="text-[12px] text-ink-3">{permission.description}</div>
                    </Td>
                    <Td>
                      <div className="flex gap-1">
                        {permission.scopes.map((scope) => (
                          <Badge key={scope}>{scope}</Badge>
                        ))}
                      </div>
                    </Td>

                    {creating ? (
                      <Td>
                        <div className="flex gap-1">
                          <button
                            type="button"
                            onClick={() => {
                              const next = { ...grants };
                              delete next[permission.key];
                              setGrants(next);
                            }}
                            className={cn(
                              "rounded-[--radius-xs] border px-1.5 py-0.5 font-mono text-[10px]",
                              grants[permission.key] === undefined
                                ? "border-ink-3 bg-surface-2 text-ink-2"
                                : "border-line text-ink-3 hover:border-ink-3",
                            )}
                          >
                            none
                          </button>
                          {permission.scopes.map((scope) => (
                            <button
                              key={scope}
                              type="button"
                              onClick={() => setGrants({ ...grants, [permission.key]: scope })}
                              className={cn(
                                "rounded-[--radius-xs] border px-1.5 py-0.5 font-mono text-[10px]",
                                grants[permission.key] === scope
                                  ? "border-accent bg-accent-soft text-accent-ink"
                                  : "border-line text-ink-3 hover:border-accent",
                              )}
                            >
                              {SCOPE_LABEL[scope]}
                            </button>
                          ))}
                        </div>
                      </Td>
                    ) : (
                      roles
                        .filter((r) => !r.is_team_scoped || r.key !== "super_admin")
                        .map((role) => {
                          const scope = role.grants[permission.key];
                          return (
                            <Td key={role.key} className="text-center">
                              {scope ? (
                                <span className="font-mono text-[10px] uppercase text-accent">
                                  {scope}
                                </span>
                              ) : (
                                <span className="text-ink-3">·</span>
                              )}
                            </Td>
                          );
                        })
                    )}
                  </tr>
                ))}
              </tbody>
            </Table>
          </section>
        ))}
      </div>
    </Card>
  );
}
