import { ResponsiveContainer, AreaChart, Area } from "recharts";
import { cn } from "@/lib/utils";

export const MetricCard = ({ title, value, sub, trend, spark, accent = "brand", testId }) => {
  const accentText = {
    brand: "text-brand", success: "text-success", warning: "text-warning",
    destructive: "text-destructive", muted: "text-muted-foreground",
  }[accent] || "text-brand";

  const strokeColor = {
    brand: "#0d94fb", success: "#17a56b", warning: "#f2a01f", destructive: "#dc3d43", muted: "#64748b",
  }[accent] || "#0d94fb";

  return (
    <div
      data-testid={testId}
      className="card-surface rounded-md border border-border bg-card p-4 transition-colors hover:border-brand/40"
    >
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{title}</p>
          <p className={cn("mt-1.5 text-[22px] font-bold tabular leading-none", accentText)}>{value}</p>
          {sub && <p className="mt-1.5 text-xs text-muted-foreground truncate">{sub}</p>}
        </div>
        {trend != null && (
          <span className={cn("shrink-0 text-xs font-semibold", trend >= 0 ? "text-success" : "text-destructive")}>
            {trend >= 0 ? "▲" : "▼"} {Math.abs(trend)}%
          </span>
        )}
      </div>
      {spark && spark.length > 1 && (
        <div className="mt-3 h-8 -mx-1">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={spark.map((v, i) => ({ i, v }))} margin={{ top: 2, bottom: 0, left: 0, right: 0 }}>
              <defs>
                <linearGradient id={`g-${title}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={strokeColor} stopOpacity={0.28} />
                  <stop offset="100%" stopColor={strokeColor} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey="v" stroke={strokeColor} strokeWidth={1.25} fill={`url(#g-${title})`} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
};

export default MetricCard;
