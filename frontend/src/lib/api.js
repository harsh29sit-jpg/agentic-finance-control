import axios from "axios";

// When frontend and backend share the same Vercel deployment, use the same-origin
// /api route. REACT_APP_BACKEND_URL can still override this for a separate backend.
export const API = `${process.env.REACT_APP_BACKEND_URL || ""}/api`;

const api = axios.create({ baseURL: API, withCredentials: true });

api.interceptors.request.use((cfg) => {
  const t = localStorage.getItem("token");
  if (t) cfg.headers.Authorization = `Bearer ${t}`;
  return cfg;
});

export default api;
