// Domain types mirroring the /state JSON contract emitted by
// src/lambdas/dashboard_lambda.py (via RepoState.to_item()).

export type RepoStatus =
  | "discovered"
  | "in_progress"
  | "skipped_complex"
  | "fix_failed"
  | "pr_opened"
  | "success"
  | "rejected"
  | "dormant"
  | "ignored";

/** Real-GitHub enrichment attached to rows from GET /live. */
export interface LiveEnrichment {
  stars?: number | null;
  open_issues?: number | null;
  language?: string | null;
  html_url?: string | null;
  issue_title?: string | null;
  issue_url?: string | null;
}

/** One row of the ResurrectorState table, as returned by GET /state. */
export interface RepoState {
  repo_full_name: string;
  status: RepoStatus;
  issue_number?: number | null;
  pr_number?: number | null;
  pr_url?: string | null;
  opened_at?: string | null;
  last_action_at?: string | null;
  maintainer_responded?: boolean;
  follow_up_count?: number;
  complexity?: string | null;
  notes?: string | null;
  /** Present on rows sourced from GET /live (real GitHub candidates). */
  live?: LiveEnrichment | null;
}

/** Top-level payload from GET /state. */
export interface DashboardPayload {
  generated_at: string;
  count: number;
  repos: RepoState[];
}

/** The three sub-agents the Orchestrator delegates to, in pipeline order. */
export type AgentName = "analyst" | "engineer" | "communicator";

export type PipelineStage =
  | "queued"
  | "analyzing"
  | "engineering"
  | "communicating"
  | "done";
