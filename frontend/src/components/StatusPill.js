import { cn } from "@/lib/utils";

// Semantic status pills — subtle fills, refined palette.
const MAP = {
  matched: { label: "Matched", cls: "bg-[#e7f6ef] text-[#0f7d55] border-[#17a56b]/35 dark:bg-[#17a56b]/10 dark:text-[#3ddc97]" },
  resolved: { label: "Resolved", cls: "bg-[#e7f6ef] text-[#0f7d55] border-[#17a56b]/35 dark:bg-[#17a56b]/10 dark:text-[#3ddc97]" },
  pending_review: { label: "Pending Review", cls: "bg-[#e8f3fe] text-[#0b6fc2] border-[#0d94fb]/35 dark:bg-[#0d94fb]/10 dark:text-[#5cb8ff]" },
  pending_approval: { label: "Pending Approval", cls: "bg-[#fdf4e0] text-[#95620a] border-[#f2a01f]/45 dark:bg-[#f2a01f]/10 dark:text-[#ffc069]" },
  open: { label: "Open", cls: "bg-[#fdeded] text-[#b02a37] border-[#dc3d43]/35 dark:bg-[#dc3d43]/10 dark:text-[#ff8a8a]" },
  exception: { label: "Exception", cls: "bg-[#fdeded] text-[#b02a37] border-[#dc3d43]/35 dark:bg-[#dc3d43]/10 dark:text-[#ff8a8a]" },
  escalated: { label: "Escalated", cls: "bg-[#fdf4e0] text-[#95620a] border-[#f2a01f]/45 dark:bg-[#f2a01f]/10 dark:text-[#ffc069]" },
  rejected: { label: "Rejected", cls: "bg-muted text-muted-foreground border-border" },
  timing_lag: { label: "Timing Lag", cls: "bg-[#fdf4e0] text-[#95620a] border-[#f2a01f]/45" },
  high_risk: { label: "High Risk", cls: "bg-[#a92831] text-white border-[#a92831]" },
};

export const StatusPill = ({ status, label, testId, className = "" }) => {
  const cfg = MAP[status] || { label: label || status, cls: "bg-muted text-muted-foreground border-border" };
  return (
    <span
      data-testid={testId || `status-${status}`}
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide",
        cfg.cls,
        className,
      )}
    >
      {label || cfg.label}
    </span>
  );
};

export default StatusPill;
