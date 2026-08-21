export const rupees = (paise) =>
  `₹${((paise || 0) / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const rupeesCompact = (paise) => {
  const r = (paise || 0) / 100;
  if (Math.abs(r) >= 1e7) return `₹${(r / 1e7).toFixed(2)}Cr`;
  if (Math.abs(r) >= 1e5) return `₹${(r / 1e5).toFixed(2)}L`;
  if (Math.abs(r) >= 1e3) return `₹${(r / 1e3).toFixed(1)}K`;
  return `₹${r.toFixed(2)}`;
};

export const fmtDate = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-IN", {
      day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
};

export const TAXONOMY_LABEL = {
  MISSING_IN_BANK: "Missing in Bank",
  MISSING_IN_LEDGER: "Missing in Ledger",
  AMOUNT_MISMATCH: "Amount Mismatch",
  DUPLICATE: "Duplicate Credit",
  TIMING_LAG: "Timing Lag",
  NARRATION_AMBIGUOUS: "Ambiguous Narration",
  UNIDENTIFIED_CREDIT: "Unidentified Credit",
};

export const formatApiErrorDetail = (detail) => {
  if (detail == null) return "Something went wrong. Please try again.";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail))
    return detail.map((e) => (e && typeof e.msg === "string" ? e.msg : JSON.stringify(e))).filter(Boolean).join(" ");
  if (detail && typeof detail.msg === "string") return detail.msg;
  return String(detail);
};
