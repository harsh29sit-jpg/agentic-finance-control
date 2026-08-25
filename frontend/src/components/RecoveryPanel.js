import { useCallback, useEffect, useState } from "react";
import api from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { rupeesCompact } from "@/lib/format";
import { toast } from "sonner";
import { ShieldCheck, Zap, CircleSlash, Clock } from "lucide-react";

const ACTION_LABEL = {
  link_orphan_credit: "Link orphan credit",
  resolve_as_fee: "Resolve as fee delta",
  draft_bank_query: "Draft bank query",
  sla_watch_auto_clear: "SLA watch · auto-clear",
};

export default function RecoveryPanel({ batchId }) {
  const { user } = useAuth();
  const canExecute = ["analyst", "controller", "admin"].includes(user?.role);
  const [plan, setPlan] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [policy, setPolicy] = useState(null);
  const [picked, setPicked] = useState({});
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    if (!batchId) return;
    api.get(`/recovery/plan?batch_id=${batchId}`).then(({ data }) => setPlan(data)).catch(() => {});
  }, [batchId]);

  useEffect(() => {
    load();
    api.get("/recovery/metrics").then(({ data }) => setMetrics(data)).catch(() => {});
    api.get("/recovery/policy").then(({ data }) => setPolicy(data)).catch(() => {});
    setPicked({});
  }, [load]);

  const execute = async () => {
    const ids = Object.keys(picked).filter((k) => picked[k]);
    if (!ids.length) return;
    setBusy(true);
    try {
      const { data } = await api.post("/recovery/execute", { case_ids: ids });
      const counts = data.results.reduce((a, r) => ((a[r.outcome] = (a[r.outcome] || 0) + 1), a), {});
      toast.success(`Recovery: ${Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(" · ")}`);
      load();
      api.get("/recovery/metrics").then(({ data: m }) => setMetrics(m)).catch(() => {});
      setPicked({});
    } catch (e) {
      toast.error(e.response?.data?.detail || "Recovery failed");
    } finally { setBusy(false); }
  };

  if (!plan) return null;
  const items = plan.plan || [];

  return (
    <div className="rounded-md border border-border bg-card" data-testid="recovery-panel">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-accent px-4 py-2 text-white">
        <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wide">
          <Zap size={14} /> Bounded Recovery Orchestrator
          <span className="rounded bg-white/15 px-1.5 py-0.5 font-mono text-[10px]">
            policy v{policy?.version ?? 0}
          </span>
          {policy && !policy.enabled && (
            <span data-testid="recovery-killswitch" className="flex items-center gap-1 rounded bg-destructive px-1.5 py-0.5 font-mono text-[10px] font-bold">
              <CircleSlash size={11} /> KILL SWITCH ON
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 font-mono text-[11px]">
          <span className="text-success" data-testid="recovery-recovered">₹{rupeesCompact(metrics?.value_recovered_paise || 0)} recovered</span>
          <span className="text-warning flex items-center gap-1"><Clock size={11} /> ₹{rupeesCompact(metrics?.value_pending_paise || 0)} pending</span>
          <span className="text-white/60">{metrics?.attempts || 0} attempts</span>
        </div>
      </div>

      {items.length === 0 ? (
        <p className="px-4 py-3 text-xs text-muted-foreground">No bounded recovery actions proposed for this batch.</p>
      ) : (
        <table className="w-full text-left text-xs">
          <thead className="bg-secondary/60 text-muted-foreground">
            <tr><th></th><th>Taxonomy</th><th>Ref</th><th>Action</th><th>Evidence</th><th className="text-right">VaR</th><th>Attempts</th></tr>
          </thead>
          <tbody className="divide-y divide-border">
            {items.map((it) => (
              <tr key={it.case_id} data-testid={`recovery-row-${it.case_id}`}>
                <td className="pl-4">
                  {canExecute && (
                    <input type="checkbox" data-testid={`recover-check-${it.case_id}`}
                      checked={!!picked[it.case_id]}
                      onChange={(e) => setPicked((p) => ({ ...p, [it.case_id]: e.target.checked }))} />
                  )}
                </td>
                <td><span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] font-bold uppercase text-destructive">{it.taxonomy}</span></td>
                <td className="font-mono">{it.settlement_id || it.case_id.slice(0, 8)}</td>
                <td className="font-semibold">{ACTION_LABEL[it.action]}</td>
                <td className="max-w-[220px] truncate font-mono text-[10px] text-muted-foreground">{JSON.stringify(it.evidence)}</td>
                <td className="text-right font-mono">{rupeesCompact(it.value_at_risk_paise)}</td>
                <td className="font-mono text-muted-foreground">{it.attempts_used}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {canExecute && items.length > 0 && (
        <div className="flex items-center justify-between border-t border-border px-4 py-2">
          <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
            <ShieldCheck size={12} /> Every action passes attempt-budget · cool-off · daily value cap · maker-checker gates, and lands in the audit chain.
          </span>
          <Button size="sm" data-testid="recovery-execute-btn" disabled={busy || !Object.values(picked).some(Boolean)}
            onClick={execute} className="h-7 gap-1 bg-brand text-[11px] font-semibold text-white hover:bg-brand/90">
            <Zap size={12} /> Execute selected ({Object.values(picked).filter(Boolean).length})
          </Button>
        </div>
      )}
    </div>
  );
}
