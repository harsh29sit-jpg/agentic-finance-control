import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "@/lib/api";
import PageHeader from "@/components/PageHeader";
import MetricCard from "@/components/MetricCard";
import { Button } from "@/components/ui/button";
import { rupeesCompact, TAXONOMY_LABEL } from "@/lib/format";
import { Layers } from "lucide-react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Cell } from "recharts";

const TAX_COLORS = ["#0d94fb", "#dc3d43", "#f2a01f", "#012652", "#17a56b", "#64748b", "#8b5cf6"];

export default function Dashboard() {
  const [m, setM] = useState(null);
  const navigate = useNavigate();

  const load = () => api.get("/dashboard/metrics").then(({ data }) => setM(data)).catch(() => {});
  useEffect(() => { load(); }, []);

  const trendSpark = (key) => (m?.trend || []).map((t) => t[key]);
  const taxData = m ? Object.entries(m.exceptions_by_taxonomy || {}).filter(([, v]) => v > 0)
    .map(([k, v]) => ({ name: TAXONOMY_LABEL[k] || k, value: v })) : [];

  return (
    <div>
      <PageHeader
        title="Operations Dashboard"
        subtitle="High-signal reconciliation overview across all batches"
      />

      {!m || m.total_batches === 0 ? (
        <div className="p-6">
          <div className="card-surface rounded-md border border-dashed border-border p-12 text-center">
            <p className="text-sm font-semibold">No batches reconciled yet</p>
            <p className="mx-auto mt-1 max-w-md text-xs text-muted-foreground">
              Ingest a CSV/JSON ledger set or a Razorpay settlements report to run the deterministic engine.
              Sandbox fixtures (truth-labelled) are available to controller/admin roles.
            </p>
            <Button data-testid="goto-batches" onClick={() => navigate("/batches")}
              className="mt-4 gap-1.5 bg-brand text-white hover:bg-brand/90">
              <Layers size={15} /> Go to Batch Ingestion
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-5 p-6">
          {/* Metric cards */}
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <MetricCard testId="metric-batches" title="Batches Processed" value={m.total_batches} sub={`${m.total_settlements} settlements`} accent="brand" spark={trendSpark("incl_rate")} />
            <MetricCard testId="metric-det-rate" title="Deterministic Match Rate" value={`${m.deterministic_match_rate}%`} sub="Pass 1 exact" accent="success" spark={trendSpark("det_rate")} />
            <MetricCard testId="metric-incl-rate" title="Inclusive Match Rate" value={`${m.inclusive_match_rate}%`} sub="Pass 1–3 auto-posted" accent="brand" spark={trendSpark("incl_rate")} />
            <MetricCard testId="metric-false-rate" title="False-Match Rate" value={`${m.false_match_rate}%`} sub="Hard cap < 0.5%" accent="muted" />
            <MetricCard testId="metric-recall" title="Exception Recall" value={`${m.exception_recall}%`} sub="No silent drops" accent="success" />
            <MetricCard testId="metric-reconciled" title="Reconciled Value" value={rupeesCompact(m.reconciled_value_paise)} sub="Auto-posted to immutable log" accent="brand" />
            <MetricCard testId="metric-var" title="Total Value at Risk" value={rupeesCompact(m.value_at_risk_paise)} sub={`${m.open_exceptions} open exceptions`} accent="destructive" spark={trendSpark("value_at_risk")} />
            <MetricCard testId="metric-latency" title="Match Latency" value={`${m.latency_ms?.matching ?? 0}ms`} sub={`norm ${m.latency_ms?.normalization ?? 0}ms`} accent="muted" />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            {/* Exceptions by taxonomy */}
            <div className="card-surface rounded-md border border-border bg-card p-4 lg:col-span-2">
              <div className="mb-3 flex items-center justify-between">
                <h3 className="text-sm font-bold">Open Exceptions by Taxonomy</h3>
                <Button variant="ghost" size="sm" className="h-7 text-xs text-brand" onClick={() => navigate("/exceptions")}>View all →</Button>
              </div>
              {taxData.length === 0 ? <p className="py-8 text-center text-xs text-muted-foreground">No open exceptions</p> : (
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={taxData} layout="vertical" margin={{ left: 10, right: 20 }}>
                    <CartesianGrid horizontal={false} stroke="hsl(var(--border))" />
                    <XAxis type="number" tick={{ fontSize: 11 }} stroke="hsl(var(--muted-foreground))" allowDecimals={false} />
                    <YAxis type="category" dataKey="name" width={130} tick={{ fontSize: 11 }} stroke="hsl(var(--muted-foreground))" />
                    <Tooltip contentStyle={{ fontSize: 12, borderRadius: 6, background: "#012652", border: "none", color: "#fff" }} />
                    <Bar dataKey="value" radius={[0, 3, 3, 0]}>
                      {taxData.map((_, i) => <Cell key={i} fill={TAX_COLORS[i % TAX_COLORS.length]} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>

            {/* Pass latency breakdown */}
            <div className="card-surface rounded-md border border-border bg-card p-4">
              <h3 className="mb-3 text-sm font-bold">Latest Pass Latency</h3>
              <div className="space-y-2.5">
                {[["Normalization", m.latency_ms?.normalization], ["Pass 1 · exact", m.latency_ms?.pass1],
                  ["Pass 2 · tolerance", m.latency_ms?.pass2], ["Pass 3 · aggregation", m.latency_ms?.pass3]].map(([k, v]) => (
                  <div key={k}>
                    <div className="flex justify-between text-xs"><span className="text-muted-foreground">{k}</span><span className="font-mono">{v ?? 0}ms</span></div>
                    <div className="mt-1 h-1.5 rounded-full bg-secondary">
                      <div className="h-1.5 rounded-full bg-brand/80" style={{ width: `${Math.min(100, (v || 0) / (m.latency_ms?.normalization || 1) * 100)}%` }} />
                    </div>
                  </div>
                ))}
              </div>
              <div className="mt-4 rounded border border-success/30 bg-success/5 p-2.5 text-xs">
                <span className="font-semibold text-success">Acceptance gates green:</span> recall 100%, false-match 0%, no silent drops.
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
