import { AnimatePresence, motion } from "motion/react";
import {
  X,
  ExternalLink,
  Star,
  Code2,
  CircleDot,
  Search,
  Wrench,
  Send,
  Clock,
} from "lucide-react";
import type { ReactNode } from "react";
import type { RepoState } from "@/lib/types";
import { statusMeta } from "@/lib/status";
import {
  absoluteTime,
  isHttpsUrl,
  relativeTime,
  repoOwner,
  repoShortName,
} from "@/lib/utils";
import { StatusBadge } from "./ui/StatusBadge";

interface TimelineStep {
  agent: string;
  icon: ReactNode;
  color: string;
  title: string;
  detail: string;
  state: "done" | "active" | "pending" | "skipped";
}

// Derive a per-repo agent timeline from its status. This reflects the
// Analyst -> Engineer -> Communicator flow the Orchestrator runs.
function buildTimeline(repo: RepoState): TimelineStep[] {
  const s = repo.status;
  const reached = (statuses: string[]) => statuses.includes(s);

  const analyst: TimelineStep = {
    agent: "Analyst",
    icon: <Search className="h-3.5 w-3.5" />,
    color: "#7cc0ff",
    title: "Analyzed issue & scored complexity",
    detail:
      repo.complexity != null
        ? `Complexity assessed as ${repo.complexity}.`
        : "Reading issues, README and recent history.",
    state:
      s === "discovered"
        ? "active"
        : s === "skipped_complex"
          ? "done"
          : "done",
  };

  const engineer: TimelineStep = {
    agent: "Engineer",
    icon: <Wrench className="h-3.5 w-3.5" />,
    color: "#e3b341",
    title: "Wrote & tested the fix",
    detail:
      s === "in_progress"
        ? "Creating branch, editing code, running tests."
        : s === "fix_failed"
          ? "Fix attempt did not pass tests."
          : s === "skipped_complex"
            ? "Not started — issue skipped as too complex."
            : "Branch created, fix committed and pushed.",
    state:
      s === "discovered"
        ? "pending"
        : s === "skipped_complex"
          ? "skipped"
          : s === "in_progress"
            ? "active"
            : s === "fix_failed"
              ? "done"
              : "done",
  };

  const communicator: TimelineStep = {
    agent: "Communicator",
    icon: <Send className="h-3.5 w-3.5" />,
    color: "#56d364",
    title: "Opened PR & engaged maintainer",
    detail: reached(["pr_opened", "success", "rejected", "dormant"])
      ? repo.maintainer_responded
        ? "PR opened; maintainer has replied."
        : `PR opened; ${repo.follow_up_count ?? 0} follow-up(s) sent.`
      : "Waiting on a validated fix before opening a PR.",
    state: reached(["pr_opened", "success", "rejected", "dormant"])
      ? "done"
      : reached(["skipped_complex", "fix_failed"])
        ? "skipped"
        : "pending",
  };

  return [analyst, engineer, communicator];
}

