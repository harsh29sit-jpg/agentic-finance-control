import { createContext, useContext, useEffect, useState } from "react";
import api from "@/lib/api";

const AuthContext = createContext(null);

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null); // null = checking, false = anon
  const [meta, setMeta] = useState({ labels: {}, roles: [], taxonomy: [] });

  const loadMe = async () => {
    const token = localStorage.getItem("token");
    if (!token) { setUser(false); return; }
    try {
      const { data } = await api.get("/auth/me");
      setUser(data);
    } catch {
      localStorage.removeItem("token");
      setUser(false);
    }
  };

  useEffect(() => { loadMe(); }, []);

  useEffect(() => {
    if (user && user !== false) {
      api.get("/meta/roles").then(({ data }) => setMeta(data)).catch(() => {});
    }
  }, [user]);

  const login = async (email, password) => {
    const { data } = await api.post("/auth/login", { email, password });
    localStorage.setItem("token", data.token);
    if (data.refresh_token) localStorage.setItem("refresh_token", data.refresh_token);
    setUser(data);
    return data;
  };

  const logout = async () => {
    try {
      await api.post("/auth/logout",
        { refresh_token: localStorage.getItem("refresh_token") });
    } catch {}
    localStorage.removeItem("token");
    localStorage.removeItem("refresh_token");
    setUser(false);
  };

  return (
    <AuthContext.Provider value={{ user, meta, login, logout, loadMe }}>
      {children}
    </AuthContext.Provider>
  );
};

export const useAuth = () => useContext(AuthContext);
