import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import api from "@/lib/api";
import { Button } from "@/components/ui/button";
import { formatApiErrorDetail } from "@/lib/format";
import { toast } from "sonner";
import { GitCompareArrows, ShieldCheck, Zap } from "lucide-react";

const DEMO = [
  { email: "analyst@recon.io", role: "Analyst" },
  { email: "controller@recon.io", role: "Controller" },
  { email: "compliance@recon.io", role: "Compliance" },
  { email: "admin@recon.io", role: "Admin" },
  { email: "support@recon.io", role: "Support" },
];
const PW = { "analyst@recon.io": "analyst123", "controller@recon.io": "controller123", "compliance@recon.io": "compliance123", "admin@recon.io": "admin123", "support@recon.io": "support123" };

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("analyst@recon.io");
  const [password, setPassword] = useState("analyst123");
  const [totp, setTotp] = useState("");
  const [mfaRequired, setMfaRequired] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [ssoEnabled, setSsoEnabled] = useState(false);

  useEffect(() => {
    api.get("/auth/sso/config").then(({ data }) => setSsoEnabled(!!data.enabled)).catch(() => {});
  }, []);

  const submit = async (e, em, pw) => {
    e?.preventDefault?.();
    setBusy(true); setError("");
    try {
      await login(em || email, pw || password, totp || undefined);
      toast.success("Signed in");
      setMfaRequired(false); setTotp("");
      navigate("/");
    } catch (err) {
      const detail = err.response?.data?.detail;
      if (detail?.mfa_required) {
        setMfaRequired(true);
        setError("Enter the 6-digit code from your authenticator app.");
      } else {
        setMfaRequired(false);
        setError(formatApiErrorDetail(detail) || err.message);
      }
    } finally { setBusy(false); }
  };

  return (
    <div className="flex min-h-screen bg-background">
      {/* Left brand panel */}
      <div className="hidden w-1/2 flex-col justify-between bg-[#012652] p-12 text-white lg:flex">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded bg-[#0d94fb] font-bold">R</div>
          <div className="text-sm font-bold uppercase tracking-widest">Reconciliation Control Tower</div>
        </div>
        <div>
          <h1 className="text-4xl font-bold leading-tight">Deterministic-first settlement reconciliation.</h1>
          <p className="mt-4 max-w-md text-sm text-white/60">
            Ingest three independent ledgers, match them with a deterministic engine, and use bounded AI agents only on the ambiguous tail — every decision evidence-backed and audited.
          </p>
          <div className="mt-8 space-y-3 text-sm">
            {[[GitCompareArrows, "3-pass exact + tolerance + aggregation matching"],
              [ShieldCheck, "Maker-checker overrides, append-only audit log"],
              [Zap, "Claude agents for triage, narration & Q&A — never auto-post"]].map(([Icon, t], i) => (
              <div key={i} className="flex items-center gap-3 text-white/80">
                <Icon size={16} className="text-[#0d94fb]" /> {t}
              </div>
            ))}
          </div>
        </div>
        <div className="font-mono text-[11px] text-white/40">paise-integer engine · Razorpay Blade</div>
      </div>

      {/* Right form */}
      <div className="flex w-full flex-col justify-center px-6 lg:w-1/2 lg:px-20">
        <div className="mx-auto w-full max-w-sm">
          <h2 className="text-2xl font-bold">Sign in</h2>
          <p className="mt-1 text-sm text-muted-foreground">Access the operator console.</p>

          <form onSubmit={submit} className="mt-6 space-y-3">
            <div>
              <label className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Email</label>
              <input data-testid="login-email" value={email} onChange={(e) => setEmail(e.target.value)}
                className="mt-1 w-full rounded border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand" />
            </div>
            <div>
              <label className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Password</label>
              <input data-testid="login-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                className="mt-1 w-full rounded border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-brand" />
            </div>
            {mfaRequired && (
              <div>
                <label className="text-[11px] font-semibold uppercase tracking-wide text-brand">Authenticator code</label>
                <input data-testid="login-totp" inputMode="numeric" maxLength={10} autoFocus value={totp}
                  onChange={(e) => setTotp(e.target.value)}
                  placeholder="6-digit code or recovery code"
                  className="mt-1 w-full rounded border border-brand/50 bg-background px-3 py-2 font-mono text-sm tracking-widest outline-none focus:ring-2 focus:ring-brand" />
              </div>
            )}
            {ssoEnabled && (
              <a data-testid="login-sso"
                href={`${process.env.REACT_APP_BACKEND_URL || ""}/api/auth/sso/login`}
                className="flex h-10 w-full items-center justify-center rounded border border-border text-sm font-semibold text-foreground transition-colors hover:border-brand hover:text-brand">
                Sign in with SSO
              </a>
            )}
            {error && <div data-testid="login-error" className="rounded border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">{error}</div>}
            <Button data-testid="login-submit" type="submit" disabled={busy}
              className="h-10 w-full bg-brand font-semibold text-white hover:bg-brand/90">
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>

          <div className="mt-6">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Demo accounts</div>
            <div className="mt-2 grid grid-cols-2 gap-2">
              {DEMO.map((d) => (
                <button key={d.email} data-testid={`demo-${d.role.toLowerCase()}`}
                  onClick={(e) => submit(e, d.email, PW[d.email])} disabled={busy}
                  className="rounded border border-border px-3 py-2 text-left text-xs transition-colors hover:border-brand hover:bg-secondary">
                  <div className="font-semibold">{d.role}</div>
                  <div className="font-mono text-[10px] text-muted-foreground">{d.email}</div>
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
