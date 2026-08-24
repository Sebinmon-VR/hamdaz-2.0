"use client";

import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";

import { Button, Callout, Card, CardBody, CardHeader, Field, Input, Select } from "@/components/ui";
import { ApiError, api } from "@/lib/api";
import type { LabelKind } from "@/lib/types";

export function CreateLabel(): ReactNode {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [kind, setKind] = useState<LabelKind>("category");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Keys are matched by the rules engine, so they follow a fixed shape.
  const derivedKey =
    key.trim() ||
    name
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "");

  async function submit(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      await api.post("/admin/labels", {
        key: derivedKey,
        name: name.trim(),
        kind,
        description: description.trim() || null,
      });
      setOpen(false);
      setName("");
      setKey("");
      setDescription("");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not create the label.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <Button variant="primary" onClick={() => setOpen(true)}>
        New label
      </Button>
    );
  }

  return (
    <Card className="w-full max-w-md">
      <CardHeader
        title="New label"
        action={
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        }
      />
      <CardBody className="flex flex-col gap-4">
        <Field label="Name">
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Part-time"
            autoFocus
          />
        </Field>

        <Field
          label="Key"
          hint={
            derivedKey
              ? `Rules will reference this as “${derivedKey}”.`
              : "Derived from the name if left blank."
          }
        >
          <Input
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={derivedKey || "part-time"}
            className="font-mono text-[12px]"
          />
        </Field>

        <Field
          label="Kind"
          hint={
            kind === "category"
              ? "Drives capacity multipliers and ratio targets."
              : kind === "skill"
                ? "Gates eligibility for work that requires it."
                : "Drives eligibility — e.g. excluded from rotation."
          }
        >
          <Select value={kind} onChange={(e) => setKind(e.target.value as LabelKind)}>
            <option value="category">Category</option>
            <option value="skill">Skill</option>
            <option value="status">Status</option>
          </Select>
        </Field>

        <Field label="Description">
          <Input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this label means"
          />
        </Field>

        {error ? <Callout tone="danger">{error}</Callout> : null}

        <div>
          <Button
            variant="primary"
            onClick={submit}
            disabled={busy || name.trim().length < 2 || derivedKey.length < 2}
          >
            {busy ? "Creating…" : "Create label"}
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}
