import { useEffect, useState } from "react";
import api from "@/lib/api";
import PageHeader from "@/components/PageHeader";
import { fmtDate } from "@/lib/format";
import { ScrollText, Cpu, GitBranch } from "lucide-react";

const ACTION_COLOR = {
  auto_post: "text-success", batch_created: "text-brand", ai_analyze: "text-brand",
  copilot_query: "text-brand", policy_updated: "text-warning",
};

export default function Audit() {
  const [batches, setBatches] = useState([]);
  const [batchId, setBatchId] = useState("");
  const [tab, setTab] = useState("events");
  const [events, setEvents] = useState([]);
  const [invs, setInvs] = useState([]);

  useEffect(() => { api.get("/batches").then(({ data }) => { setBatches(data); if (data[0]) setBatchId(data[0].id); }); }, []);
  useEffect(() => {
    const q = batchId ? `?batch_id=${batchId}` : "";
    api.get(`/audit${q}`).then(({ data }) => setEvents(data));
    api.get(`/model-invocations${q}`).then(({ data }) => setInvs(data));
  }, [batchId]);

  return (
    <div>
      <PageHeader title="Audit & Controls Console" subtitle="Append-only operational history · model invocation logs"
        actions={<select data-testid="audit-batch-select" value={batchId} onChange={(e) => setBatchId(e.target.value)}
          className="h-9 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand">
          <option value="">All batches</option>
          {batches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
        </select>} />

      <div className="flex gap-1 border-b border-border px-6">
        {[["events", "Decision Timeline", ScrollText], ["models", "Model Invocations", Cpu]].map(([k, l, Icon]) => (
          <button key={k} data-testid={`audit-tab-${k}`} onClick={() => setTab(k)}
            className={`flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-xs font-semibold ${tab === k ? "border-brand text-brand" : "border-transparent text-muted-foreground hover:text-foreground"}`}>
            <Icon size={14} /> {l}
          </button>
        ))}
      </div>

      <div className="p-6">
        {tab === "events" ? (
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
        ) : (
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
      </div>
    </div>
  );
}
