"use client";

import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { Button, Callout, Card, CardBody, CardHeader, Checkbox, Field, Input } from "@/components/ui";
import { ApiError, api } from "@/lib/api";

export function CreateTeam({ modules }: { modules: string[] }): ReactNode {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.post("/admin/teams", {
        name: name.trim(),
        description: description.trim() || null,
        enabled_modules: enabled,
      });
      setOpen(false);
      setName("");
      setDescription("");
      setEnabled([]);
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the team.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <Button variant="primary" onClick={() => setOpen(true)}>
        New team
      </Button>
    );
  }

  return (
    <Card className="w-full max-w-md">
      <CardHeader
        title="New team"
        action={
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        }
      />
      <CardBody className="flex flex-col gap-4">
        <Field label="Name" htmlFor="team-name" hint="The slug is derived automatically.">
          <Input
            id="team-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Business Development"
            autoFocus
          />
        </Field>

        <Field label="Description" htmlFor="team-desc">
          <Input
            id="team-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this team does"
          />
        </Field>

        <Field
          label="Modules"
          hint="A team only sees what it has enabled. This is what lets one platform serve teams doing unlike work."
        >
          <div className="flex flex-wrap gap-x-4 gap-y-2">
            {modules.map((module) => (
              <Checkbox
                key={module}
                label={module}
                checked={enabled.includes(module)}
                onChange={(e) =>
                  setEnabled(
                    e.target.checked
                      ? [...enabled, module]
                      : enabled.filter((m) => m !== module),
                  )
                }
              />
            ))}
          </div>
        </Field>

        {error ? <Callout tone="danger">{error}</Callout> : null}

        <div>
          <Button variant="primary" onClick={submit} disabled={busy || name.trim().length < 2}>
            {busy ? "Creating…" : "Create team"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
