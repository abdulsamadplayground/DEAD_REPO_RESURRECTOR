import type { RepoStatus, PipelineStage } from "./types";

// Presentation metadata for every valid status. Colors are expressed as CSS
// custom-property-friendly hex so both the badge and the charts share one
// palette. Keep labels human-friendly but never call a repo "dead"/"abandoned"
// per github-conventions — surface neutral, factual phrasing.

export interface StatusMeta {
  label: string;
  /** Short helper shown in tooltips / detail. */
  description: string;
  /** Foreground/text accent. */
  color: string;
  /** Translucent background tint. */
  tint: string;
  /** Which lifecycle group this belongs to for grouping/sorting. */
  group: "pipeline" | "outcome" | "dormant";
}

export const STATUS_META: Record<RepoStatus, StatusMeta> = {
  discovered: {
    label: "Discovered",
    description: "Found by the scanner and queued for analysis.",
    color: "#7cc0ff",
    tint: "rgba(124, 192, 255, 0.14)",
    group: "pipeline",
  },
  in_progress: {
    label: "In progress",
    description: "An agent is actively analyzing or fixing this repository.",
    color: "#e3b341",
    tint: "rgba(227, 179, 65, 0.14)",
    group: "pipeline",
  },
  pr_opened: {
    label: "PR opened",
    description: "A pull request addressing the issue is open for review.",
    color: "#56d364",
    tint: "rgba(86, 211, 100, 0.14)",
    group: "pipeline",
  },
  success: {
    label: "Merged",
    description: "The maintainer merged the pull request.",
    color: "#3fb950",
    tint: "rgba(63, 185, 80, 0.16)",
    group: "outcome",
  },
  rejected: {
    label: "Closed",
    description: "The maintainer closed the pull request without merging.",
    color: "#ff7b72",
    tint: "rgba(255, 123, 114, 0.14)",
    group: "outcome",
  },
  fix_failed: {
    label: "Fix failed",
    description: "The engineer could not produce a valid, tested fix.",
    color: "#ff7b72",
    tint: "rgba(255, 123, 114, 0.14)",
    group: "outcome",
  },
  skipped_complex: {
    label: "Skipped (complex)",
    description: "The analyst judged the issue too complex or low-confidence.",
    color: "#d2a8ff",
    tint: "rgba(210, 168, 255, 0.14)",
    group: "outcome",
  },
  dormant: {
    label: "Dormant",
    description: "No maintainer reply after the 7-day and 14-day follow-ups.",
    color: "#9aa3b2",
    tint: "rgba(154, 163, 178, 0.12)",
    group: "dormant",
  },
  ignored: {
    label: "Ignored",
    description: "Set aside; re-eligible after 30 days.",
    color: "#6e7681",
    tint: "rgba(110, 118, 129, 0.12)",
    group: "dormant",
  },
};

export const ALL_STATUSES: RepoStatus[] = [
  "discovered",
  "in_progress",
  "pr_opened",
  "success",
  "rejected",
  "fix_failed",
  "skipped_complex",
  "dormant",
  "ignored",
];

export function statusMeta(status: string): StatusMeta {
  return (
    STATUS_META[status as RepoStatus] ?? {
      label: status || "unknown",
      description: "Unrecognized status.",
      color: "#9aa3b2",
      tint: "rgba(154, 163, 178, 0.12)",
      group: "outcome",
    }
  );
}

// Map a repo status onto its position in the Analyst -> Engineer ->
// Communicator pipeline, used to drive the live activity visualization.
export function stageForStatus(status: string): PipelineStage {
  switch (status) {
    case "discovered":
      return "queued";
    case "in_progress":
      return "engineering";
    case "pr_opened":
    case "success":
    case "rejected":
    case "dormant":
      return "done";
    case "skipped_complex":
      return "analyzing";
    case "fix_failed":
      return "engineering";
    default:
      return "queued";
  }
}
