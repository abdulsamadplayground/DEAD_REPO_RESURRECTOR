import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { fetchDashboard } from "@/lib/api";
import { computeMetrics, statusDistribution } from "@/lib/metrics";
import type { RepoState } from "@/lib/types";
import { Header } from "@/components/Header";
import { StatCards } from "@/components/StatCards";
import { StatusChart } from "@/components/StatusChart";
import { PipelineView } from "@/components/PipelineView";
import { RepoTable } from "@/components/RepoTable";
import { RepoDrawer } from "@/components/RepoDrawer";
import { LoadingState } from "@/components/LoadingState";
import { ConnectionBanner } from "@/components/ConnectionBanner";

// Auto-refresh cadence. REQ-7.2/7.3: the legacy page refreshed every 30s; we
// keep that so a transition is reflected well inside the 60s freshness budget.
const REFRESH_MS = 30_000;

export default function App() {
  const [selected, setSelected] = useState<RepoState | null>(null);

  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ["dashboard"],
    queryFn: ({ signal }) => fetchDashboard(signal),
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: true,
  });

  const payload = data?.payload;
  const sources = data?.sources ?? { state: false, live: false };
  const connected = sources.state || sources.live;
  const repos = payload?.repos ?? [];
  const metrics = computeMetrics(repos);
  const distribution = statusDistribution(metrics.statusCounts);

  return (
    <div className="min-h-full">
      <Header
        connected={connected}
        generatedAt={payload?.generated_at}
        isFetching={isFetching}
        onRefresh={() => refetch()}
      />

      <main className="mx-auto max-w-[1400px] space-y-5 px-6 py-6">
        {isLoading ? (
          <LoadingState />
        ) : isError ? (
          <div className="flex items-center gap-3 rounded-xl border border-bad/25 bg-bad/10 px-4 py-3 text-sm text-bad">
            <AlertTriangle className="h-5 w-5" />
            Could not load dashboard state. Retrying automatically…
          </div>
        ) : (
          <>
            <ConnectionBanner sources={sources} repoCount={repos.length} />

            <StatCards metrics={metrics} />

            <div className="grid grid-cols-1 gap-5 lg:grid-cols-3">
              <div className="lg:col-span-2">
                <PipelineView repos={repos} onSelect={setSelected} />
              </div>
              <StatusChart data={distribution} />
            </div>

            <RepoTable
              repos={repos}
              onSelect={setSelected}
              selected={selected?.repo_full_name}
            />

            <footer className="pt-2 text-center text-[11px] text-faint">
              Every action this system takes results in a real change on GitHub —
              a PR, a comment, a branch. This console is a read-only view of that
              activity.
            </footer>
          </>
        )}
      </main>

      <RepoDrawer repo={selected} onClose={() => setSelected(null)} />
    </div>
  );
}
