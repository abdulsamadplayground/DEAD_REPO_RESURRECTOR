import type { DashboardPayload, RepoState } from "./types";

// --- API URL resolution (deploy-time configurable, no rebuild) -------------
// First match wins, mirroring the legacy src/dashboard/index.html so existing
// deploy tooling keeps working:
//   1. ?api=<url> query parameter          -> the /state endpoint
//   2. window.DASHBOARD_API_URL global      -> the /state endpoint
//   3. <meta name="dashboard-api" content>  -> the /state endpoint
//   4. relative default "/state"
//
// The live-discovery endpoint (/live) is derived from the same origin as the
// resolved /state URL (they share one API Gateway stage), or overridden with
// ?live=<url> / <meta name="dashboard-live">.

declare global {
  interface Window {
    DASHBOARD_API_URL?: string;
    DASHBOARD_LIVE_URL?: string;
  }
}

function metaContent(name: string): string | null {
  const el = document.querySelector(`meta[name="${name}"]`);
  return el?.getAttribute("content") || null;
}

export function resolveStateUrl(): string {
  const params = new URLSearchParams(window.location.search);
  return (
    params.get("api") ||
    window.DASHBOARD_API_URL ||
    metaContent("dashboard-api") ||
    "/state"
  );
}

/** Derive the /live URL by swapping the trailing /state path segment. */
export function resolveLiveUrl(): string {
  const params = new URLSearchParams(window.location.search);
  const explicit =
    params.get("live") ||
    window.DASHBOARD_LIVE_URL ||
    metaContent("dashboard-live");
  if (explicit) return explicit;

  const stateUrl = resolveStateUrl();
  // Replace a trailing "/state" with "/live"; otherwise fall back to "/live".
  if (/\/state\/?$/.test(stateUrl)) {
    return stateUrl.replace(/\/state\/?$/, "/live");
  }
  return "/live";
}

export interface FetchResult {
  payload: DashboardPayload;
  /** Which endpoints actually returned data, for honest UI labeling. */
  sources: { state: boolean; live: boolean };
}

async function fetchJson(
  url: string,
  signal?: AbortSignal,
): Promise<DashboardPayload | null> {
  try {
    const res = await fetch(url, {
      headers: { Accept: "application/json" },
      signal,
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = (await res.json()) as DashboardPayload;
    if (!data || !Array.isArray(data.repos)) return null;
    return data;
  } catch (err) {
    if (signal?.aborted) throw err;
    return null;
  }
}

/**
 * Fetch dashboard data from real endpoints only — no mock data, ever.
 *
 * Merges two live sources:
 *   - /state : what the agent pipeline has actually done (may be empty).
 *   - /live  : real GitHub distress-pattern candidates with open issues.
 *
 * A repo present in /state takes precedence over the same repo in /live (state
 * reflects real agent work; live is discovery). If both endpoints fail, the
 * result is an empty payload with both sources false — the UI then shows an
 * honest error/empty state rather than anything fabricated.
 */
export async function fetchDashboard(signal?: AbortSignal): Promise<FetchResult> {
  const [state, live] = await Promise.all([
    fetchJson(resolveStateUrl(), signal),
    fetchJson(resolveLiveUrl(), signal),
  ]);

  const byRepo = new Map<string, RepoState>();
  // Live first, then overlay state so real agent state wins on collisions.
  for (const r of live?.repos ?? []) byRepo.set(r.repo_full_name, r);
  for (const r of state?.repos ?? []) {
    const existing = byRepo.get(r.repo_full_name);
    // Preserve live enrichment (stars/issue title) if state lacks it.
    byRepo.set(r.repo_full_name, existing?.live ? { ...r, live: existing.live } : r);
  }

  const repos = Array.from(byRepo.values());
  repos.sort((a, b) =>
    (b.last_action_at ?? "").localeCompare(a.last_action_at ?? ""),
  );

  return {
    payload: {
      generated_at: state?.generated_at ?? live?.generated_at ?? new Date().toISOString(),
      count: repos.length,
      repos,
    },
    sources: { state: state !== null, live: live !== null },
  };
}
