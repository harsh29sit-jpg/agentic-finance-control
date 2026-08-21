import { useState } from "react";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { rupees, fmtDate, TAXONOMY_LABEL } from "@/lib/format";
import { StatusPill } from "@/components/StatusPill";
import { Button } from "@/components/ui/button";
import { CheckCircle2, XCircle, Sparkles, ArrowUpCircle, ShieldCheck } from "lucide-react";

const Field = ({ k, v, mono }) => (
  <div className="flex items-baseline justify-between gap-3 border-b border-border py-1.5 last:border-0">
    <span className="text-[11px] uppercase tracking-wide text-muted-foreground">{k}</span>
    <span className={mono ? "font-mono text-xs font-medium" : "text-xs font-medium text-right"}>{v}</span>
  </div>
);

const RecordBlock = ({ title, rec, color }) => {
  if (!rec) return (
    <div className="rounded border border-dashed border-border p-2.5 text-xs text-muted-foreground">
      {title}: <span className="font-semibold">absent</span>
    </div>
  );
  const list = Array.isArray(rec) ? rec : [rec];
  return (
    <div className="rounded border border-border">
      <div className={`border-b border-border px-2.5 py-1 text-[11px] font-bold uppercase tracking-wide ${color}`}>{title}</div>
      <div className="divide-y divide-border">
        {list.map((r, i) => (
          <div key={i} className="px-2.5 py-1.5 text-[11px]">
            <div className="flex justify-between font-mono"><span>{r.external_id}</span><span className="font-semibold">{rupees(r.amount_paise)}</span></div>
            {r.utr && <div className="font-mono text-muted-foreground">UTR {r.utr}</div>}
            {r.narration && <div className="mt-0.5 text-muted-foreground truncate">{r.narration}</div>}
          </div>
        ))}
      </div>
    </div>
  );
};

