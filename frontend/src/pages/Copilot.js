import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Sparkles, Paperclip, SendHorizonal, FileText, Wrench, Check, X,
  CornerDownLeft, Copy, Loader2, Quote,
} from "lucide-react";

const EXAMPLES = [
  "Show open exceptions with the highest value at risk",
  "Top merchants by value at risk",
  "Run the benchmark — precision and recall",
  "Why is settlement SETL_1001 failing?",
];

const KIND_LABEL = {
  bank_statement: "bank statement",
  razorpay_settlements: "rzp settlements",
  razorpay_payments: "rzp payments",
  ledger: "ledger",
  unreadable: "unreadable",
};

export default function Copilot() {
  const navigate = useNavigate();
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [files, setFiles] = useState([]);
  const [busy, setBusy] = useState(false);
  const [provider, setProvider] = useState(null);
  const scrollRef = useRef();
  const fileRef = useRef();

  useEffect(() => {
    api.get("/agents/metrics")
      .then(({ data }) => setProvider(data.provider))
      .catch(() => {});
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, busy]);

  const addFiles = (e) => {
    const picked = Array.from(e.target.files || []);
    setFiles((cur) => [...cur, ...picked].slice(0, 3));
    if (fileRef.current) fileRef.current.value = "";
  };

  const send = async (preset) => {
    const q = (preset ?? input).trim();
    if (!q || q.length < 3 || busy) return;
    setBusy(true);
    setInput("");
    const sentFiles = files;
    setFiles([]);
    setMessages((m) => [...m, { role: "user", text: q, files: sentFiles }]);
    try {
      const fd = new FormData();
      fd.append("question", q);
      sentFiles.forEach((f) => fd.append("files", f));
      const { data } = await api.post("/copilot/agent", fd,
        { headers: { "Content-Type": "multipart/form-data" }, timeout: 120000 });
      setMessages((m) => [...m, { role: "assistant", data }]);
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", error:
        err.response?.data?.detail || "Agent run failed" }]);
    } finally {
      setBusy(false);
    }
  };

  const cite = (ref) => {
    navigator.clipboard?.writeText(ref).catch(() => {});
    navigate(`/workbench`);
  };

  return (
    <div className="flex h-[calc(100vh-3rem)]">
      {/* ---------------- thread ---------------- */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* console header */}
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-border bg-card px-5">
          <div className="flex items-center gap-2">
            <Sparkles size={15} className="text-brand" />
            <span className="text-sm font-bold">Agentic Finance Copilot</span>
            <span className="rounded bg-brand/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wide text-brand">multi-agent</span>
          </div>
          <div className="flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-muted-foreground">
            <span className={cn("h-1.5 w-1.5 rounded-full", busy ? "bg-warning animate-pulse" : "bg-success")} />
            {provider ? `provider: ${provider}` : "read-only tools"}
          </div>
        </div>

        {/* messages */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto px-5 py-4">
          {messages.length === 0 && (
            <div className="mx-auto mt-10 max-w-xl text-center">
              <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-lg bg-gradient-to-br from-[#0d94fb] to-[#0768b3] shadow-[0_4px_16px_rgba(13,148,251,0.3)]">
                <Sparkles size={20} className="text-white" />
              </div>
              <h3 className="mt-3 text-base font-bold">Ask anything about your reconciled data</h3>
              <p className="mt-1 text-xs text-muted-foreground">
                The agent plans read-only tool calls, executes them against live batches and
                synthesizes a grounded answer with citations. Attach a bank statement and ask it to reconcile.
              </p>
              <div className="mt-5 grid grid-cols-1 gap-2 sm:grid-cols-2">
                {EXAMPLES.map((ex) => (
                  <button key={ex} onClick={() => send(ex)}
                    className="card-surface rounded-md border border-border bg-card px-3 py-2.5 text-left text-xs text-muted-foreground transition-colors hover:border-brand/50 hover:text-foreground">
                    <CornerDownLeft size={12} className="mr-1.5 inline text-brand" />{ex}
                  </button>
                ))}
              </div>
              <p className="mt-4 text-[10px] text-muted-foreground">
                Attachments: bank-statement CSV/XLSX → reconcile preview · Razorpay reports → parsed summary
              </p>
            </div>
          )}

          {messages.map((msg, i) =>
            msg.role === "user"
              ? <UserRow key={i} msg={msg} />
              : <AssistantRow key={i} msg={msg} onCite={cite} />)}
        </div>

        {/* composer */}
        <div className="shrink-0 border-t border-border bg-card px-5 py-3">
          {files.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-1.5">
              {files.map((f, i) => (
                <span key={i} className="inline-flex items-center gap-1 rounded border border-border bg-secondary px-2 py-0.5 text-[11px]">
                  <FileText size={11} className="text-brand" />{f.name}
                  <button onClick={() => setFiles(files.filter((_, j) => j !== i))}
                    className="text-muted-foreground hover:text-destructive">×</button>
                </span>
              ))}
            </div>
          )}
          <div className="flex items-end gap-2">
            <input ref={fileRef} type="file" multiple accept=".csv,.xlsx,.json" onChange={addFiles} className="hidden" data-testid="copilot-file-input" />
            <button data-testid="copilot-attach" onClick={() => fileRef.current?.click()} disabled={busy}
              title="Attach up to 3 files (CSV/XLSX)"
              className="flex h-9 w-9 shrink-0 items-center justify-center rounded border border-border text-muted-foreground transition-colors hover:border-brand hover:text-brand disabled:opacity-50">
              <Paperclip size={15} />
            </button>
            <textarea
              data-testid="copilot-input"
              value={input}
              rows={1}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
              placeholder="Ask the agent — e.g. 'reconcile the attached statement' or 'why are exceptions spiking?'"
              className="max-h-32 min-h-[36px] flex-1 resize-none rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand"
            />
            <button data-testid="copilot-send" onClick={() => send()} disabled={busy || input.trim().length < 3}
              className="flex h-9 items-center gap-1.5 rounded-md bg-brand px-3.5 text-sm font-semibold text-white transition-opacity hover:bg-brand/90 disabled:opacity-40">
              {busy ? <Loader2 size={14} className="animate-spin" /> : <SendHorizonal size={14} />}
              Run
            </button>
          </div>
        </div>
      </div>

      {/* ---------------- run trace panel ---------------- */}
      <aside className="hidden w-[330px] shrink-0 flex-col overflow-y-auto border-l border-border bg-card xl:flex">
        <TracePanel messages={messages} busy={busy} />
      </aside>
    </div>
  );
}

