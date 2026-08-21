import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import api from "@/lib/api";
import PageHeader from "@/components/PageHeader";
import { fmtDate, rupeesCompact } from "@/lib/format";
import { ScrollText, Cpu, GitBranch, GitCompareArrows, ArrowRight } from "lucide-react";

const ACTION_COLOR = {
  auto_post: "text-success", batch_created: "text-brand", ai_analyze: "text-brand",
  copilot_query: "text-brand", policy_updated: "text-warning", batch_rerun: "text-warning",
};

const KIND_STYLE = {
  resolved: "border-success/40 bg-success/5 text-success",
  regressed: "border-destructive/40 bg-destructive/5 text-destructive",
  changed: "border-warning/40 bg-warning/5 text-warning",
};

export default function Audit() {
  const [params] = useSearchParams();
  const [batches, setBatches] = useState([]);
  const [batchId, setBatchId] = useState("");
  const [tab, setTab] = useState(params.get("diff") ? "diff" : "events");
  const [events, setEvents] = useState([]);
  const [invs, setInvs] = useState([]);
  const [base, setBase] = useState(params.get("base") || "");
  const [compare, setCompare] = useState(params.get("compare") || "");
  const [diff, setDiff] = useState(null);

  useEffect(() => {
    api.get("/batches").then(({ data }) => {
      setBatches(data);
      if (data[0] && !base) setBase(data[0].id);
      if (data[1] && !compare) setCompare(data[1].id);
    });
  }, []);

  useEffect(() => {
    const q = batchId ? `?batch_id=${batchId}` : "";
    api.get(`/audit${q}`).then(({ data }) => setEvents(data));
    api.get(`/model-invocations${q}`).then(({ data }) => setInvs(data));
  }, [batchId]);

  useEffect(() => {
    if (tab === "diff" && base && compare && base !== compare)
      api.get(`/diff?base=${base}&compare=${compare}`).then(({ data }) => setDiff(data)).catch(() => setDiff(null));
    else if (base === compare) setDiff(null);
  }, [tab, base, compare]);

  return (
    <div>
      <PageHeader title="Audit & Controls Console" subtitle="Append-only history · model invocations · rerun diff"
        actions={tab !== "diff" && (
          <select data-testid="audit-batch-select" value={batchId} onChange={(e) => setBatchId(e.target.value)}
            className="h-9 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand">
            <option value="">All batches</option>
            {batches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
          </select>)} />

      <div className="flex gap-1 border-b border-border px-6">
        {[["events", "Decision Timeline", ScrollText], ["models", "Model Invocations", Cpu], ["diff", "Rerun Diff", GitCompareArrows]].map(([k, l, Icon]) => (
          <button key={k} data-testid={`audit-tab-${k}`} onClick={() => setTab(k)}
            className={`flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-xs font-semibold ${tab === k ? "border-brand text-brand" : "border-transparent text-muted-foreground hover:text-foreground"}`}>
            <Icon size={14} /> {l}
          </button>
        ))}
      </div>

      <div className="p-6">
        {tab === "events" && (
          <div className="relative pl-6">
            <div className="absolute left-[7px] top-1 bottom-1 w-px bg-border" />
            {events.length === 0 && <p className="text-sm text-muted-foreground">No audit events.</p>}
            {events.map((e) => (
              <div key={e.id} data-testid={`audit-event-${e.id}`} className="relative mb-3 animate-fade-up">
                <div className="absolute -left-[22px] top-1 h-3.5 w-3.5 rounded-full border-2 border-background bg-brand" />
                <div className="rounded-md border border-border bg-card p-3">
                  <div className="flex items-center justify-between">
                    <span className={`text-xs font-bold uppercase tracking-wide ${ACTION_COLOR[e.action] || "text-foreground"}`}>{e.action.replace(/_/g, " ")}</span>
                    <span className="font-mono text-[10px] text-muted-foreground">{fmtDate(e.created_at)}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                    <GitBranch size={11} /> {e.entity} · <span className="font-mono">{(e.entity_id || "").slice(0, 12)}</span> · by <b className="text-foreground">{e.actor}</b> ({e.role})
                  </div>
                  {e.details && Object.keys(e.details).length > 0 && (
                    <pre className="mt-1.5 overflow-x-auto rounded bg-secondary px-2 py-1 font-mono text-[10px] text-muted-foreground">{JSON.stringify(e.details)}</pre>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {tab === "models" && (
          <div className="overflow-hidden rounded-md border border-border">
            <table className="recon-table w-full text-left text-xs">
              <thead className="bg-secondary text-muted-foreground"><tr><th>Agent</th><th>Model</th><th>Prompt</th><th>Output</th><th>When</th></tr></thead>
              <tbody className="divide-y divide-border">
                {invs.length === 0 && <tr><td colSpan={5} className="py-10 text-center text-muted-foreground">No AI invocations yet — run an exception analysis or ask the Copilot.</td></tr>}
                {invs.map((v) => (
                  <tr key={v.id} data-testid={`inv-${v.id}`}>
                    <td><span className="rounded bg-brand/10 px-1.5 py-0.5 text-[10px] font-bold uppercase text-brand">{v.agent}</span></td>
                    <td className="font-mono text-[10px]">{v.model}</td>
                    <td className="max-w-[200px] truncate text-muted-foreground">{v.prompt_preview}</td>
                    <td className="max-w-[280px] truncate text-muted-foreground">{v.output_preview}</td>
                    <td className="font-mono text-[10px] text-muted-foreground">{fmtDate(v.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {tab === "diff" && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-end gap-3 rounded-md border border-border bg-secondary/30 p-3">
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Base run</label>
                <select data-testid="diff-base-select" value={base} onChange={(e) => setBase(e.target.value)}
                  className="mt-1 block h-9 w-64 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand">
                  {batches.map((b) => <option key={b.id} value={b.id}>{b.name} · v{b.policy_version}</option>)}
                </select>
              </div>
              <ArrowRight size={16} className="mb-2 text-muted-foreground" />
              <div>
                <label className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Compare run</label>
                <select data-testid="diff-compare-select" value={compare} onChange={(e) => setCompare(e.target.value)}
                  className="mt-1 block h-9 w-64 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand">
                  {batches.map((b) => <option key={b.id} value={b.id}>{b.name} · v{b.policy_version}</option>)}
                </select>
              </div>
            </div>

            {base === compare ? (
              <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">Select two different runs to compare.</div>
            ) : !diff ? (
              <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">Loading diff…</div>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
                  {[["Total changes", diff.total_changes, "text-foreground"], ["Resolved", diff.resolved, "text-success"],
                    ["Regressed", diff.regressed, "text-destructive"], ["Other changes", diff.changed, "text-warning"]].map(([l, v, c]) => (
                    <div key={l} className="rounded-md border border-border bg-card p-3">
                      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{l}</div>
                      <div className={`mt-1 font-mono text-xl font-bold ${c}`}>{v}</div>
                    </div>
                  ))}
                </div>

                <div className="grid grid-cols-2 gap-3 text-xs">
                  {[diff.base, diff.compare].map((b, i) => (
                    <div key={i} className="rounded-md border border-border bg-card p-3">
                      <div className="font-semibold">{i === 0 ? "Base" : "Compare"}: {b.name}</div>
                      <div className="mt-1 flex flex-wrap gap-3 font-mono text-[11px] text-muted-foreground">
                        <span>policy v{b.policy_version}</span>
                        <span>incl. {b.metrics.inclusive_match_rate}%</span>
                        <span className="text-destructive">VaR {rupeesCompact(b.metrics.value_at_risk_paise)}</span>
                      </div>
                    </div>
                  ))}
                </div>

                <div className="overflow-hidden rounded-md border border-border">
                  <div className="border-b border-border bg-secondary px-4 py-2 text-xs font-bold uppercase tracking-wide">Per-Settlement Changes</div>
                  <table className="recon-table w-full text-left text-xs">
                    <thead className="bg-card text-muted-foreground"><tr><th>Settlement / UTR</th><th>Change</th><th>Base state</th><th>Compare state</th><th>Base pass / taxonomy</th><th>Compare pass / taxonomy</th></tr></thead>
                    <tbody className="divide-y divide-border">
                      {diff.changes.length === 0 && <tr><td colSpan={6} className="py-10 text-center text-muted-foreground">No differences — reruns are identical (idempotent).</td></tr>}
                      {diff.changes.map((c) => (
                        <tr key={c.key} data-testid={`diff-row-${c.key}`}>
                          <td className="font-mono font-semibold">{c.key}</td>
                          <td><span className={`rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase ${KIND_STYLE[c.kind]}`}>{c.kind}</span></td>
                          <td className="font-mono">{c.base_state}</td>
                          <td className="font-mono">{c.compare_state}</td>
                          <td className="font-mono text-muted-foreground">{c.base_pass ? `P${c.base_pass}` : c.base_taxonomy || "—"}</td>
                          <td className="font-mono text-muted-foreground">{c.compare_pass ? `P${c.compare_pass}` : c.compare_taxonomy || "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
