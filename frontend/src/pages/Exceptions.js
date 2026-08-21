import { useEffect, useState, useCallback } from "react";
import api from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import PageHeader from "@/components/PageHeader";
import { StatusPill } from "@/components/StatusPill";
import { EvidenceDrawer } from "@/components/EvidenceDrawer";
import { rupees, rupeesCompact, TAXONOMY_LABEL } from "@/lib/format";
import { toast } from "sonner";
import { AlertTriangle, Clock } from "lucide-react";

const GROUPS = [{ k: "none", l: "Flat" }, { k: "taxonomy", l: "Taxonomy" }, { k: "merchant_id", l: "Merchant" }, { k: "rail", l: "Bank / Rail" }];

export default function Exceptions() {
  const { user } = useAuth();
  const [batches, setBatches] = useState([]);
  const [batchId, setBatchId] = useState("");
  const [groupBy, setGroupBy] = useState("taxonomy");
  const [data, setData] = useState({ grouped: false, items: [] });
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState(false);
  const canApprove = ["controller", "admin"].includes(user?.role);

  useEffect(() => {
    api.get("/batches").then(({ data }) => { setBatches(data); if (data[0]) setBatchId(data[0].id); });
  }, []);

  const load = useCallback(() => {
    if (!batchId) return;
    const g = groupBy === "none" ? "" : `&group_by=${groupBy}`;
    api.get(`/exceptions?batch_id=${batchId}${g}`).then(({ data }) => setData(data));
  }, [batchId, groupBy]);
  useEffect(() => { load(); }, [load]);

  const refreshSelected = async (id) => {
    const { data } = await api.get(`/exceptions/${id}`);
    setSelected(data);
  };

  const analyze = async (c) => {
    setBusy(true);
    try { const { data } = await api.post(`/exceptions/${c.id}/analyze`); setSelected(data); toast.success("AI analysis complete"); load(); }
    catch (e) { toast.error(e.response?.data?.detail || "Analysis failed"); } finally { setBusy(false); }
  };

  const review = async (action, note) => {
    setBusy(true);
    try {
      const { data } = await api.post(`/exceptions/${selected.id}/review`, { action, note });
      toast.success(`Case ${data.status}`);
      if (data.status === "pending_approval") { await refreshSelected(selected.id); } else setSelected(null);
      load();
    } catch (e) { toast.error(e.response?.data?.detail || "Failed"); } finally { setBusy(false); }
  };

  const approve = async (ok, note) => {
    setBusy(true);
    try { await api.post(`/exceptions/${selected.id}/override-approval`, { approve: ok, note }); toast.success(ok ? "Override approved" : "Override rejected"); setSelected(null); load(); }
    catch (e) { toast.error(e.response?.data?.detail || "Failed"); } finally { setBusy(false); }
  };

  const Row = ({ c }) => (
    <tr data-testid={`exception-row-${c.id}`} className="cursor-pointer" onClick={() => setSelected(c)}>
      <td><span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] font-bold uppercase text-destructive">{TAXONOMY_LABEL[c.taxonomy] || c.taxonomy}</span></td>
      <td className="font-mono">{c.settlement_id || c.utr || "—"}</td>
      <td>{c.merchant_id || <span className="text-muted-foreground">unmapped</span>}</td>
      <td className="font-mono text-[10px]">{c.rail}</td>
      <td className="max-w-xs truncate text-muted-foreground">{c.reason}</td>
      <td className="text-right font-mono font-semibold text-destructive">{rupees(c.value_at_risk_paise)}</td>
      <td className="text-center">{c.sla_breached ? <span className="inline-flex items-center gap-1 text-[10px] font-bold text-warning"><Clock size={11} />SLA</span> : <span className="text-[10px] text-muted-foreground">OK</span>}</td>
      <td className="text-center">{c.ai_analyzed ? <span className="text-[10px] font-semibold text-brand">AI ✓</span> : <span className="text-[10px] text-muted-foreground">pending</span>}</td>
      <td><StatusPill status={c.status} /></td>
    </tr>
  );

  const Head = () => (
    <thead className="sticky top-0 bg-secondary text-muted-foreground">
      <tr><th>Taxonomy</th><th>Reference</th><th>Merchant</th><th>Rail</th><th>Reason</th><th className="text-right">Value at Risk</th><th className="text-center">SLA</th><th className="text-center">AI</th><th>Status</th></tr>
    </thead>
  );

  return (
    <div>
      <PageHeader title="Exception Command Center" subtitle="Resolve the long tail · bulk triage · value-at-risk sorting"
        actions={
          <div className="flex items-center gap-2">
            <div className="flex rounded border border-border p-0.5">
              {GROUPS.map((g) => <button key={g.k} data-testid={`group-${g.k}`} onClick={() => setGroupBy(g.k)}
                className={`rounded px-2.5 py-1 text-xs font-semibold ${groupBy === g.k ? "bg-brand text-white" : "hover:bg-secondary"}`}>{g.l}</button>)}
            </div>
            <select data-testid="exc-batch-select" value={batchId} onChange={(e) => setBatchId(e.target.value)}
              className="h-9 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand">
              {batches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          </div>} />

      <div className="space-y-4 p-6">
        {data.grouped ? (
          data.groups.length === 0 ? <Empty /> : data.groups.map((g) => (
            <div key={g.key} className="overflow-hidden rounded-md border border-border">
              <div className="flex items-center justify-between bg-accent px-4 py-2 text-white">
                <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-wide">
                  <AlertTriangle size={14} /> {TAXONOMY_LABEL[g.key] || g.key}
                  <span className="rounded bg-white/15 px-1.5 py-0.5 font-mono text-[10px]">{g.count}</span>
                </div>
                <span className="font-mono text-xs">{rupeesCompact(g.value_at_risk_paise)} at risk</span>
              </div>
              <table className="recon-table w-full text-left text-xs"><Head /><tbody className="divide-y divide-border">{g.items.map((c) => <Row key={c.id} c={c} />)}</tbody></table>
            </div>
          ))
        ) : (
          <div className="overflow-hidden rounded-md border border-border">
            <table className="recon-table w-full text-left text-xs"><Head />
              <tbody className="divide-y divide-border">
                {data.items.length === 0 ? <tr><td colSpan={9}><Empty /></td></tr> : data.items.map((c) => <Row key={c.id} c={c} />)}
              </tbody></table>
          </div>
        )}
      </div>

      <EvidenceDrawer open={!!selected} onClose={() => setSelected(null)} data={selected} kind="exception"
        onReview={review} onAnalyze={analyze} onApprove={approve} canApprove={canApprove} busy={busy} />
    </div>
  );
}

const Empty = () => <div className="py-12 text-center text-sm text-muted-foreground">No exceptions in this batch — clean reconciliation.</div>;