/* ------------------------------------------------------------------ rows */
const UserRow = ({ msg }) => (
  <div className="mb-4 flex justify-end">
    <div className="max-w-[75%] rounded-md rounded-br-sm border border-border bg-secondary px-3.5 py-2.5">
      {!!msg.files?.length && (
        <div className="mb-1.5 flex flex-wrap gap-1">
          {msg.files.map((f, i) => (
            <span key={i} className="inline-flex items-center gap-1 rounded bg-background px-1.5 py-0.5 text-[10px] font-mono text-muted-foreground">
              <FileText size={10} className="text-brand" />{f.name}
            </span>
          ))}
        </div>
      )}
      <p className="whitespace-pre-wrap text-sm">{msg.text}</p>
    </div>
  </div>
);

const AssistantRow = ({ msg, onCite }) => {
  if (msg.error) {
    return (
      <div className="mb-4 max-w-[85%] rounded-md border border-destructive/30 bg-destructive/5 px-3.5 py-2.5 text-sm text-destructive">
        {msg.error}
      </div>
    );
  }
  const d = msg.data;
  return (
    <div className="card-surface mb-4 max-w-[92%] rounded-md rounded-bl-sm border border-border bg-card" data-testid="assistant-msg">
      {/* plan strip */}
      <div className="flex flex-wrap items-center gap-1.5 border-b border-border px-3.5 py-2">
        <Wrench size={11} className="text-muted-foreground" />
        {(d.plan || []).map((p, i) => (
          <span key={i} title={`${p.tool}${p.error ? ` — ${p.error}` : ""}`}
            className={cn("inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10px]",
              p.ok ? (p.state_changed ? "bg-warning/10 text-warning" : "bg-success/10 text-success")
                   : "bg-destructive/10 text-destructive")}>
            {p.state_changed ? <Zap size={9} /> :
              p.ok ? <Check size={9} /> : <X size={9} />}{p.tool}{p.ms != null && <span className="opacity-60">{p.ms}ms</span>}
          </span>
        ))}
        {!d.plan?.length && <span className="text-[10px] text-muted-foreground">no tool calls needed</span>}
        <span className="ml-auto font-mono text-[10px] text-muted-foreground">{d.latency_ms}ms</span>
      </div>

      <div className="px-3.5 py-3">
        <p className="whitespace-pre-wrap text-sm leading-relaxed">{d.answer}</p>

        {!!d.cited_records?.length && (
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            <Quote size={11} className="text-muted-foreground" />
            {d.cited_records.map((c, i) => (
              <button key={i} onClick={() => onCite(c)} title="Copy reference · open workbench"
                className="rounded border border-brand/30 bg-brand/5 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-brand transition-colors hover:bg-brand/10">
                {c}
              </button>
            ))}
          </div>
        )}

        {!!d.failed_checks?.length && (
          <ul className="mt-2 space-y-0.5 text-[11px] text-warning">
            {d.failed_checks.map((c, i) => <li key={i}>⚠ {c}</li>)}
          </ul>
        )}
        {d.suggested_next_action && (
          <p className="mt-2 text-[11px] text-muted-foreground">
            <span className="font-semibold text-foreground">Next:</span> {d.suggested_next_action}
          </p>
        )}
        {!!d.attachments?.length && (
          <div className="mt-2 flex flex-wrap gap-1">
            {d.attachments.map((a, i) => (
              <span key={i} className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">
                {a.name} · {KIND_LABEL[a.kind] || a.kind} · {a.rows} rows
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
};

/* ------------------------------------------------------- trace side panel */
const TracePanel = ({ messages, busy }) => {
  const last = [...messages].reverse().find((m) => m.role === "assistant" && m.data);
  return (
    <div className="p-4">
      <h4 className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Last Run Trace</h4>
      {busy && (
        <div className="mt-3 flex items-center gap-2 rounded border border-warning/40 bg-warning/5 px-2.5 py-2 text-xs text-warning">
          <Loader2 size={12} className="animate-spin" /> planning & executing…
        </div>
      )}
      {!last && !busy && (
        <p className="mt-3 text-xs text-muted-foreground">Run a request to see the agent's plan, tool timings and grounding checks here.</p>
      )}
      {last?.data && (
        <div className="mt-3 space-y-2">
          {(last.data.plan || []).map((p, i) => (
            <div key={i} className="card-surface rounded border border-border p-2.5">
              <div className="flex items-center justify-between">
                <span className="font-mono text-[11px] font-semibold text-foreground">{p.tool}</span>
                <span className={cn("font-mono text-[10px]", p.ok ? "text-success" : "text-destructive")}>
                  {p.ok ? `${p.ms ?? "?"}ms` : "failed"}
                </span>
              </div>
              {p.why && <p className="mt-0.5 text-[10px] text-muted-foreground">{p.why}</p>}
              {p.error && <p className="mt-0.5 text-[10px] text-destructive">{p.error}</p>}
            </div>
          ))}
          {!!last.data.attachments?.length && (
            <div className="card-surface rounded border border-border p-2.5">
              <p className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">Attachments</p>
              {last.data.attachments.map((a, i) => (
                <p key={i} className="mt-1 font-mono text-[10px]">{a.name} — {KIND_LABEL[a.kind] || a.kind} ({a.rows})</p>
              ))}
            </div>
          )}
          <div className="flex items-center justify-between px-0.5 text-[10px] text-muted-foreground">
            <span>mode: {last.data.mode}</span>
            <span>provider: {last.data.provider}</span>
          </div>
        </div>
      )}
    </div>
  );
};
