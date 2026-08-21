import { cn } from "@/lib/utils";

const MAP = {
  matched: { label: "Matched", cls: "bg-[#e6f8f0] text-[#04a05a] border-[#04db7c]/40 dark:bg-[#04db7c]/10 dark:text-[#2ee89a]" },
  resolved: { label: "Resolved", cls: "bg-[#e6f8f0] text-[#04a05a] border-[#04db7c]/40 dark:bg-[#04db7c]/10 dark:text-[#2ee89a]" },
  pending_review: { label: "Pending Review", cls: "bg-[#e5f4ff] text-[#0d78d1] border-[#0d94fb]/40 dark:bg-[#0d94fb]/10 dark:text-[#5cb8ff]" },
  pending_approval: { label: "Pending Approval", cls: "bg-[#fffae6] text-[#b06f00] border-[#ff991f]/50 dark:bg-[#ff991f]/10 dark:text-[#ffbb55]" },
  open: { label: "Open", cls: "bg-[#ffebe6] text-[#c53415] border-[#de350b]/40 dark:bg-[#de350b]/10 dark:text-[#ff7a5c]" },
  exception: { label: "Exception", cls: "bg-[#ffebe6] text-[#c53415] border-[#de350b]/40 dark:bg-[#de350b]/10 dark:text-[#ff7a5c]" },
  escalated: { label: "Escalated", cls: "bg-[#fffae6] text-[#b06f00] border-[#ff991f]/50 dark:bg-[#ff991f]/10 dark:text-[#ffbb55]" },
  rejected: { label: "Rejected", cls: "bg-muted text-muted-foreground border-border" },
  timing_lag: { label: "Timing Lag", cls: "bg-[#fffae6] text-[#b06f00] border-[#ff991f]/50" },
  high_risk: { label: "High Risk", cls: "bg-[#bf2600] text-white border-[#bf2600]" },
};

export const StatusPill = ({ status, label, testId }) => {
  const cfg = MAP[status] || { label: label || status, cls: "bg-muted text-muted-foreground border-border" };
  return (
    <span
      data-testid={testId}
      className={cn(
        "inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-semibold uppercase tracking-wide border leading-none",
        cfg.cls
      )}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      {label || cfg.label}
    </span>
  );
};

export default StatusPill;
