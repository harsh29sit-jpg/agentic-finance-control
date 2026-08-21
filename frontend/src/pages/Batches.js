import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "@/lib/api";
import PageHeader from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { StatusPill } from "@/components/StatusPill";
import { rupeesCompact, fmtDate } from "@/lib/format";
import { toast } from "sonner";
import { Play, Upload, Database } from "lucide-react";

export default function Batches() {
  const [batches, setBatches] = useState([]);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const fileRef = useRef();
  const navigate = useNavigate();

  const load = () => api.get("/batches").then(({ data }) => setBatches(data)).catch(() => {});
  useEffect(() => { load(); }, []);

  const runDemo = async () => {
    setBusy(true);
    try { const { data } = await api.post("/batches/run-demo"); toast.success(`Reconciled ${data.name}`); await load(); }
    catch (e) { toast.error(e.response?.data?.detail || "Failed"); } finally { setBusy(false); }
  };

  const upload = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    const fd = new FormData();
    fd.append("file", file);
    fd.append("name", name || file.name);
    try {
      const { data } = await api.post("/ingestion/upload", fd, { headers: { "Content-Type": "multipart/form-data" } });
      toast.success(`Ingested ${data.name} · ${data.counts.A + data.counts.B + data.counts.C} rows`);
      await load();
    } catch (err) { toast.error(err.response?.data?.detail || "Upload failed"); }
    finally { setBusy(false); if (fileRef.current) fileRef.current.value = ""; }
  };

  return (
    <div>
      <PageHeader title="Batch Ingestion" subtitle="Upload or schedule Source A / B / C ledgers · idempotent re-runs"
        actions={<>
          <input placeholder="Batch name" value={name} onChange={(e) => setName(e.target.value)}
            data-testid="batch-name-input" className="h-9 w-40 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand" />
          <input ref={fileRef} type="file" accept=".csv,.json" onChange={upload} className="hidden" data-testid="upload-input" />
          <Button variant="outline" className="h-9 gap-1.5" disabled={busy} data-testid="upload-btn" onClick={() => fileRef.current?.click()}><Upload size={15} /> Upload CSV/JSON</Button>
          <Button className="h-9 gap-1.5 bg-brand text-white hover:bg-brand/90" disabled={busy} data-testid="batch-run-demo" onClick={runDemo}><Play size={15} /> Run Demo Batch</Button>
        </>} />

      <div className="p-6">
        <div className="mb-4 rounded-md border border-border bg-secondary/40 p-3 text-xs text-muted-foreground">
          <span className="font-semibold text-foreground">CSV/JSON schema:</span> <span className="font-mono">source (A|B|C), external_id, settlement_id, utr, amount, merchant_id, rail, narration, txn_date</span>. Amounts accepted in paise or rupees.
        </div>

        <div className="overflow-hidden rounded-md border border-border">
          <table className="recon-table w-full text-left text-xs">
            <thead className="bg-secondary text-muted-foreground">
              <tr>
                <th>Batch</th><th>Source</th><th className="text-right">A·B·C</th><th className="text-right">Det. Rate</th>
                <th className="text-right">Incl. Rate</th><th className="text-right">Value at Risk</th><th className="text-right">Exceptions</th>
                <th>Status</th><th>Created</th><th></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {batches.length === 0 && <tr><td colSpan={10} className="py-10 text-center text-muted-foreground"><Database className="mx-auto mb-2 opacity-40" /> No batches yet</td></tr>}
              {batches.map((b) => (
                <tr key={b.id} data-testid={`batch-row-${b.id}`} className="cursor-pointer" onClick={() => navigate(`/workbench?batch=${b.id}`)}>
                  <td className="font-semibold">{b.name}</td>
                  <td className="font-mono text-muted-foreground">{b.source_label}</td>
                  <td className="text-right font-mono">{b.counts.A}·{b.counts.B}·{b.counts.C}</td>
                  <td className="text-right font-mono">{b.metrics.deterministic_match_rate}%</td>
                  <td className="text-right font-mono">{b.metrics.inclusive_match_rate}%</td>
                  <td className="text-right font-mono text-destructive">{rupeesCompact(b.metrics.value_at_risk_paise)}</td>
                  <td className="text-right font-mono">{b.metrics.open_exceptions}</td>
                  <td><StatusPill status="matched" label="Reconciled" /></td>
                  <td className="text-muted-foreground">{fmtDate(b.created_at)}</td>
                  <td className="text-right text-brand">Open →</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
