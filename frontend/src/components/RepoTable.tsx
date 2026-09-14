import { useMemo, useState } from "react";
import { motion } from "motion/react";
import { Search, ExternalLink, ChevronRight } from "lucide-react";
import type { RepoState, RepoStatus } from "@/lib/types";
import { ALL_STATUSES, statusMeta } from "@/lib/status";
import {
  absoluteTime,
  cn,
  isHttpsUrl,
  relativeTime,
  repoOwner,
  repoShortName,
} from "@/lib/utils";
import { StatusBadge } from "./ui/StatusBadge";
import { Card, CardHeader } from "./ui/Card";

type Filter = "all" | RepoStatus;

export function RepoTable({
  repos,
  onSelect,
  selected,
}: {
  repos: RepoState[];
  onSelect: (repo: RepoState) => void;
  selected?: string;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");

  const presentStatuses = useMemo(() => {
    const set = new Set(repos.map((r) => r.status));
    return ALL_STATUSES.filter((s) => set.has(s));
  }, [repos]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return repos.filter((r) => {
      if (filter !== "all" && r.status !== filter) return false;
      if (q && !r.repo_full_name.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [repos, query, filter]);

  return (
    <Card>
      <CardHeader
        title="Repositories"
        subtitle={`${filtered.length} of ${repos.length} shown`}
        right={
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter by name…"
              className="w-44 rounded-lg border border-border bg-bg-elevated py-1.5 pl-8 pr-2 text-xs text-text outline-none transition-colors placeholder:text-faint focus:border-accent"
            />
          </div>
        }
      />

      {/* Status filter chips */}
      <div className="flex flex-wrap gap-1.5 border-b border-border px-5 py-3">
        <FilterChip
          active={filter === "all"}
          onClick={() => setFilter("all")}
          label="All"
          count={repos.length}
          color="#8a93a6"
        />
        {presentStatuses.map((s) => (
          <FilterChip
            key={s}
            active={filter === s}
            onClick={() => setFilter(s)}
            label={statusMeta(s).label}
            count={repos.filter((r) => r.status === s).length}
            color={statusMeta(s).color}
          />
        ))}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="text-left text-[11px] uppercase tracking-wide text-faint">
              <th className="px-5 py-2.5 font-medium">Repository</th>
              <th className="px-3 py-2.5 font-medium">Status</th>
              <th className="px-3 py-2.5 font-medium">Issue</th>
              <th className="px-3 py-2.5 font-medium">PR</th>
              <th className="px-3 py-2.5 font-medium">Reply</th>
              <th className="px-3 py-2.5 font-medium">Follow-ups</th>
              <th className="px-3 py-2.5 font-medium">Last action</th>
              <th className="px-3 py-2.5" />
            </tr>
          </thead>
          <tbody>
            {filtered.map((r, i) => (
              <motion.tr
                key={r.repo_full_name}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ delay: Math.min(i * 0.02, 0.3) }}
                onClick={() => onSelect(r)}
                className={cn(
                  "cursor-pointer border-t border-border/70 transition-colors hover:bg-panel-hover",
                  selected === r.repo_full_name && "bg-panel-hover",
                )}
              >
                <td className="px-5 py-3">
                  <div className="font-medium leading-tight text-text">
                    {repoShortName(r.repo_full_name)}
                  </div>
                  <div className="text-[11px] text-faint">
                    {repoOwner(r.repo_full_name)}
                  </div>
                </td>
                <td className="px-3 py-3">
                  <StatusBadge status={r.status} size="sm" />
                </td>
                <td className="px-3 py-3 text-muted">
                  {r.issue_number ? `#${r.issue_number}` : "—"}
                </td>
                <td className="px-3 py-3">
                  {isHttpsUrl(r.pr_url) ? (
                    <a
                      href={r.pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      onClick={(e) => e.stopPropagation()}
                      className="inline-flex items-center gap-1 text-accent hover:underline"
                    >
                      #{r.pr_number ?? "PR"}
                      <ExternalLink className="h-3 w-3" />
                    </a>
                  ) : (
                    <span className="text-faint">—</span>
                  )}
                </td>
                <td className="px-3 py-3">
                  {r.maintainer_responded ? (
                    <span className="text-good">yes</span>
                  ) : (
                    <span className="text-faint">no</span>
                  )}
                </td>
                <td className="px-3 py-3 text-muted">
                  {r.follow_up_count ?? 0}
                </td>
                <td
                  className="px-3 py-3 text-muted"
                  title={absoluteTime(r.last_action_at)}
                >
                  {relativeTime(r.last_action_at)}
                </td>
                <td className="px-3 py-3 text-right">
                  <ChevronRight className="ml-auto h-4 w-4 text-faint" />
                </td>
              </motion.tr>
            ))}
          </tbody>
        </table>

        {filtered.length === 0 && (
          <div className="px-5 py-12 text-center text-sm text-faint">
            No repositories match this filter.
          </div>
        )}
      </div>
    </Card>
  );
}

function FilterChip({
  active,
  onClick,
  label,
  count,
  color,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
  color: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] font-medium transition-colors",
        active
          ? "border-border-strong bg-panel-hover text-text"
          : "border-border bg-panel text-muted hover:text-text",
      )}
    >
      <span
        className="inline-block h-2 w-2 rounded-full"
        style={{ background: color }}
      />
      {label}
      <span className="text-faint">{count}</span>
    </button>
  );
}
