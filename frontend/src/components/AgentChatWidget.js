import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import api from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  Sparkles, X, Minus, Paperclip, SendHorizonal, FileText,
  Loader2, Zap, Quote,
} from "lucide-react";

const KIND_LABEL = {
  bank_statement: "bank stmt", razorpay_settlements: "rzp stl",
  razorpay_payments: "rzp pay", ledger: "ledger", unreadable: "?",
};

export default function AgentChatWidget() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [files, setFiles] = useState([]);
  const [busy, setBusy] = useState(false);
  const [unread, setUnread] = useState(0);
  const scrollRef = useRef();
  const fileRef = useRef();

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, busy, open]);

  useEffect(() => {
    if (open) setUnread(0);
  }, [messages, open]);

  const toggle = () => { setOpen((o) => !o); setUnread(0); };

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
        { headers: { "Content-Type": "multipart/form-data" }, timeout: 180000 });
      setMessages((m) => [...m, { role: "assistant", data }]);
      if (!open) setUnread((u) => u + 1);
    } catch (err) {
      setMessages((m) => [...m, { role: "assistant", error:
        err.response?.status === 503
          ? "Agent LLM not configured — add ANTHROPIC_API_KEY to backend/.env and restart."
          : err.response?.data?.detail || "Agent run failed" }]);
    } finally { setBusy(false); }
  };

  const cite = (ref) => {
    navigator.clipboard?.writeText(ref).catch(() => {});
    navigate("/workbench");
  };

  return (
    <>
      {/* floating action button */}
      <button data-testid="agent-fab" onClick={toggle}
        className="fixed bottom-5 right-5 z-50 flex h-[52px] w-[52px] items-center justify-center rounded-full bg-gradient-to-br from-[#0d94fb] to-[#0768b3] text-white shadow-[0_8px_24px_rgba(13,148,251,0.4)] transition-transform hover:scale-105 active:scale-95">
        {open ? <X size={22} /> : <Sparkles size={22} />}
        {!open && !busy && (
          <span className="absolute right-0 top-0 h-3 w-3 rounded-full border-2 border-background bg-success" />
        )}
        {!open && unread > 0 && (
          <span className="absolute -right-1 -top-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-bold">
            {unread}
          </span>
        )}
        {busy && <Loader2 size={22} className="absolute animate-spin" />}
      </button>

      {/* popup panel */}
      {open && (
        <div data-testid="agent-panel"
          className="card-surface fixed bottom-[88px] right-5 z-50 flex h-[min(600px,calc(100vh-7rem))] w-[min(420px,calc(100vw-2.5rem))] animate-fade-up flex-col overflow-hidden rounded-xl border border-border bg-card shadow-2xl">
          {/* header */}
          <div className="flex shrink-0 items-center gap-2 bg-gradient-to-r from-[#012652] to-[#04315f] px-3.5 py-2.5 text-white">
            <div className="flex h-7 w-7 items-center justify-center rounded-md bg-white/10">
              <Sparkles size={14} className="text-[#5cb8ff]" />
            </div>
            <div className="flex-1 leading-tight">
              <div className="text-xs font-bold">Finance Agent</div>
              <div className="text-[9px] uppercase tracking-wider text-white/50">
                reads + acts · audited as you
              </div>
            </div>
            <button onClick={toggle} title="Minimize"
              className="rounded p-1 text-white/60 hover:bg-white/10 hover:text-white"><Minus size={14} /></button>
          </div>

          {/* messages */}
          <div ref={scrollRef} className="flex-1 overflow-y-auto bg-secondary/40 px-3 py-3">
            {messages.length === 0 && (
              <div className="mt-6 px-2 text-center">
                <p className="text-xs font-semibold">Ask me anything about your reconciliation data.</p>
                <p className="mt-1 text-[11px] text-muted-foreground">
                  I can investigate batches & exceptions, reconcile attached statements,
                  resolve cases, rerun batches, create schedules — every action audited.
                </p>
                <div className="mt-4 space-y-1.5 text-left">
                  {["Top exceptions by value at risk?",
                    "Resolve SETL_1035's exception with note 'duplicate confirmed'",
                    "Rerun the latest batch"].map((ex) => (
                    <button key={ex} onClick={() => send(ex)}
                      className="w-full rounded-md border border-border bg-card px-2.5 py-1.5 text-left text-[11px] text-muted-foreground transition-colors hover:border-brand/50 hover:text-foreground">
                      {ex}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((msg, i) =>
              msg.role === "user"
                ? (
                  <div key={i} className="mb-2.5 flex justify-end">
                    <div className="max-w-[85%] rounded-lg rounded-br-sm border border-brand/25 bg-brand/10 px-2.5 py-1.5">
                      {!!msg.files?.length && (
                        <div className="mb-1 flex flex-wrap gap-1">
                          {msg.files.map((f, j) => (
                            <span key={j} className="inline-flex items-center gap-0.5 rounded bg-card px-1 py-0.5 font-mono text-[9px] text-muted-foreground">
                              <FileText size={8} className="text-brand" />{f.name}
                            </span>
                          ))}
                        </div>
                      )}
                      <p className="whitespace-pre-wrap text-xs">{msg.text}</p>
                    </div>
                  </div>
                )
                : (
                  <div key={i} className="mb-2.5 mr-4 rounded-lg border border-border bg-card" data-testid="widget-assistant-msg">
                    {!!msg.error ? (
                      <p className="px-2.5 py-2 text-[11px] text-destructive">{msg.error}</p>
                    ) : (
                      <>
                        <div className="flex flex-wrap items-center gap-1 border-b border-border px-2 py-1.5">
                          {(msg.data.plan || []).map((p, j) => (
                            <span key={j} title={p.error || `${p.tool} ${p.ms ?? ""}ms`}
                              className={cn("inline-flex items-center gap-0.5 rounded px-1 py-px font-mono text-[9px]",
                                p.ok ? (p.state_changed ? "bg-warning/10 text-warning"
                                       : "bg-success/10 text-success")
                                      : "bg-destructive/10 text-destructive")}>
                              {p.state_changed && <Zap size={8} />}{p.tool}
                            </span>
                          ))}
                        </div>
                        <div className="px-2.5 py-2">
                          <p className="whitespace-pre-wrap text-xs leading-relaxed">{msg.data.answer}</p>
                          {!!msg.data.cited_records?.length && (
                            <div className="mt-1.5 flex flex-wrap gap-1">
                              {msg.data.cited_records.map((c, j) => (
                                <button key={j} onClick={() => cite(c)}
                                  className="rounded border border-brand/30 bg-brand/5 px-1 py-px font-mono text-[9px] font-semibold text-brand hover:bg-brand/10">
                                  {c}
                                </button>
                              ))}
                            </div>
                          )}
                          {!!msg.data.failed_checks?.length && (
                            <ul className="mt-1 space-y-0.5 text-[10px] text-warning">
                              {msg.data.failed_checks.map((c, j) => <li key={j}>⚠ {c}</li>)}
                            </ul>
                          )}
                          {msg.data.suggested_next_action && (
                            <p className="mt-1 text-[10px] text-muted-foreground">
                              → {msg.data.suggested_next_action}
                            </p>
                          )}
                          {!!msg.data.attachments?.length && (
                            <div className="mt-1 flex flex-wrap gap-1">
                              {msg.data.attachments.map((a, j) => (
                                <span key={j} className="rounded bg-secondary px-1 py-px text-[9px] text-muted-foreground">
                                  {a.name} · {KIND_LABEL[a.kind]}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </div>
                ))}

            {busy && (
              <div className="mb-2.5 inline-flex items-center gap-1.5 rounded-lg border border-border bg-card px-2.5 py-1.5 text-[11px] text-muted-foreground">
                <Loader2 size={11} className="animate-spin text-brand" />
                agent working…
              </div>
            )}
          </div>

          {/* composer */}
          <div className="shrink-0 border-t border-border bg-card px-2.5 py-2">
            {files.length > 0 && (
              <div className="mb-1 flex flex-wrap gap-1">
                {files.map((f, i) => (
                  <span key={i} className="inline-flex items-center gap-0.5 rounded border border-border bg-secondary px-1.5 py-0.5 text-[10px]">
                    <FileText size={9} className="text-brand" />{f.name}
                    <button onClick={() => setFiles(files.filter((_, j) => j !== i))}
                      className="text-muted-foreground hover:text-destructive">×</button>
                  </span>
                ))}
              </div>
            )}
            <div className="flex items-end gap-1.5">
              <input ref={fileRef} type="file" multiple accept=".csv,.xlsx,.json"
                onChange={(e) => {
                  setFiles([...files, ...Array.from(e.target.files || [])].slice(0, 3));
                  if (fileRef.current) fileRef.current.value = "";
                }} className="hidden" />
              <button onClick={() => fileRef.current?.click()} disabled={busy}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-border text-muted-foreground hover:border-brand hover:text-brand disabled:opacity-40">
                <Paperclip size={13} />
              </button>
              <textarea value={input} rows={1}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
                placeholder="Ask or instruct…"
                className="max-h-24 min-h-[32px] flex-1 resize-none rounded-md border border-input bg-background px-2.5 py-1.5 text-xs outline-none focus:ring-2 focus:ring-brand" />
              <button data-testid="widget-send" onClick={() => send()} disabled={busy || input.trim().length < 3}
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-brand text-white hover:bg-brand/90 disabled:opacity-40">
                <SendHorizonal size={13} />
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
