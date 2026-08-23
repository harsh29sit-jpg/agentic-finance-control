import "@/App.css";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { AuthProvider } from "@/context/AuthContext";
import { ThemeProvider } from "@/context/ThemeContext";
import ProtectedRoute from "@/components/ProtectedRoute";
import ErrorBoundary from "@/components/ErrorBoundary";
import Login from "@/pages/Login";
import Dashboard from "@/pages/Dashboard";
import Batches from "@/pages/Batches";
import Workbench from "@/pages/Workbench";
import Exceptions from "@/pages/Exceptions";
import Reports from "@/pages/Reports";
import Evaluation from "@/pages/Evaluation";
import Audit from "@/pages/Audit";
import Copilot from "@/pages/Copilot";
import Admin from "@/pages/Admin";

const P = ({ children }) => <ProtectedRoute>{children}</ProtectedRoute>;

function App() {
  return (
    <div className="App">
      <ThemeProvider>
        <AuthProvider>
          <BrowserRouter>
            <Routes>
              <Route path="/login" element={<Login />} />
              <Route path="/" element={<P><ErrorBoundary><Dashboard /></ErrorBoundary></P>} />
              <Route path="/batches" element={<P><ErrorBoundary><Batches /></ErrorBoundary></P>} />
              <Route path="/workbench" element={<P><ErrorBoundary><Workbench /></ErrorBoundary></P>} />
              <Route path="/exceptions" element={<P><ErrorBoundary><Exceptions /></ErrorBoundary></P>} />
              <Route path="/reports" element={<P><ErrorBoundary><Reports /></ErrorBoundary></P>} />
              <Route path="/evaluation" element={<P><ErrorBoundary><Evaluation /></ErrorBoundary></P>} />
              <Route path="/audit" element={<P><ErrorBoundary><Audit /></ErrorBoundary></P>} />
              <Route path="/copilot" element={<P><ErrorBoundary><Copilot /></ErrorBoundary></P>} />
              <Route path="/admin" element={<P><ErrorBoundary><Admin /></ErrorBoundary></P>} />
            </Routes>
          </BrowserRouter>
          <Toaster position="top-right" richColors />
        </AuthProvider>
      </ThemeProvider>
    </div>
  );
}

export default App;
