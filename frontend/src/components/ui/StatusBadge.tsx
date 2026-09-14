import { statusMeta } from "@/lib/status";
import { cn } from "@/lib/utils";

interface Props {
  status: string;
  className?: string;
  size?: "sm" | "md";
}

/** Pill showing a repo's lifecycle status with the shared status palette. */
export function StatusBadge({ status, className, size = "md" }: Props) {
  const meta = statusMeta(status);
  return (
    <span
      title={meta.description}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full font-semibold whitespace-nowrap",
        size === "sm" ? "px-2 py-0.5 text-[11px]" : "px-2.5 py-1 text-xs",
        className,
      )}
      style={{
        color: meta.color,
        background: meta.tint,
        border: `1px solid ${meta.color}33`,
      }}
    >
      <span
        className="inline-block rounded-full"
        style={{
          width: size === "sm" ? 5 : 6,
          height: size === "sm" ? 5 : 6,
          background: meta.color,
        }}
      />
      {meta.label}
    </span>
  );
}