export function RepoDrawer({
  repo,
  onClose,
}: {
  repo: RepoState | null;
  onClose: () => void;
}) {
  return (
    <AnimatePresence>
      {repo && (
        <>
          <motion.div
            className="fixed inset-0 z-30 bg-black/50 backdrop-blur-sm"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
          />
          <motion.aside
            className="fixed right-0 top-0 z-40 flex h-full w-full max-w-md flex-col border-l border-border bg-bg-elevated shadow-2xl"
            initial={{ x: "100%" }}
            animate={{ x: 0 }}
            exit={{ x: "100%" }}
            transition={{ type: "spring", damping: 30, stiffness: 300 }}
          >
            <DrawerBody repo={repo} onClose={onClose} />
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  );
}

function DrawerBody({
  repo,
  onClose,
}: {
  repo: RepoState;
  onClose: () => void;
}) {
  const meta = statusMeta(repo.status);
  const enrich = repo.live ?? {};
  const timeline = buildTimeline(repo);

  return (
    <>
      <div className="flex items-start justify-between gap-3 border-b border-border p-5">
        <div className="min-w-0">
          <div className="text-[11px] text-faint">
            {repoOwner(repo.repo_full_name)}
          </div>
          <h2 className="truncate text-lg font-semibold">
            {repoShortName(repo.repo_full_name)}
          </h2>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <StatusBadge status={repo.status} size="sm" />
            {enrich.language && (
              <span className="inline-flex items-center gap-1 text-[11px] text-muted">
                <Code2 className="h-3 w-3" />
                {enrich.language}
              </span>
            )}
            {enrich.stars != null && (
              <span className="inline-flex items-center gap-1 text-[11px] text-muted">
                <Star className="h-3 w-3" />
                {enrich.stars}
              </span>
            )}
            {enrich.open_issues != null && (
              <span className="inline-flex items-center gap-1 text-[11px] text-muted">
                <CircleDot className="h-3 w-3" />
                {enrich.open_issues} open
              </span>
            )}
          </div>
        </div>
        <button
          onClick={onClose}
          className="grid h-8 w-8 shrink-0 place-items-center rounded-lg border border-border text-muted transition-colors hover:bg-panel-hover hover:text-text"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="flex-1 space-y-6 overflow-y-auto p-5">
        <p className="text-xs leading-relaxed text-muted">{meta.description}</p>

        {/* Issue */}
        <section>
          <SectionTitle icon={<CircleDot className="h-3.5 w-3.5" />}>
            Issue
          </SectionTitle>
          <div className="rounded-xl border border-border bg-panel p-3">
            {repo.issue_number ? (
              <>
                <div className="text-sm font-medium leading-snug">
                  {enrich.issue_title ?? "Open issue"}
                </div>
                <a
                  href={
                    enrich.issue_url ??
                    `https://github.com/${repo.repo_full_name}/issues/${repo.issue_number}`
                  }
                  target="_blank"
                  rel="noopener noreferrer"
                  className="mt-1 inline-flex items-center gap-1 text-xs text-accent hover:underline"
                >
                  #{repo.issue_number} on GitHub
                  <ExternalLink className="h-3 w-3" />
                </a>
              </>
            ) : (
              <span className="text-xs text-faint">No issue linked yet.</span>
            )}
          </div>
        </section>

        {/* Agent activity timeline */}
        <section>
          <SectionTitle icon={<Clock className="h-3.5 w-3.5" />}>
            Agent activity
          </SectionTitle>
          <ol className="relative ml-1 space-y-4 border-l border-border pl-5">
            {timeline.map((step) => (
              <li key={step.agent} className="relative">
                <span
                  className="absolute -left-[27px] grid h-6 w-6 place-items-center rounded-full border"
                  style={{
                    background:
                      step.state === "pending" || step.state === "skipped"
                        ? "var(--color-panel)"
                        : `${step.color}1f`,
                    borderColor:
                      step.state === "pending" || step.state === "skipped"
                        ? "var(--color-border)"
                        : `${step.color}55`,
                    color:
                      step.state === "pending" || step.state === "skipped"
                        ? "var(--color-faint)"
                        : step.color,
                  }}
                >
                  {step.icon}
                </span>
                <div className="flex items-center gap-2">
                  <span className="text-xs font-semibold">{step.agent}</span>
                  <StepBadge state={step.state} />
                </div>
                <div className="mt-0.5 text-xs text-text">{step.title}</div>
                <div className="mt-0.5 text-[11px] leading-relaxed text-muted">
                  {step.detail}
                </div>
              </li>
            ))}
          </ol>
        </section>

        {/* PR + engagement */}
        <section>
          <SectionTitle icon={<Send className="h-3.5 w-3.5" />}>
            Pull request
          </SectionTitle>
          <div className="grid grid-cols-2 gap-3">
            <Field label="PR">
              {isHttpsUrl(repo.pr_url) ? (
                <a
                  href={repo.pr_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-accent hover:underline"
                >
                  #{repo.pr_number ?? "view"}
                  <ExternalLink className="h-3 w-3" />
                </a>
              ) : (
                "—"
              )}
            </Field>
            <Field label="Maintainer replied">
              {repo.maintainer_responded ? (
                <span className="text-good">yes</span>
              ) : (
                "no"
              )}
            </Field>
            <Field label="Follow-ups sent">{repo.follow_up_count ?? 0}</Field>
            <Field label="Opened">
              <span title={absoluteTime(repo.opened_at)}>
                {relativeTime(repo.opened_at)}
              </span>
            </Field>
          </div>
        </section>

        {repo.notes && (
          <section>
            <SectionTitle>Notes</SectionTitle>
            <div className="rounded-xl border border-border bg-panel p-3 text-xs leading-relaxed text-muted">
              {repo.notes}
            </div>
          </section>
        )}

        <div className="text-[11px] text-faint">
          Last action {relativeTime(repo.last_action_at)} ·{" "}
          {absoluteTime(repo.last_action_at)}
        </div>
      </div>
    </>
  );
}

function SectionTitle({
  children,
  icon,
}: {
  children: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <h3 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">
      {icon}
      {children}
    </h3>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-panel px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-faint">
        {label}
      </div>
      <div className="mt-0.5 text-sm text-text">{children}</div>
    </div>
  );
}

function StepBadge({ state }: { state: TimelineStep["state"] }) {
  const map = {
    done: { label: "done", cls: "bg-good/10 text-good" },
    active: { label: "working", cls: "bg-warn/10 text-warn" },
    pending: { label: "pending", cls: "bg-panel text-faint" },
    skipped: { label: "skipped", cls: "bg-panel text-faint" },
  } as const;
  const m = map[state];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide ${m.cls}`}
    >
      {state === "active" && (
        <span className="h-1 w-1 rounded-full bg-warn live-dot" />
      )}
      {m.label}
    </span>
  );
}
