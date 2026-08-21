import { useEffect, useState, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import api from "@/lib/api";
import PageHeader from "@/components/PageHeader";
import { StatusPill } from "@/components/StatusPill";
import { EvidenceDrawer } from "@/components/EvidenceDrawer";
import { rupees } from "@/lib/format";
import { toast } from "sonner";

const BatchSelect = ({ batches, value, onChange }) => (
  <select data-testid="batch-select" value={value || ""} onChange={(e) => onChange(e.target.value)}
    className="h-9 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand">
    {batches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
  </select>
);

export default function Workbench() {
  const [params, setParams] = useSearchParams();
  const [batches, setBatches] = useState([]);
  const [batchId, setBatchId] = useState(params.get("batch") || "");
  const [rows, setRows] = useState([]);
  const [filter, setFilter] = useState("all");
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.get("/batches").then(({ data }) => {
      setBatches(data);
      if (!batchId && data[0]) { setBatchId(data[0].id); }
    });
  }, []);

  const load = useCallback(() => {
    if (!batchId) return;
    api.get(`/reconciliation?batch_id=${batchId}`).then(({ data }) => setRows(data));
  }, [batchId]);

  useEffect(() => { if (batchId) { setParams({ batch: batchId }); load(); } }, [batchId, load, setParams]);

  const review = async (action, note) => {
    setBusy(true);
    try {
      await api.post(`/reconciliation/${selected.id}/review`, { action, note });
      toast.success(`Match ${action}`);
      setSelected(null); load();
    } catch (e) { toast.error(e.response?.data?.detail || "Failed"); } finally { setBusy(false); }
  };

  const filtered = rows.filter((r) => filter === "all" || r.status === filter);
  const summary = {
    matched: rows.filter((r) => r.status === "matched").length,
    pending: rows.filter((r) => r.status === "pending_review").length,
    total: rows.length,
  };

  return (
    <div>
      <PageHeader title="Reconciliation Workbench" subtitle="Source A / B / C side-by-side · pass trace · evidence drawer"
        actions={<BatchSelect batches={batches} value={batchId} onChange={setBatchId} />} />

      {/* Summary strip */}
      <div className="flex items-center gap-6 border-b border-border bg-secondary/30 px-6 py-2.5 text-xs">
        <span><b className="font-mono text-success">{summary.matched}</b> matched</span>
        <span><b className="font-mono text-brand">{summary.pending}</b> pending review</span>
        <span><b className="font-mono">{summary.total}</b> settlements</span>
        <div className="ml-auto flex gap-1">
          {["all", "matched", "pending_review"].map((f) => (
            <button key={f} data-testid={`filter-${f}`} onClick={() => setFilter(f)}
              className={`rounded px-2.5 py-1 text-xs font-semibold ${filter === f ? "bg-brand text-white" : "border border-border hover:bg-secondary"}`}>
              {f === "all" ? "All" : f === "matched" ? "Matched" : "Pending"}
            </button>
          ))}
        </div>
      </div>

      <div className="overflow-x-auto p-6 pt-4">
        <table className="recon-table w-full text-left text-xs">
          <thead className="sticky top-0 bg-secondary text-muted-foreground">
            <tr>
              <th>Settlement</th><th>UTR</th><th>Merchant</th><th>Rail</th><th className="text-center">Pass</th>
              <th className="text-right">Settlement ₹</th><th className="text-right">Bank ₹</th><th className="text-right">Δ paise</th>
              <th className="text-center">Conf.</th><th className="text-center">Pays</th><th>Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {filtered.length === 0 && <tr><td colSpan={11} className="py-10 text-center text-muted-foreground">No matches for this filter</td></tr>}
            {filtered.map((r) => (
              <tr key={r.id} data-testid={`recon-row-${r.settlement_id}`} className="cursor-pointer" onClick={() => setSelected(r)}>
                <td className="font-mono font-semibold">{r.settlement_id}</td>
                <td className="font-mono text-muted-foreground">{r.utr}</td>
                <td>{r.merchant_id}</td>
                <td><span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px]">{r.rail}</span></td>
                <td className="text-center"><span className="rounded bg-brand/10 px-1.5 py-0.5 font-mono text-[10px] font-bold text-brand">P{r.pass_number}</span></td>
                <td className="text-right font-mono">{rupees(r.settlement_amount_paise)}</td>
                <td className="text-right font-mono">{rupees(r.bank_amount_paise)}</td>
                <td className={`text-right font-mono ${r.tolerance_paise > 0 ? "text-warning" : "text-muted-foreground"}`}>{r.tolerance_paise}</td>
                <td className="text-center font-mono">{(r.confidence * 100).toFixed(0)}%</td>
                <td className="text-center font-mono text-muted-foreground">{r.payments_count}</td>
                <td><StatusPill status={r.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <EvidenceDrawer open={!!selected} onClose={() => setSelected(null)} data={selected} kind="match"
        onReview={selected?.status === "pending_review" ? review : null} busy={busy} />
    </div>
  );
}
