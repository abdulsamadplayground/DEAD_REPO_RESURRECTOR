import { AlertTriangle, Info } from "lucide-react";

interface Props {
  sources: { state: boolean; live: boolean };
  repoCount: number;
}

/**
 * Honest status banner. This dashboard shows live data only — never mock data.
 * The banner explains exactly which real endpoints answered so the view is
 * never silently misleading.
 */
export function ConnectionBanner({ sources, repoCount }: Props) {
  // Everything reachable and we have repos: no banner needed.
  if (sources.live && repoCount > 0) return null;

  if (!sources.state && !sources.live) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-bad/25 bg-bad/10 px-4 py-2.5 text-xs text-bad">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        <span>
          Can't reach the live endpoints (<code className="font-mono">/state</code>{" "}
          and <code className="font-mono">/live</code>). Nothing is shown because
          this dashboard never displays sample data. Check the API URL or your
          connection.
        </span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 rounded-xl border border-warn/25 bg-warn/10 px-4 py-2.5 text-xs text-warn">
      <Info className="h-4 w-4 shrink-0" />
      <span>
        {sources.live
          ? "Live GitHub discovery returned no repositories right now."
          : "Showing agent-pipeline state only — live GitHub discovery is unavailable."}
      </span>
    </div>
  );
}
