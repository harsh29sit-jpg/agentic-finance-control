import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import PageHeader from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { StatusPill } from "@/components/StatusPill";
import { rupeesCompact, fmtDate } from "@/lib/format";
import { toast } from "sonner";
import { Play, Upload, Database, RotateCw, Plug, FlaskConical } from "lucide-react";

export default function Batches() {
  const { user } = useAuth();
  const canWrite = user?.role !== "support";
  const canSandbox = ["controller", "admin"].includes(user?.role);
  const [batches, setBatches] = useState([]);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const fileRef = useRef();
  const rzRef = useRef();
  const navigate = useNavigate();

  const load = () => api.get("/batches").then(({ data }) => setBatches(data)).catch(() => {});
  useEffect(() => { load(); }, []);

  const loadSandbox = async () => {
    setBusy(true);
    try {
      const { data } = await api.post("/sandbox/batch");
      toast.success(`Sandbox fixture reconciled · ${data.counts.A + data.counts.B + data.counts.C} rows`);
      await load();
    }
    catch (e) {
      toast.error(e.response?.status === 403
        ? "Sandbox loading is limited to controller/admin roles"
        : e.response?.data?.detail || "Failed");
    } finally { setBusy(false); }
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
      toast.success(`Ingested ${data.name} · ${data.counts.A + data.counts.B + data.counts.C} rows${data.deduplicated ? " (deduplicated)" : ""}`);
      await load();
    } catch (err) { toast.error(err.response?.data?.detail || "Upload failed"); }
    finally { setBusy(false); if (fileRef.current) fileRef.current.value = ""; }
  };

  const uploadRazorpay = async (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    const fd = new FormData();
    fd.append("settlements_file", file);
    fd.append("name", name || `Razorpay ${file.name}`);
    try {
      const { data } = await api.post("/ingestion/razorpay", fd, { headers: { "Content-Type": "multipart/form-data" } });
      if (data.deduplicated) toast.info(`Identical report already ingested · ${data.id.slice(0, 8)}`);
      else toast.success(`Razorpay batch reconciled · ${JSON.stringify(data.parse_summary?.settlements || {})}`);
      await load();
    } catch (err) { toast.error(err.response?.data?.detail || "Connector failed"); }
    finally { setBusy(false); if (rzRef.current) rzRef.current.value = ""; }
  };

  const rerun = async (e, b) => {
    e.stopPropagation();
    setBusy(true);
    try {
      const { data } = await api.post(`/batches/${b.id}/rerun`);
      toast.success(`Rerun created · ${data.name}`);
      await load();
      navigate(`/audit?diff=1&base=${b.parent_batch_id || b.id}&compare=${data.id}`);
    } catch (err) { toast.error(err.response?.data?.detail || "Rerun failed"); }
    finally { setBusy(false); }
  };

  const runRealistic = async () => {
    setBusy(true);
    try {
      const { data } = await api.post("/batches/run-realistic");
      toast.success(`${data.batch_name} · precision ${data.auto_match_precision}% · recall ${data.match_recall}% · F1 ${data.f1_score}%`);
      await load();
    } catch (e) { toast.error(e.response?.data?.detail || "Realistic run failed"); } finally { setBusy(false); }
  };

  return (
    <div>
      <PageHeader title="Batch Ingestion" subtitle="Upload or schedule Source A / B / C ledgers · idempotent re-runs"
        actions={<>
          <input placeholder="Batch name" value={name} onChange={(e) => setName(e.target.value)}
            data-testid="batch-name-input" className="h-9 w-40 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand" />
          <input ref={fileRef} type="file" accept=".csv,.json" onChange={upload} className="hidden" data-testid="upload-input" />
          <input ref={rzRef} type="file" accept=".csv,.xlsx" onChange={uploadRazorpay} className="hidden" data-testid="razorpay-input" />
          {canWrite && <>
            <Button variant="outline" className="h-9 gap-1.5" disabled={busy} data-testid="upload-btn" onClick={() => fileRef.current?.click()}><Upload size={15} /> Upload CSV/JSON</Button>
            <Button variant="outline" className="h-9 gap-1.5 border-brand/40 text-brand hover:bg-brand/5" disabled={busy}
              data-testid="razorpay-btn" onClick={() => rzRef.current?.click()}>
              <Plug size={15} /> Razorpay Report
            </Button>
            <Button variant="outline" className="h-9 gap-1.5 border-brand/40 text-brand hover:bg-brand/5" disabled={busy}
              data-testid="realistic-btn" onClick={runRealistic}>
              <Database size={15} /> Run Realistic (Paysim)
            </Button>
          </>}
          {canSandbox && (
            <Button className="h-9 gap-1.5 bg-brand text-white hover:bg-brand/90" disabled={busy}
              data-testid="sandbox-btn" onClick={loadSandbox}><Play size={15} /> Sandbox Batch</Button>
          )}
        </>} />

      <div className="p-6">
        {!canWrite && <div className="mb-4 rounded-md border border-border bg-secondary/40 px-3 py-2 text-xs text-muted-foreground">Read-only role — ingestion controls are hidden.</div>}
        {canSandbox && (
          <div className="mb-4 flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-xs text-muted-foreground">
            <FlaskConical size={14} className="mt-0.5 shrink-0 text-warning" />
            <span><span className="font-semibold text-foreground">Sandbox batches</span> are synthetic, truth-labelled fixtures for evaluation and policy diffing. They never count toward production dashboards.</span>
          </div>
        )}
        <div className="mb-4 rounded-md border border-border bg-secondary/40 p-3 text-xs text-muted-foreground">
          <span className="font-semibold text-foreground">CSV/JSON schema:</span> <span className="font-mono">source (A|B|C), external_id, settlement_id, utr, amount, merchant_id, rail, narration, txn_date</span>. Amounts accepted in paise or rupees.
          <span className="mx-2 text-border">|</span>
          <span className="font-semibold text-foreground">Razorpay:</span> settlements export (.csv/.xlsx) maps to Source B; pair a bank-statement CSV via API for full A·B·C recon.
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
                  <td className="font-semibold">
                    {b.sandbox && <span title="Synthetic fixture — excluded from production metrics"
                      className="mr-1.5 inline-block rounded bg-warning/15 px-1 py-0.5 align-middle text-[9px] font-bold uppercase tracking-wide text-warning">sandbox</span>}
                    {b.name}
                  </td>
                  <td className="font-mono text-muted-foreground">{b.source_label}</td>
                  <td className="text-right font-mono">{b.counts.A}·{b.counts.B}·{b.counts.C}</td>
                  <td className="text-right font-mono">{b.metrics.deterministic_match_rate}%</td>
                  <td className="text-right font-mono">{b.metrics.inclusive_match_rate}%</td>
                  <td className="text-right font-mono text-destructive">{rupeesCompact(b.metrics.value_at_risk_paise)}</td>
                  <td className="text-right font-mono">{b.metrics.open_exceptions}</td>
                  <td><StatusPill status={b.sandbox ? "pending_review" : "matched"} label={b.sandbox ? "Fixture" : "Reconciled"} /></td>
                  <td className="text-muted-foreground">{fmtDate(b.created_at)}</td>
                  <td className="text-right">
                    <div className="flex items-center justify-end gap-2" onClick={(e) => e.stopPropagation()}>
                      {canWrite && (
                        <button data-testid={`rerun-btn-${b.id}`} disabled={busy} onClick={(e) => rerun(e, b)}
                          className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-[11px] font-semibold text-muted-foreground transition-colors hover:border-brand hover:text-brand disabled:opacity-50">
                          <RotateCw size={12} /> Rerun
                        </button>
                      )}
                      <button onClick={() => navigate(`/workbench?batch=${b.id}`)} className="text-brand">Open →</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
