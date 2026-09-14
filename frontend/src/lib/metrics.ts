import type { RepoState, RepoStatus } from "./types";
import { ALL_STATUSES, statusMeta } from "./status";

export interface Metrics {
  total: number;
  active: number; // discovered + in_progress + pr_opened
  prsOpen: number; // pr_opened
  merged: number; // success
  responded: number;
  successRate: number; // merged / (merged + rejected), 0..1
  statusCounts: Record<RepoStatus, number>;
}

const ACTIVE: RepoStatus[] = ["discovered", "in_progress", "pr_opened"];

export function computeMetrics(repos: RepoState[]): Metrics {
  const statusCounts = Object.fromEntries(
    ALL_STATUSES.map((s) => [s, 0]),
  ) as Record<RepoStatus, number>;

  let responded = 0;
  for (const r of repos) {
    if (statusCounts[r.status as RepoStatus] !== undefined) {
      statusCounts[r.status as RepoStatus] += 1;
    }
    if (r.maintainer_responded) responded += 1;
  }

  const merged = statusCounts.success;
  const rejected = statusCounts.rejected;
  const decided = merged + rejected;

  return {
    total: repos.length,
    active: ACTIVE.reduce((n, s) => n + statusCounts[s], 0),
    prsOpen: statusCounts.pr_opened,
    merged,
    responded,
    successRate: decided === 0 ? 0 : merged / decided,
    statusCounts,
  };
}

export interface StatusDatum {
  status: RepoStatus;
  label: string;
  value: number;
  color: string;
}

/** Chart-ready status distribution, dropping empty buckets. */
export function statusDistribution(
  statusCounts: Record<RepoStatus, number>,
): StatusDatum[] {
  return ALL_STATUSES.map((status) => ({
    status,
    label: statusMeta(status).label,
    value: statusCounts[status],
    color: statusMeta(status).color,
  })).filter((d) => d.value > 0);
}
