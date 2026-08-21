import { useEffect, useState } from "react";
import api from "@/lib/api";
import PageHeader from "@/components/PageHeader";
import { Gauge, CheckCircle2, XCircle, Target, ShieldAlert } from "lucide-react";

const Bar = ({ value, ok }) => (
  <div className="mt-1 h-2 rounded-full bg-secondary">
    <div className={`h-2 rounded-full ${ok ? "bg-success" : "bg-destructive"}`} style={{ width: `${Math.min(100, value)}%` }} />
  </div>
);

const ScoreCard = ({ title, value, ok, icon: Icon, sub }) => (
  <div data-testid={`eval-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`} className={`rounded-md border p-4 ${ok ? "border-success/30" : "border-destructive/30 bg-destructive/5"}`}>
    <div className="flex items-center justify-between">
      <span className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"><Icon size={13} /> {title}</span>
      {ok ? <CheckCircle2 size={15} className="text-success" /> : <XCircle size={15} className="text-destructive" />}
    </div>
    <div className={`mt-1.5 font-mono text-2xl font-bold ${ok ? "text-foreground" : "text-destructive"}`}>{value}%</div>
    <Bar value={value} ok={ok} />
    {sub && <div className="mt-1.5 text-[10px] text-muted-foreground">{sub}</div>}
  </div>
);

export default function Evaluation() {
  const [batches, setBatches] = useState([]);
  const [batchId, setBatchId] = useState("");
  const [score, setScore] = useState(null);
  const [all, setAll] = useState([]);

  useEffect(() => {
    api.get("/batches").then(({ data }) => {
      const labelled = data.filter((b) => b.has_truth);
      setBatches(labelled);
      if (labelled[0]) setBatchId(labelled[0].id);
    });
    api.get("/benchmark").then(({ data }) => setAll(data)).catch(() => {});
  }, []);

  useEffect(() => { if (batchId) api.get(`/benchmark/${batchId}`).then(({ data }) => setScore(data)); }, [batchId]);

  return (
    <div>
      <PageHeader title="Evaluation Dashboard" subtitle="Benchmark engine output against a labelled ground-truth set · precision · recall · F1"
        actions={
          <select data-testid="eval-batch-select" value={batchId} onChange={(e) => setBatchId(e.target.value)}
            className="h-9 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand">
            {batches.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
          </select>} />

      <div className="space-y-5 p-6">
        {!score ? (
          <div className="rounded-md border border-dashed border-border p-12 text-center text-sm text-muted-foreground">Select a labelled batch to view its evaluation.</div>
        ) : !score.has_truth ? (
          <div className="rounded-md border border-dashed border-border p-12 text-center text-sm text-muted-foreground">{score.message}</div>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <ScoreCard title="Auto-Match Precision" value={score.auto_match_precision} ok={score.gates.precision_ok} icon={Target} sub={`target ≥ 99% · ${score.false_positive} false matches`} />
              <ScoreCard title="Match Recall" value={score.match_recall} icon={Gauge} ok={score.match_recall >= 99} sub={`${score.true_positive}/${score.true_positive + score.false_negative} true matches`} />
              <ScoreCard title="Exception Recall" value={score.exception_recall} ok={score.gates.exception_recall_ok} icon={ShieldAlert} sub={`${score.true_exceptions_caught}/${score.true_exceptions_total} exceptions caught`} />
              <ScoreCard title="F1 Score" value={score.f1_score} ok={score.f1_score >= 99} icon={Target} sub="harmonic mean" />
            </div>

            {/* Confusion + gates */}
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <div className="rounded-md border border-border bg-card p-4">
                <h3 className="mb-3 text-sm font-bold">Confusion Matrix (Match classification)</h3>
                <div className="grid grid-cols-2 gap-2 text-center text-xs">
                  <div className="rounded border border-success/30 bg-success/5 p-3"><div className="font-mono text-2xl font-bold text-success">{score.true_positive}</div><div className="text-muted-foreground">True Positive</div></div>
                  <div className="rounded border border-destructive/30 bg-destructive/5 p-3"><div className="font-mono text-2xl font-bold text-destructive">{score.false_positive}</div><div className="text-muted-foreground">False Match (FP)</div></div>
                  <div className="rounded border border-warning/30 bg-warning/5 p-3"><div className="font-mono text-2xl font-bold text-warning">{score.false_negative}</div><div className="text-muted-foreground">Missed Match (FN)</div></div>
                  <div className="rounded border border-border p-3"><div className="font-mono text-2xl font-bold">{score.truth_size}</div><div className="text-muted-foreground">Labelled Records</div></div>
                </div>
                {score.false_matches.length > 0 && (
                  <div className="mt-3 rounded border border-destructive/40 bg-destructive/5 p-2 text-[11px] text-destructive">
                    <b>False matches (hard-cap breach):</b> <span className="font-mono">{score.false_matches.join(", ")}</span>
                  </div>
                )}
              </div>

              <div className="rounded-md border border-border bg-card p-4">
                <h3 className="mb-3 text-sm font-bold">Acceptance Gates</h3>
                <div className="space-y-2 text-xs">
                  {[["Auto-match precision ≥ 99%", score.gates.precision_ok, `${score.auto_match_precision}%`],
                    ["Exception recall = 100%", score.gates.exception_recall_ok, `${score.exception_recall}%`],
                    ["False-match rate < 0.5%", score.gates.false_match_ok, `${score.false_match_rate}%`]].map(([l, ok, v]) => (
                    <div key={l} className={`flex items-center justify-between rounded border px-3 py-2 ${ok ? "border-success/30 bg-success/5" : "border-destructive/30 bg-destructive/5"}`}>
                      <span>{l}</span>
                      <span className="flex items-center gap-2 font-mono">{v} {ok ? <CheckCircle2 size={14} className="text-success" /> : <XCircle size={14} className="text-destructive" />}</span>
                    </div>
                  ))}
                </div>
                <p className="mt-3 text-[10px] text-muted-foreground">Policy version in effect: <span className="font-mono">v{score.policy_version}</span></p>
              </div>
            </div>

            {/* All batches trend */}
            {all.length > 1 && (
              <div className="overflow-hidden rounded-md border border-border">
                <div className="border-b border-border bg-secondary px-4 py-2 text-xs font-bold uppercase tracking-wide">Precision & Recall across labelled batches</div>
                <table className="recon-table w-full text-left text-xs">
                  <thead className="bg-card text-muted-foreground"><tr><th>Batch</th><th>Policy</th><th className="text-right">Precision</th><th className="text-right">Match Recall</th><th className="text-right">Exc. Recall</th><th className="text-right">F1</th><th className="text-right">False Matches</th></tr></thead>
                  <tbody className="divide-y divide-border">
                    {all.map((s) => (
                      <tr key={s.batch_id} data-testid={`eval-trend-${s.batch_id}`}>
                        <td className="font-semibold">{s.batch_name}</td>
                        <td className="font-mono text-muted-foreground">v{s.policy_version}</td>
                        <td className="text-right font-mono">{s.auto_match_precision}%</td>
                        <td className="text-right font-mono">{s.match_recall}%</td>
                        <td className="text-right font-mono">{s.exception_recall}%</td>
                        <td className="text-right font-mono">{s.f1_score}%</td>
                        <td className={`text-right font-mono ${s.false_positive > 0 ? "text-destructive" : "text-success"}`}>{s.false_positive}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
