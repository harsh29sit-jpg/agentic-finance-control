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

/* Single-flight access-token refresh on 401.
 * Rotates the refresh token server-side; replays the original request once. */
let refreshingPromise = null;

api.interceptors.response.use(
  (r) => r,
  async (error) => {
    const original = error.config || {};
    const status = error.response?.status;
    const url = original.url || "";

    if (status === 401 && !original._retried &&
        localStorage.getItem("refresh_token") &&
        !url.includes("/auth/refresh") && !url.includes("/auth/login")) {
      original._retried = true;
      try {
        refreshingPromise = refreshingPromise ||
          axios.post(`${API}/auth/refresh`,
            { refresh_token: localStorage.getItem("refresh_token") });
        const { data } = await refreshingPromise;
        localStorage.setItem("token", data.token);
        localStorage.setItem("refresh_token", data.refresh_token);
        original.headers.Authorization = `Bearer ${data.token}`;
        return api(original);
      } catch (refreshError) {
        localStorage.removeItem("token");
        localStorage.removeItem("refresh_token");
        if (!window.location.pathname.startsWith("/login")) {
          window.location.href = "/login";
        }
        throw refreshError;
      } finally {
        refreshingPromise = null;
      }
    }
    throw error;
  },
);

export default api;
