import { useEffect, useState, useRef } from "react";
import api from "@/lib/api";
import PageHeader from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Sparkles, Send, FileText, AlertOctagon, ArrowRight, Quote } from "lucide-react";

const SUGGESTED = [
  "Which merchant has the highest value at risk and why?",
  "Summarize all missing-in-bank exceptions for this batch.",
  "What is the deterministic match rate and how many settlements auto-posted?",
  "List any duplicate bank credits and their UTRs.",
];

export default function Copilot() {
  const [batches, setBatches] = useState([]);
  const [batchId, setBatchId] = useState("");
  const [q, setQ] = useState("");
  const [thread, setThread] = useState([]);
  const [busy, setBusy] = useState(false);
  const endRef = useRef();

  useEffect(() => { api.get("/batches").then(({ data }) => { setBatches(data); if (data[0]) setBatchId(data[0].id); }); }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [thread]);

  const ask = async (question) => {
    const text = question || q;
    if (!text.trim()) return;
    setBusy(true); setQ("");
    setThread((t) => [...t, { role: "user", text }]);
    try {
      const { data } = await api.post("/copilot/ask", { question: text, batch_id: batchId });
      setThread((t) => [...t, { role: "assistant", ...data }]);
    } catch (e) { toast.error(e.response?.data?.detail || "Copilot unavailable"); }
    finally { setBusy(false); }
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader title="Finance Copilot" subtitle="Read-only, grounded Q&A over reconciled outputs · exact citations · cannot mutate outcomes"
        actions={<select data-testid="copilot-batch-select" value={batchId} onChange={(e) => setBatchId(e.target.value)}
          className="h-9 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand">
          {batches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
        </select>} />

      <div className="flex min-h-0 flex-1">
        {/* Conversation */}
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex-1 space-y-4 overflow-y-auto p-6">
            {thread.length === 0 && (
              <div className="mx-auto max-w-lg pt-8 text-center">
                <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-md bg-brand/10"><Sparkles className="text-brand" /></div>
                <h3 className="mt-3 text-sm font-bold">Grounded settlement Q&A</h3>
                <p className="mt-1 text-xs text-muted-foreground">Every answer cites exact settlement IDs, UTRs and rule failures from the selected batch. The copilot is strictly read-only.</p>
              </div>
            )}
            {thread.map((m, i) => m.role === "user" ? (
              <div key={i} className="flex justify-end">
                <div className="max-w-[75%] rounded-md rounded-tr-none bg-brand px-3 py-2 text-sm text-white">{m.text}</div>
              </div>
            ) : (
              <div key={i} data-testid={`copilot-answer-${i}`} className="max-w-[85%] animate-fade-up rounded-md border border-border bg-card">
                <div className="border-b border-border px-3 py-2 text-xs font-semibold">{m.answer}</div>
                <div className="space-y-2 p-3">
                  {m.cited_records?.length > 0 && (
                    <div>
                      <div className="mb-1 flex items-center gap-1 text-[10px] font-bold uppercase text-muted-foreground"><Quote size={11} /> Cited records</div>
                      {m.cited_records.map((c, j) => <div key={j} className="rounded border border-border bg-secondary/40 px-2 py-1 font-mono text-[10px]">{c}</div>)}
                    </div>
                  )}
                  {m.failed_checks?.length > 0 && (
                    <div>
                      <div className="mb-1 flex items-center gap-1 text-[10px] font-bold uppercase text-destructive"><AlertOctagon size={11} /> Failed checks</div>
                      {m.failed_checks.map((c, j) => <div key={j} className="text-[11px] text-destructive">• {c}</div>)}
                    </div>
                  )}
                  {m.suggested_next_action && (
                    <div className="flex items-start gap-1.5 rounded border border-brand/30 bg-brand/5 px-2 py-1.5 text-[11px]">
                      <ArrowRight size={12} className="mt-0.5 shrink-0 text-brand" /> <span>{m.suggested_next_action}</span>
                    </div>
                  )}
                </div>
              </div>
            ))}
            {busy && <div className="max-w-[85%] animate-pulse rounded-md border border-border bg-card px-3 py-2 text-xs text-muted-foreground">Copilot is grounding the answer…</div>}
            <div ref={endRef} />
          </div>

          <div className="border-t border-border p-4">
            <div className="flex gap-2">
              <input data-testid="copilot-input" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && ask()}
                placeholder="Ask about matches, exceptions, value at risk…"
                className="h-10 flex-1 rounded border border-input bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-brand" />
              <Button data-testid="copilot-send" onClick={() => ask()} disabled={busy} className="h-10 gap-1.5 bg-brand text-white hover:bg-brand/90"><Send size={15} /> Ask</Button>
            </div>
          </div>
        </div>

        {/* Suggested panel */}
        <div className="hidden w-72 shrink-0 border-l border-border p-4 lg:block">
          <div className="flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-muted-foreground"><FileText size={13} /> Suggested queries</div>
          <div className="mt-3 space-y-2">
            {SUGGESTED.map((s, i) => (
              <button key={i} data-testid={`suggested-${i}`} onClick={() => ask(s)} disabled={busy}
                className="w-full rounded border border-border px-3 py-2 text-left text-xs transition-colors hover:border-brand hover:bg-secondary">{s}</button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
