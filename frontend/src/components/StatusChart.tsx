import {
  PieChart,
  Pie,
  Cell,
  ResponsiveContainer,
  Tooltip,
} from "recharts";
import type { StatusDatum } from "@/lib/metrics";
import { Card, CardHeader } from "./ui/Card";

function ChartTooltip({ active, payload }: any) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload as StatusDatum;
  return (
    <div className="rounded-lg border border-border-strong bg-bg-elevated px-3 py-2 text-xs shadow-xl">
      <div className="font-semibold" style={{ color: d.color }}>
        {d.label}
      </div>
      <div className="text-muted">
        {d.value} {d.value === 1 ? "repository" : "repositories"}
      </div>
    </div>
  );
}

export function StatusChart({ data }: { data: StatusDatum[] }) {
  const total = data.reduce((n, d) => n + d.value, 0);

  return (
    <Card className="flex h-full flex-col">
      <CardHeader
        title="Status distribution"
        subtitle="Where every tracked repository sits right now"
      />
      <div className="flex flex-1 flex-col items-center gap-4 p-5 sm:flex-row">
        <div className="relative h-[180px] w-[180px] shrink-0">
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={data}
                dataKey="value"
                nameKey="label"
                innerRadius={58}
                outerRadius={86}
                paddingAngle={2}
                stroke="none"
              >
                {data.map((d) => (
                  <Cell key={d.status} fill={d.color} />
                ))}
              </Pie>
              <Tooltip content={<ChartTooltip />} />
            </PieChart>
          </ResponsiveContainer>
          <div className="pointer-events-none absolute inset-0 grid place-items-center">
            <div className="text-center">
              <div className="text-2xl font-semibold">{total}</div>
              <div className="text-[10px] uppercase tracking-wide text-faint">
                repos
              </div>
            </div>
          </div>
        </div>

        <ul className="grid w-full grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
          {data.map((d) => (
            <li
              key={d.status}
              className="flex items-center justify-between gap-2 text-xs"
            >
              <span className="flex items-center gap-2 text-muted">
                <span
                  className="inline-block h-2.5 w-2.5 rounded-sm"
                  style={{ background: d.color }}
                />
                {d.label}
              </span>
              <span className="font-semibold text-text">{d.value}</span>
            </li>
          ))}
        </ul>
      </div>
    </Card>
  );
}
