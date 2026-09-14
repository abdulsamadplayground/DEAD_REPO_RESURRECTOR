import { Activity, RefreshCw, Github } from "lucide-react";
import { cn } from "@/lib/utils";
import { relativeTime } from "@/lib/utils";

interface Props {
  connected: boolean;
  generatedAt?: string;
  isFetching: boolean;
  onRefresh: () => void;
}

export function Header({ connected, generatedAt, isFetching, onRefresh }: Props) {
  return (
    <header className="sticky top-0 z-20 border-b border-border bg-bg/70 backdrop-blur-xl">
      <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-4 px-6 py-4">
        <div className="flex items-center gap-3">
          <div className="grid h-10 w-10 place-items-center rounded-xl border border-border-strong bg-panel">
            <Activity className="h-5 w-5 text-accent" />
          </div>
          <div>
            <h1 className="text-base font-semibold leading-tight tracking-tight">
              Dead Repo Resurrector
            </h1>
            <p className="text-xs text-muted">
              Autonomous multi-agent maintenance console
            </p>
          </div>
        </div>

        <div className="ml-auto flex items-center gap-3">
          <span
            className={cn(
              "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-xs font-medium",
              connected
                ? "border-good/30 bg-good/10 text-good"
                : "border-bad/30 bg-bad/10 text-bad",
            )}
            title={
              connected
                ? "Reading real, live GitHub data from the API"
                : "The live API is unreachable — no data is shown"
            }
          >
            <span
              className={cn(
                "inline-block h-2 w-2 rounded-full",
                connected ? "bg-good live-dot" : "bg-bad",
              )}
            />
            {connected ? "Live" : "Disconnected"}
          </span>

          <span className="hidden text-xs text-muted sm:inline">
            Updated {relativeTime(generatedAt)}
          </span>

          <button
            onClick={onRefresh}
            className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-panel px-3 py-1.5 text-xs font-medium text-text transition-colors hover:border-border-strong hover:bg-panel-hover"
          >
            <RefreshCw
              className={cn("h-3.5 w-3.5", isFetching && "animate-spin")}
            />
            Refresh
          </button>

          <a
            href="https://github.com/search?q=stars%3A10..500&type=repositories"
            target="_blank"
            rel="noopener noreferrer"
            className="hidden items-center gap-1.5 rounded-lg border border-border bg-panel px-3 py-1.5 text-xs font-medium text-muted transition-colors hover:text-text md:inline-flex"
          >
            <Github className="h-3.5 w-3.5" />
            GitHub
          </a>
        </div>
      </div>
    </header>
  );
}
