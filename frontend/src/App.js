import "@/App.css";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { Toaster } from "@/components/ui/sonner";
import { AuthProvider } from "@/context/AuthContext";
import { ThemeProvider } from "@/context/ThemeContext";
import ProtectedRoute from "@/components/ProtectedRoute";
import Login from "@/pages/Login";
import Dashboard from "@/pages/Dashboard";
import Batches from "@/pages/Batches";
import Workbench from "@/pages/Workbench";
import Exceptions from "@/pages/Exceptions";
import Reports from "@/pages/Reports";
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
              <Route path="/" element={<P><Dashboard /></P>} />
              <Route path="/batches" element={<P><Batches /></P>} />
              <Route path="/workbench" element={<P><Workbench /></P>} />
              <Route path="/exceptions" element={<P><Exceptions /></P>} />
              <Route path="/reports" element={<P><Reports /></P>} />
              <Route path="/audit" element={<P><Audit /></P>} />
              <Route path="/copilot" element={<P><Copilot /></P>} />
              <Route path="/admin" element={<P><Admin /></P>} />
            </Routes>
          </BrowserRouter>
          <Toaster position="top-right" richColors />
        </AuthProvider>
      </ThemeProvider>
    </div>
  );
}

export default App;
