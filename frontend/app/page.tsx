import { redirect } from "next/navigation";

import { getMe } from "@/lib/session";

export default async function RootPage(): Promise<never> {
  redirect((await getMe()) ? "/dashboard" : "/login");
}
