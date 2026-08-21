import { useEffect, useState } from "react";
import api from "@/lib/api";
import PageHeader from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { StatusPill } from "@/components/StatusPill";
import { rupees, rupeesCompact, TAXONOMY_LABEL } from "@/lib/format";
import { Download, CheckCircle2, XCircle } from "lucide-react";

export default function Reports() {
  const [batches, setBatches] = useState([]);
  const [batchId, setBatchId] = useState("");
  const [rep, setRep] = useState(null);

  useEffect(() => { api.get("/batches").then(({ data }) => { setBatches(data); if (data[0]) setBatchId(data[0].id); }); }, []);
  useEffect(() => { if (batchId) api.get(`/reports/${batchId}`).then(({ data }) => setRep(data)); }, [batchId]);

  const exportCsv = () => {
    if (!rep) return;
    const rows = [["taxonomy", "settlement_id", "utr", "merchant", "rail", "value_at_risk_paise", "status", "reason"]];
    rep.exception_ledger.forEach((e) => rows.push([e.taxonomy, e.settlement_id, e.utr, e.merchant_id, e.rail, e.value_at_risk_paise, e.status, `"${(e.reason || "").replace(/"/g, "'")}"`]));
    const blob = new Blob([rows.map((r) => r.join(",")).join("\n")], { type: "text/csv" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `exception_ledger_${batchId.slice(0, 8)}.csv`; a.click();
  };

  const m = rep?.metrics;
  const maxMerchant = Math.max(1, ...(rep?.value_at_risk_by_merchant || []).map((x) => x.value_paise));

  return (
    <div>
      <PageHeader title="Reporting & Audit" subtitle="Batch summary · value-at-risk · exception ledger · acceptance gates"
        actions={<>
          <select data-testid="rep-batch-select" value={batchId} onChange={(e) => setBatchId(e.target.value)}
            className="h-9 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand">
            {batches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
          </select>
          <Button variant="outline" className="h-9 gap-1.5" data-testid="export-csv-btn" onClick={exportCsv}><Download size={15} /> Export Ledger</Button>
        </>} />

      {rep && (
        <div className="space-y-5 p-6">
          {/* Acceptance gates */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {Object.entries(rep.acceptance_gates).map(([k, g]) => (
              <div key={k} data-testid={`gate-${k}`} className={`rounded-md border p-3 ${g.pass ? "border-success/30 bg-success/5" : "border-destructive/30 bg-destructive/5"}`}>
                <div className="flex items-center justify-between">
                  <span className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{k.replace(/_/g, " ")}</span>
                  {g.pass ? <CheckCircle2 size={15} className="text-success" /> : <XCircle size={15} className="text-destructive" />}
                </div>
                <div className="mt-1 font-mono text-lg font-bold">{typeof g.value === "number" ? `${g.value}` : g.value}</div>
                <div className="text-[10px] text-muted-foreground">target {g.target}</div>
              </div>
            ))}
          </div>

          {/* Summary stats */}
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {[["Reconciled value", rupeesCompact(m.reconciled_value_paise), "text-success"],
              ["Value at risk", rupeesCompact(m.value_at_risk_paise), "text-destructive"],
              ["Auto-matched", `${m.auto_matched}/${m.total_settlements}`, "text-brand"],
              ["Open exceptions", m.open_exceptions, "text-warning"]].map(([l, v, c]) => (
              <div key={l} className="rounded-md border border-border bg-card p-3">
                <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{l}</div>
                <div className={`mt-1 font-mono text-xl font-bold ${c}`}>{v}</div>
              </div>
            ))}
          </div>

          {/* Value at risk by merchant */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div className="rounded-md border border-border bg-card p-4">
              <h3 className="mb-3 text-sm font-bold">Value at Risk · by Merchant</h3>
              <div className="space-y-2">
                {rep.value_at_risk_by_merchant.slice(0, 6).map((x) => (
                  <div key={x.key}>
                    <div className="flex justify-between text-xs"><span className="font-mono">{x.key || "unmapped"}</span><span className="font-mono text-destructive">{rupeesCompact(x.value_paise)}</span></div>
                    <div className="mt-1 h-1.5 rounded-full bg-secondary"><div className="h-1.5 rounded-full bg-destructive" style={{ width: `${x.value_paise / maxMerchant * 100}%` }} /></div>
                  </div>
                ))}
              </div>
            </div>
            <div className="rounded-md border border-border bg-card p-4">
              <h3 className="mb-3 text-sm font-bold">Value at Risk · by Rail</h3>
              <div className="space-y-2">
                {rep.value_at_risk_by_rail.map((x) => (
                  <div key={x.key} className="flex items-center justify-between rounded border border-border px-3 py-2 text-xs">
                    <span className="rounded bg-secondary px-2 py-0.5 font-mono">{x.key}</span>
                    <span className="font-mono font-semibold text-destructive">{rupees(x.value_paise)}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Exception ledger */}
          <div className="overflow-hidden rounded-md border border-border">
            <div className="border-b border-border bg-secondary px-4 py-2 text-xs font-bold uppercase tracking-wide">Complete Exception Ledger ({rep.exception_ledger.length})</div>
            <table className="recon-table w-full text-left text-xs">
              <thead className="bg-card text-muted-foreground"><tr><th>Taxonomy</th><th>Reference</th><th>Merchant</th><th className="text-right">Value at Risk</th><th>Status</th></tr></thead>
              <tbody className="divide-y divide-border">
                {rep.exception_ledger.map((e) => (
                  <tr key={e.id}><td>{TAXONOMY_LABEL[e.taxonomy] || e.taxonomy}</td><td className="font-mono">{e.settlement_id || e.utr}</td><td>{e.merchant_id || "—"}</td>
                    <td className="text-right font-mono text-destructive">{rupees(e.value_at_risk_paise)}</td><td><StatusPill status={e.status} /></td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
