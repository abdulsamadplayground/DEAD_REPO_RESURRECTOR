import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function Card({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={cn("panel", className)}>{children}</div>;
}

export function CardHeader({
  title,
  subtitle,
  right,
  className,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  right?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex items-start justify-between gap-4 border-b border-border px-5 py-4",
        className,
      )}
    >
      <div>
        <h2 className="text-sm font-semibold tracking-tight text-text">
          {title}
        </h2>
        {subtitle && (
          <p className="mt-0.5 text-xs text-muted">{subtitle}</p>
        )}
      </div>
      {right}
    </div>
  );
}