// Generic right-side drawer used by Workbench (decision) and Exceptions (case)
export const EvidenceDrawer = ({ open, onClose, data, kind, onReview, onAnalyze, onApprove, canApprove, busy }) => {
  const [note, setNote] = useState("");
  if (!data) return null;
  const isCase = kind === "exception";

  return (
    <Sheet open={open} onOpenChange={(o) => !o && onClose()}>
      <SheetContent className="w-full overflow-y-auto p-0 sm:max-w-md" data-testid="evidence-drawer">
        <SheetHeader className="border-b border-border px-4 py-3">
          <SheetTitle className="flex items-center justify-between text-sm">
            <span className="font-mono">{isCase ? (data.settlement_id || data.utr || "Exception") : data.settlement_id}</span>
            <StatusPill status={data.status} />
          </SheetTitle>
        </SheetHeader>

        <div className="space-y-4 p-4">
          {isCase ? (
            <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3">
              <div className="text-[11px] font-bold uppercase tracking-wide text-destructive">
                {TAXONOMY_LABEL[data.taxonomy] || data.taxonomy}
              </div>
              <p className="mt-1 text-xs">{data.reason}</p>
              <div className="mt-2 font-mono text-sm font-bold text-destructive">Value at risk: {rupees(data.value_at_risk_paise)}</div>
            </div>
          ) : (
            <div className="rounded-md border border-border bg-secondary/50 p-3">
              <Field k="Pass" v={`Pass ${data.pass_number}`} />
              <Field k="Confidence" v={`${(data.confidence * 100).toFixed(0)}%`} />
              <Field k="Settlement amt" v={rupees(data.settlement_amount_paise)} mono />
              <Field k="Bank amt" v={rupees(data.bank_amount_paise)} mono />
              <Field k="Delta" v={`${data.tolerance_paise} paise`} mono />
              <Field k="Date gap" v={`${data.date_gap_days}d`} />
              <div className="mt-2 text-xs text-muted-foreground">{data.note}</div>
              {data.aggregation_note && <div className="mt-1 text-[11px] text-brand">Σ {data.aggregation_note}</div>}
            </div>
          )}

          {/* Side-by-side ledgers */}
          <div>
            <div className="mb-2 text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Ledger evidence</div>
            <div className="space-y-2">
              <RecordBlock title="Source A · Payments" rec={data.source_a} color="text-brand" />
              <RecordBlock title="Source B · Settlement" rec={data.source_b} color="text-accent dark:text-brand" />
              <RecordBlock title="Source C · Bank" rec={data.source_c} color="text-success" />
            </div>
          </div>

          {/* AI evidence */}
          {isCase && (
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <div className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Agentic analysis</div>
                <Button size="sm" variant="outline" className="h-7 gap-1 text-xs" disabled={busy}
                  data-testid="run-ai-analysis-btn" onClick={() => onAnalyze(data)}>
                  <Sparkles size={13} /> {data.ai_analyzed ? "Re-run AI" : "Run AI Analysis"}
                </Button>
              </div>
              <div className="rounded border border-border p-2.5 text-xs">
                <div className="flex items-center gap-2">
                  <span className="text-[10px] uppercase text-muted-foreground">{data.triage?.source === "ai" ? "AI triage" : "Deterministic triage"}</span>
                  <StatusPill status={data.triage?.severity === "high" ? "open" : data.triage?.severity === "low" ? "matched" : "pending_review"} label={data.triage?.severity} />
                </div>
                <p className="mt-1.5 font-medium">{data.triage?.suggested_action}</p>
                {data.triage?.rationale && <p className="mt-1 text-muted-foreground">{data.triage.rationale}</p>}
              </div>
              {data.reviewer_explanation && (
                <div className="rounded border border-brand/30 bg-brand/5 p-2.5 text-xs">
                  <div className="text-[10px] font-bold uppercase text-brand">Reviewer Copilot</div>
                  <p className="mt-1">{data.reviewer_explanation}</p>
                </div>
              )}
              {data.narration_analysis && (
                <div className="rounded border border-border p-2.5 text-xs">
                  <div className="text-[10px] font-bold uppercase text-muted-foreground">Narration analysis</div>
                  <p className="mt-1">{data.narration_analysis.explanation}</p>
                  {data.narration_analysis.evidence_substring && (
                    <p className="mt-1 font-mono text-[11px] text-success">evidence: "{data.narration_analysis.evidence_substring}"</p>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Review actions */}
          {onReview && (
            <div className="space-y-2 border-t border-border pt-3">
              <input
                data-testid="review-note-input"
                value={note} onChange={(e) => setNote(e.target.value)}
                placeholder="Add a decision note (audited)…"
                className="w-full rounded border border-input bg-background px-2.5 py-1.5 text-xs outline-none focus:ring-2 focus:ring-brand"
              />
              <div className="grid grid-cols-2 gap-2">
                {isCase ? (
                  <>
                    <Button size="sm" className="h-8 gap-1 bg-success text-success-foreground hover:bg-success/90" data-testid="resolve-btn"
                      disabled={busy} onClick={() => onReview("resolve", note)}><CheckCircle2 size={14} /> Resolve</Button>
                    <Button size="sm" variant="outline" className="h-8 gap-1" data-testid="escalate-btn"
                      disabled={busy} onClick={() => onReview("escalate", note)}><ArrowUpCircle size={14} /> Escalate</Button>
                    <Button size="sm" variant="outline" className="h-8 gap-1 border-warning text-warning" data-testid="override-btn"
                      disabled={busy} onClick={() => onReview("override", note)}><ShieldCheck size={14} /> Override</Button>
                    <Button size="sm" variant="outline" className="h-8 gap-1 border-destructive text-destructive" data-testid="reject-btn"
                      disabled={busy} onClick={() => onReview("reject", note)}><XCircle size={14} /> Reject</Button>
                  </>
                ) : (
                  <>
                    <Button size="sm" className="h-8 gap-1 bg-success text-success-foreground hover:bg-success/90" data-testid="approve-btn"
                      disabled={busy} onClick={() => onReview("approve", note)}><CheckCircle2 size={14} /> Approve</Button>
                    <Button size="sm" variant="outline" className="h-8 gap-1" data-testid="wb-escalate-btn"
                      disabled={busy} onClick={() => onReview("escalate", note)}><ArrowUpCircle size={14} /> Escalate</Button>
                    <Button size="sm" variant="outline" className="col-span-2 h-8 gap-1 border-destructive text-destructive" data-testid="wb-reject-btn"
                      disabled={busy} onClick={() => onReview("reject", note)}><XCircle size={14} /> Route to Exception</Button>
                  </>
                )}
              </div>
              {isCase && data.status === "pending_approval" && canApprove && (
                <div className="grid grid-cols-2 gap-2 border-t border-border pt-2">
                  <Button size="sm" className="h-8 bg-accent text-white" data-testid="approve-override-btn"
                    disabled={busy} onClick={() => onApprove(true, note)}>Approve Override</Button>
                  <Button size="sm" variant="outline" className="h-8 border-destructive text-destructive" data-testid="reject-override-btn"
                    disabled={busy} onClick={() => onApprove(false, note)}>Reject Override</Button>
                </div>
              )}
            </div>
          )}

          {data.review && (
            <div className="rounded border border-border p-2.5 text-[11px] text-muted-foreground">
              Last action <b>{data.review.action}</b> by {data.review.by} · {fmtDate(data.review.at)}
              {data.review.requires_approval && <span className="text-warning"> · awaiting checker</span>}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
};

export default EvidenceDrawer;
