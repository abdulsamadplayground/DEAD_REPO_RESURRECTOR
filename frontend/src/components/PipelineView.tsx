import { motion } from "motion/react";
import { Search, Wrench, Send, ArrowRight, Bot } from "lucide-react";
import type { ReactNode } from "react";
import type { RepoState } from "@/lib/types";
import { stageForStatus } from "@/lib/status";
import { repoShortName } from "@/lib/utils";
import { Card, CardHeader } from "./ui/Card";

interface StageDef {
  key: string;
  agent: string;
  role: string;
  icon: ReactNode;
  color: string;
  match: (r: RepoState) => boolean;
}

// The Orchestrator delegates to three sub-agents in order. We bucket repos by
// the stage their current status implies (see stageForStatus).
const STAGES: StageDef[] = [
  {
    key: "analyst",
    agent: "Analyst",
    role: "Reads issues, README & history; scores complexity",
    icon: <Search className="h-4 w-4" />,
    color: "#7cc0ff",
    match: (r) =>
      stageForStatus(r.status) === "queued" ||
      r.status === "discovered" ||
      r.status === "skipped_complex",
  },
  {
    key: "engineer",
    agent: "Engineer",
    role: "Creates a branch, writes the fix, runs tests, pushes",
    icon: <Wrench className="h-4 w-4" />,
    color: "#e3b341",
    match: (r) => r.status === "in_progress" || r.status === "fix_failed",
  },
  {
    key: "communicator",
    agent: "Communicator",
    role: "Opens the PR, comments on the issue, follows up",
    icon: <Send className="h-4 w-4" />,
    color: "#56d364",
    match: (r) =>
      r.status === "pr_opened" ||
      r.status === "success" ||
      r.status === "rejected" ||
      r.status === "dormant",
  },
];

export function PipelineView({
  repos,
  onSelect,
}: {
  repos: RepoState[];
  onSelect: (repo: RepoState) => void;
}) {
  return (
    <Card>
      <CardHeader
        title="Agent pipeline"
        subtitle="Orchestrator delegates each repository through three specialist agents"
        right={
          <span className="hidden items-center gap-1.5 rounded-full border border-border bg-panel px-2.5 py-1 text-[11px] text-muted sm:inline-flex">
            <Bot className="h-3 w-3" /> Strands agent-as-tool
          </span>
        }
      />
      <div className="grid grid-cols-1 gap-3 p-5 lg:grid-cols-[1fr_auto_1fr_auto_1fr]">
        {STAGES.map((stage, i) => {
          const inStage = repos.filter(stage.match);
          const active = inStage.filter(
            (r) => r.status === "in_progress",
          ).length;
          return (
            <div key={stage.key} className="contents">
              <motion.div
                initial={{ opacity: 0, scale: 0.97 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{ delay: i * 0.08 }}
                className="rounded-xl border border-border bg-bg-elevated/60 p-4"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span
                      className="grid h-8 w-8 place-items-center rounded-lg"
                      style={{
                        background: `${stage.color}1f`,
                        color: stage.color,
                      }}
                    >
                      {stage.icon}
                    </span>
                    <div>
                      <div className="text-sm font-semibold">{stage.agent}</div>
                      <div className="text-[11px] text-faint">
                        {inStage.length} in queue
                      </div>
                    </div>
                  </div>
                  {active > 0 && stage.key === "engineer" && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-warn/10 px-2 py-0.5 text-[10px] font-semibold text-warn">
                      <span className="h-1.5 w-1.5 rounded-full bg-warn live-dot" />
                      working
                    </span>
                  )}
                </div>
                <p className="mt-2 text-[11px] leading-relaxed text-muted">
                  {stage.role}
                </p>

                <div className="mt-3 flex flex-col gap-1.5">
                  {inStage.slice(0, 4).map((r) => (
                    <button
                      key={r.repo_full_name}
                      onClick={() => onSelect(r)}
                      className="group flex items-center justify-between rounded-lg border border-transparent bg-panel px-2.5 py-1.5 text-left text-xs transition-colors hover:border-border-strong hover:bg-panel-hover"
                    >
                      <span className="truncate font-medium text-text">
                        {repoShortName(r.repo_full_name)}
                      </span>
                      {r.status === "in_progress" ? (
                        <span className="ml-2 h-1.5 w-1.5 shrink-0 rounded-full bg-warn live-dot" />
                      ) : (
                        <ArrowRight className="ml-2 h-3 w-3 shrink-0 text-faint opacity-0 transition-opacity group-hover:opacity-100" />
                      )}
                    </button>
                  ))}
                  {inStage.length > 4 && (
                    <span className="px-1 text-[11px] text-faint">
                      +{inStage.length - 4} more
                    </span>
                  )}
                  {inStage.length === 0 && (
                    <span className="px-1 text-[11px] text-faint">Idle</span>
                  )}
                </div>
              </motion.div>

              {i < STAGES.length - 1 && (
                <div className="hidden items-center justify-center lg:flex">
                  <ArrowRight className="h-5 w-5 text-border-strong" />
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
