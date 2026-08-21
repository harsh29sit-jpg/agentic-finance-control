export const PageHeader = ({ title, subtitle, actions }) => (
  <div className="flex items-end justify-between gap-4 border-b border-border px-6 py-4">
    <div>
      <h1 className="text-xl font-bold leading-tight">{title}</h1>
      {subtitle && <p className="mt-0.5 text-xs text-muted-foreground">{subtitle}</p>}
    </div>
    {actions && <div className="flex items-center gap-2">{actions}</div>}
  </div>
);

export default PageHeader;
