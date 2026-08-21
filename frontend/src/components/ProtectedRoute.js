import { Navigate } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import AppShell from "@/components/AppShell";

export const ProtectedRoute = ({ children }) => {
  const { user } = useAuth();
  if (user === null)
    return (
      <div className="flex h-screen items-center justify-center bg-background text-sm text-muted-foreground">
        <div className="animate-pulse font-mono">Loading control tower…</div>
      </div>
    );
  if (user === false) return <Navigate to="/login" replace />;
  return <AppShell>{children}</AppShell>;
};

export default ProtectedRoute;
