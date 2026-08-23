import { useEffect, useState } from "react";
import api from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import PageHeader from "@/components/PageHeader";
import { Button } from "@/components/ui/button";
import { fmtDate } from "@/lib/format";
import { toast } from "sonner";
import { ShieldCheck, Users, SlidersHorizontal, Clock, Play, Trash2, KeyRound } from "lucide-react";

const ROLE_CAPS = {
  admin: "Full access · policies · overrides",
  controller: "Reports · approve overrides (checker) · policies · schedules",
  compliance: "View all · audit console",
  analyst: "Run batches · workbench · review (maker)",
  support: "Read-only",
};

const ACTION_LABELS = {
  replay_latest_upload: "Replay latest upload",
  sandbox_seed: "Seed sandbox fixture",
};

export default function Admin() {
  const { user, meta } = useAuth();
  const [policies, setPolicies] = useState({ active: {}, versions: [] });
  const [pending, setPending] = useState([]);
  const [schedules, setSchedules] = useState([]);
  const [schedForm, setSchedForm] = useState({ name: "", cron: "0 6 * * *", action: "replay_latest_upload" });
  const [mfa, setMfa] = useState({ stage: "idle", secret: "", uri: "", code: "", codes: null, password: "" });
  const [form, setForm] = useState({ amount_tolerance_paise: 100, timing_lag_days: 1, auto_post_confidence: 0.95, note: "" });
  const canEdit = ["controller", "admin"].includes(user?.role);

  const load = () => {
    api.get("/policies").then(({ data }) => { setPolicies(data); if (data.active) setForm((f) => ({ ...f, ...data.active, note: "" })); });
    api.get("/review/pending").then(({ data }) => setPending(data)).catch(() => {});
    api.get("/schedules").then(({ data }) => setSchedules(data)).catch(() => {});
  };
  useEffect(() => { load(); }, []);

  const save = async () => {
    try {
      await api.post("/policies", {
        amount_tolerance_paise: Number(form.amount_tolerance_paise),
        timing_lag_days: Number(form.timing_lag_days),
        auto_post_confidence: Number(form.auto_post_confidence),
        note: form.note,
      });
      toast.success("New policy version published");
      load();
    } catch (e) { toast.error(e.response?.data?.detail || "Failed"); }
  };

  const createSchedule = async () => {
    try {
      await api.post("/schedules", { ...schedForm, enabled: true });
      toast.success(`Schedule "${schedForm.name}" created`);
      setSchedForm({ name: "", cron: "0 6 * * *", action: schedForm.action });
      load();
    } catch (e) { toast.error(e.response?.data?.detail || "Failed to create schedule"); }
  };

  const runSchedule = async (s) => {
    try {
      const { data } = await api.post(`/schedules/${s.id}/run-now`);
      if (data.schedule.last_status === "ok") toast.success(`Ran "${s.name}"`);
      else toast.error(`"${s.name}" failed — see details`);
      load();
    } catch (e) { toast.error(e.response?.status === 409 ? "Schedule already running" : e.response?.data?.detail || "Run failed"); }
  };

  const deleteSchedule = async (s) => {
    try { await api.delete(`/schedules/${s.id}`); toast.success("Schedule removed"); load(); }
    catch (e) { toast.error(e.response?.data?.detail || "Delete failed"); }
  };

  // ---- MFA enrollment ----
  const mfaSetup = async () => {
    try {
      const { data } = await api.post("/auth/mfa/setup");
      setMfa({ ...mfa, stage: "verify", secret: data.secret, uri: data.otpauth_uri });
    } catch (e) { toast.error(e.response?.data?.detail || "Setup failed"); }
  };

  const mfaEnable = async () => {
    try {
      const { data } = await api.post("/auth/mfa/enable", { code: mfa.code });
      setMfa({ ...mfa, stage: "codes", codes: data.recovery_codes });
      toast.success("MFA enabled — save your recovery codes now");
    } catch (e) { toast.error(e.response?.data?.detail || "Invalid code"); }
  };

  const mfaDisable = async () => {
    try {
      await api.post("/auth/mfa/disable", { password: mfa.password });
      setMfa({ stage: "idle", secret: "", uri: "", code: "", codes: null, password: "" });
      toast.success("MFA disabled");
    } catch (e) { toast.error(e.response?.data?.detail || "Password incorrect"); }
  };

  return (
    <div>
      <PageHeader title="Admin / Policy Center" subtitle="Matching policy versions · RBAC · pending checker approvals" />

      <div className="grid grid-cols-1 gap-5 p-6 lg:grid-cols-2">
        {/* Policy editor */}
        <div className="rounded-md border border-border bg-card p-4">
          <h3 className="flex items-center gap-1.5 text-sm font-bold"><SlidersHorizontal size={15} className="text-brand" /> Matching Policy</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">Active version <span className="font-mono font-semibold">v{policies.active?.version}</span> · applied on next batch run.</p>
          <div className="mt-4 space-y-3">
            {[["amount_tolerance_paise", "Amount tolerance (paise)", "Pass 2 tolerance band"],
              ["timing_lag_days", "Timing lag (days)", "Business-day settlement window"],
              ["auto_post_confidence", "Auto-post confidence", "Min confidence to auto-post"]].map(([k, l, h]) => (
              <div key={k}>
                <label className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{l}</label>
                <input data-testid={`policy-${k}`} type="number" step={k === "auto_post_confidence" ? "0.01" : "1"} value={form[k]} disabled={!canEdit}
                  onChange={(e) => setForm({ ...form, [k]: e.target.value })}
                  className="mt-1 w-full rounded border border-input bg-background px-2.5 py-1.5 font-mono text-sm outline-none focus:ring-2 focus:ring-brand disabled:opacity-60" />
                <p className="mt-0.5 text-[10px] text-muted-foreground">{h}</p>
              </div>
            ))}
            <input data-testid="policy-note" placeholder="Change note" value={form.note} disabled={!canEdit} onChange={(e) => setForm({ ...form, note: e.target.value })}
              className="w-full rounded border border-input bg-background px-2.5 py-1.5 text-xs outline-none focus:ring-2 focus:ring-brand disabled:opacity-60" />
            <Button data-testid="save-policy-btn" onClick={save} disabled={!canEdit} className="w-full bg-brand text-white hover:bg-brand/90">
              {canEdit ? "Publish New Version" : "Read-only (controller/admin)"}
            </Button>
          </div>

          <div className="mt-4 border-t border-border pt-3">
            <div className="text-[11px] font-bold uppercase tracking-wide text-muted-foreground">Version history</div>
            <div className="mt-2 space-y-1.5">
              {policies.versions.map((v) => (
                <div key={v.id} className="flex items-center justify-between rounded border border-border px-2.5 py-1.5 text-xs">
                  <span className="font-mono font-semibold">v{v.version}</span>
                  <span className="text-muted-foreground">tol {v.amount_tolerance_paise}p · lag {v.timing_lag_days}d</span>
                  <span className="font-mono text-[10px] text-muted-foreground">{fmtDate(v.created_at)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

          {/* RBAC + MFA + pending approvals */}
          <div className="space-y-5">
          <div className="rounded-md border border-border bg-card p-4">
            <h3 className="flex items-center gap-1.5 text-sm font-bold"><KeyRound size={15} className="text-brand" /> Two-Factor Authentication</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">
              TOTP authenticator (Google Authenticator, 1Password, Authy). Required at every login once enabled.
            </p>

            {mfa.stage === "idle" && (
              <Button data-testid="mfa-setup" onClick={mfaSetup} variant="outline"
                className="mt-3 h-8 gap-1.5 border-brand/40 text-xs text-brand hover:bg-brand/5">
                <KeyRound size={13} /> Set up authenticator
              </Button>
            )}

            {mfa.stage === "verify" && (
              <div className="mt-3 space-y-2">
                <div className="rounded border border-border bg-secondary/50 p-2.5">
                  <div className="text-[10px] font-bold uppercase tracking-wide text-muted-foreground">1. Add this secret to your app</div>
                  <code className="mt-1 block break-all rounded bg-background px-2 py-1 font-mono text-[11px]">{mfa.secret}</code>
                  <div className="mt-1 break-all font-mono text-[9px] text-muted-foreground">{mfa.uri}</div>
                </div>
                <div className="flex items-center gap-2">
                  <input data-testid="mfa-code" placeholder="6-digit code" maxLength={6} value={mfa.code}
                    onChange={(e) => setMfa({ ...mfa, code: e.target.value })}
                    className="h-8 w-28 rounded border border-input bg-background px-2 font-mono text-sm tracking-widest outline-none focus:ring-2 focus:ring-brand" />
                  <Button data-testid="mfa-enable" onClick={mfaEnable} disabled={mfa.code.length !== 6}
                    className="h-8 bg-brand px-3 text-xs text-white hover:bg-brand/90">Verify & enable</Button>
                </div>
              </div>
            )}

            {mfa.stage === "codes" && (
              <div className="mt-3 space-y-2">
                <div data-testid="mfa-recovery" className="rounded border border-warning/40 bg-warning/5 p-2.5">
                  <div className="text-[10px] font-bold uppercase tracking-wide text-warning">Recovery codes — shown once</div>
                  <div className="mt-1 grid grid-cols-4 gap-1 font-mono text-[11px]">
                    {mfa.codes.map((c) => <span key={c} className="rounded bg-background px-1 py-0.5">{c}</span>)}
                  </div>
                </div>
                <Button size="sm" onClick={() => setMfa({ stage: "idle", secret: "", uri: "", code: "", codes: null, password: "" })}
                  className="h-7 bg-brand text-xs text-white hover:bg-brand/90">Done</Button>
              </div>
            )}

            {mfa.stage === "disable" && (
              <div className="mt-3 flex items-center gap-2">
                <input type="password" placeholder="Confirm password" value={mfa.password}
                  onChange={(e) => setMfa({ ...mfa, password: e.target.value })}
                  className="h-8 w-44 rounded border border-input bg-background px-2 text-xs outline-none focus:ring-2 focus:ring-brand" />
                <Button onClick={mfaDisable} variant="destructive" className="h-8 px-3 text-xs">Disable MFA</Button>
                <button onClick={() => setMfa({ stage: "idle", secret: "", uri: "", code: "", codes: null, password: "" })}
                  className="text-[11px] text-muted-foreground hover:text-foreground">cancel</button>
              </div>
            )}
            {mfa.stage === "idle" && (
              <button onClick={() => setMfa({ ...mfa, stage: "disable" })}
                className="mt-2 text-[10px] text-muted-foreground underline hover:text-destructive">Disable existing MFA</button>
            )}
          </div>

          <div className="rounded-md border border-border bg-card p-4">
            <h3 className="flex items-center gap-1.5 text-sm font-bold"><Users size={15} className="text-brand" /> Roles & Access</h3>
            <div className="mt-3 space-y-1.5">
              {(meta.roles || []).map((r) => (
                <div key={r} className={`flex items-center justify-between rounded border px-3 py-2 text-xs ${r === user?.role ? "border-brand bg-brand/5" : "border-border"}`}>
                  <div><span className="font-semibold">{meta.labels?.[r] || r}</span>{r === user?.role && <span className="ml-2 rounded bg-brand px-1.5 py-0.5 text-[9px] font-bold uppercase text-white">you</span>}</div>
                  <span className="text-[10px] text-muted-foreground">{ROLE_CAPS[r]}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="rounded-md border border-border bg-card p-4">
            <h3 className="flex items-center gap-1.5 text-sm font-bold"><Clock size={15} className="text-brand" /> Batch Schedules</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">Cron-driven recurring ingestion (UTC). Runs are overlap-guarded and audit-logged.</p>

            {canEdit && (
              <div className="mt-3 flex flex-wrap items-center gap-2">
                <input data-testid="schedule-name" placeholder="Schedule name" value={schedForm.name}
                  onChange={(e) => setSchedForm({ ...schedForm, name: e.target.value })}
                  className="h-8 w-36 rounded border border-input bg-background px-2.5 text-xs outline-none focus:ring-2 focus:ring-brand" />
                <input data-testid="schedule-cron" placeholder="0 6 * * *" value={schedForm.cron}
                  onChange={(e) => setSchedForm({ ...schedForm, cron: e.target.value })}
                  className="h-8 w-28 rounded border border-input bg-background px-2.5 font-mono text-xs outline-none focus:ring-2 focus:ring-brand" />
                <select data-testid="schedule-action" value={schedForm.action}
                  onChange={(e) => setSchedForm({ ...schedForm, action: e.target.value })}
                  className="h-8 rounded border border-input bg-background px-2 text-xs outline-none focus:ring-2 focus:ring-brand">
                  {Object.entries(ACTION_LABELS).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                </select>
                <Button data-testid="schedule-create" onClick={createSchedule} disabled={!schedForm.name.trim()}
                  className="h-8 bg-brand px-3 text-xs text-white hover:bg-brand/90">Create</Button>
              </div>
            )}

            <div className="mt-3 space-y-1.5">
              {schedules.length === 0 && <p className="py-4 text-center text-xs text-muted-foreground">No schedules configured.</p>}
              {schedules.map((s) => (
                <div key={s.id} data-testid={`schedule-${s.id}`} className="flex items-center justify-between rounded border border-border px-3 py-2 text-xs">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${!s.enabled ? "bg-muted-foreground/40" : s.last_status === "failed" ? "bg-destructive" : s.last_status === "ok" ? "bg-success" : "bg-border"}`} />
                      <span className="font-semibold">{s.name}</span>
                      <span className="rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px]">{s.cron}</span>
                    </div>
                    <div className="mt-0.5 truncate text-[10px] text-muted-foreground">
                      {ACTION_LABELS[s.action] || s.action}
                      {" · runs: "}{s.run_count ?? 0}
                      {s.next_run_at && ` · next ${fmtDate(s.next_run_at)}`}
                      {s.last_run_at && ` · last ${fmtDate(s.last_run_at)}`}
                    </div>
                  </div>
                  {canEdit && (
                    <div className="ml-2 flex shrink-0 gap-1.5">
                      <button data-testid={`schedule-run-${s.id}`} onClick={() => runSchedule(s)}
                        className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-[11px] font-semibold text-muted-foreground hover:border-brand hover:text-brand"><Play size={11} /> Run</button>
                      <button data-testid={`schedule-del-${s.id}`} onClick={() => deleteSchedule(s)}
                        className="rounded border border-border p-1 text-muted-foreground hover:border-destructive hover:text-destructive"><Trash2 size={11} /></button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div className="rounded-md border border-border bg-card p-4">
            <h3 className="flex items-center gap-1.5 text-sm font-bold"><ShieldCheck size={15} className="text-warning" /> Pending Checker Approvals</h3>
            <p className="mt-0.5 text-xs text-muted-foreground">Material overrides awaiting a controller/admin second sign-off.</p>
            <div className="mt-3 space-y-1.5">
              {pending.length === 0 && <p className="py-4 text-center text-xs text-muted-foreground">No pending approvals.</p>}
              {pending.map((c) => (
                <div key={c.id} data-testid={`pending-${c.id}`} className="rounded border border-warning/40 bg-warning/5 px-3 py-2 text-xs">
                  <div className="flex justify-between font-mono"><span>{c.settlement_id || c.utr}</span><span className="text-destructive">{(c.value_at_risk_paise / 100).toLocaleString("en-IN")}</span></div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">Requested by {c.review?.by} — resolve in Exceptions drawer.</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
