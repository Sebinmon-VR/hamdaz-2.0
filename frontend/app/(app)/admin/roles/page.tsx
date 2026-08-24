import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Badge, Card, CardHeader, PageHeader } from "@/components/ui";
import { api } from "@/lib/api";
import { forwardedHeaders, requireMe } from "@/lib/session";
import type { PermissionModule, SystemRole } from "@/lib/types";

import { RoleMatrix } from "./role-matrix";

export const metadata: Metadata = { title: "Roles" };

export default async function RolesPage(): Promise<ReactNode> {
  await requireMe();
  const headers = await forwardedHeaders();

  const [modules, roles] = await Promise.all([
    api.get<PermissionModule[]>("/meta/permissions", { headers }),
    api.get<SystemRole[]>("/meta/roles", { headers }),
  ]);

  const total = modules.reduce((sum, m) => sum + m.permissions.length, 0);

  return (
    <>
      <PageHeader
        eyebrow="Admin"
        title="Roles & permissions"
        description="Built-in roles ship with the product. Custom roles are composed from the same registry the backend enforces, so the two cannot drift."
      />

      <div className="mb-6 grid gap-4 md:grid-cols-2">
        {roles.map((role) => (
          <Card key={role.key}>
            <CardHeader
              title={role.name}
              description={role.description}
              action={
                <Badge tone={role.is_team_scoped ? "accent" : "legacy"}>
                  {role.is_team_scoped ? "team" : "organisation"}
                </Badge>
              }
            />
            <div className="px-5 py-3">
              <div className="eyebrow mb-1.5">
                {Object.keys(role.grants).length} of {total} permissions
              </div>
              <div className="flex flex-wrap gap-1">
                {[...new Set(Object.keys(role.grants).map((k) => k.split(".")[0]))]
                  .sort()
                  .map((module) => (
                    <Badge key={module}>{module}</Badge>
                  ))}
              </div>
            </div>
          </Card>
        ))}
      </div>

      <RoleMatrix modules={modules} roles={roles} />
    </>
  );
}
