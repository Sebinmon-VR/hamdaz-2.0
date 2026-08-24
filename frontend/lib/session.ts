/**
 * Server-side session helpers.
 *
 * Reads `/meta/me` with the incoming cookies forwarded, so a server component knows exactly
 * what the caller may do. Permission checks here decide what to *render*; the backend
 * re-checks everything on every call. The UI is a convenience, never a control.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { ApiError, api } from "./api";
import type { Me, Scope } from "./types";

const SCOPE_RANK: Record<Scope, number> = { own: 0, team: 1, all: 2 };

export async function forwardedHeaders(): Promise<Record<string, string>> {
  const store = await cookies();
  const cookie = store
    .getAll()
    .map((c) => `${c.name}=${c.value}`)
    .join("; ");
  return cookie ? { cookie } : {};
}

/** The signed-in user, or null. Never throws for an ordinary signed-out visitor. */
export async function getMe(): Promise<Me | null> {
  try {
    return await api.get<Me>("/meta/me", { headers: await forwardedHeaders() });
  } catch (error) {
    if (error instanceof ApiError && error.isUnauthenticated) return null;
    throw error;
  }
}

/** Require a session, or bounce to login. */
export async function requireMe(): Promise<Me> {
  const me = await getMe();
  if (!me) redirect("/login");
  return me;
}

/**
 * Does this user hold a permission?
 *
 * Mirrors `Principal.has` on the backend, including the rule that matters most: a team grant
 * never satisfies an org-wide check.
 */
export function can(
  me: Me | null,
  permission: string,
  required: Scope = "team",
  teamId?: string,
): boolean {
  if (!me) return false;
  if (me.is_super_admin) return true;

  let best: Scope | undefined = me.org_permissions[permission];

  if (teamId) {
    const team = me.teams.find((t) => t.team_id === teamId);
    const teamScope = team?.permissions[permission];
    if (teamScope && (!best || SCOPE_RANK[teamScope] > SCOPE_RANK[best])) {
      best = teamScope;
    }
  }

  return best !== undefined && SCOPE_RANK[best] >= SCOPE_RANK[required];
}

/** Teams where the user holds a permission at `required` or wider. */
export function teamsWith(me: Me | null, permission: string, required: Scope = "team"): string[] {
  if (!me) return [];
  if (me.is_super_admin) return me.teams.map((t) => t.team_id);

  const orgScope = me.org_permissions[permission];
  if (orgScope && SCOPE_RANK[orgScope] >= SCOPE_RANK.all) {
    return me.teams.map((t) => t.team_id);
  }

  return me.teams
    .filter((t) => {
      const scope = t.permissions[permission];
      return scope !== undefined && SCOPE_RANK[scope] >= SCOPE_RANK[required];
    })
    .map((t) => t.team_id);
}

export function canAny(me: Me | null, permissions: string[], required: Scope = "team"): boolean {
  return permissions.some((p) => can(me, p, required));
}

/** True when the account exists but no admin has placed them in a team yet. */
export function isAwaitingAccess(me: Me | null): boolean {
  return me !== null && !me.is_super_admin && me.teams.length === 0;
}
