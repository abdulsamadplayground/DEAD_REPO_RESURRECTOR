import { motion } from "motion/react";
import {
  GitPullRequest,
  Radar,
  CheckCircle2,
  MessageSquareReply,
} from "lucide-react";
import type { ReactNode } from "react";
import type { Metrics } from "@/lib/metrics";
import { Card } from "./ui/Card";

interface StatDef {
  key: string;
  label: string;
  value: string | number;
  hint: string;
  icon: ReactNode;
  accent: string;
}

export function StatCards({ metrics }: { metrics: Metrics }) {
  const stats: StatDef[] = [
    {
      key: "active",
      label: "In the pipeline",
      value: metrics.active,
      hint: `${metrics.total} tracked total`,
      icon: <Radar className="h-4 w-4" />,
      accent: "#6ea8fe",
    },
    {
      key: "prs",
      label: "PRs awaiting review",
      value: metrics.prsOpen,
      hint: `${metrics.responded} with a maintainer reply`,
      icon: <GitPullRequest className="h-4 w-4" />,
      accent: "#56d364",
    },
    {
      key: "merged",
      label: "Fixes merged",
      value: metrics.merged,
      hint: "Maintainer accepted the PR",
      icon: <CheckCircle2 className="h-4 w-4" />,
      accent: "#3fb950",
    },
    {
      key: "rate",
      label: "Acceptance rate",
      value: `${Math.round(metrics.successRate * 100)}%`,
      hint: "Merged of all decided PRs",
      icon: <MessageSquareReply className="h-4 w-4" />,
      accent: "#d2a8ff",
    },
  ];

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {stats.map((s, i) => (
        <motion.div
          key={s.key}
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: i * 0.06, duration: 0.35, ease: "easeOut" }}
        >
          <Card className="relative overflow-hidden p-5">
            <div
              className="pointer-events-none absolute -right-6 -top-6 h-24 w-24 rounded-full opacity-20 blur-2xl"
              style={{ background: s.accent }}
            />
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-muted">{s.label}</span>
              <span
                className="grid h-7 w-7 place-items-center rounded-lg"
                style={{ background: `${s.accent}1f`, color: s.accent }}
              >
                {s.icon}
              </span>
            </div>
            <div className="mt-3 text-3xl font-semibold tracking-tight">
              {s.value}
            </div>
            <div className="mt-1 text-xs text-faint">{s.hint}</div>
          </Card>
        </motion.div>
      ))}
    </div>
  );
}
