#!/usr/bin/env node
/* Local same-origin server: serves the production React build and proxies
 * /api/* to the FastAPI backend. Mirrors the Vercel deployment topology.
 *
 * Usage: node scripts/local_server.js [port] [buildDir] [backendPort]
 */
const http = require("http");
const fs = require("fs");
const path = require("path");

const PORT = parseInt(process.argv[2] || process.env.PORT || "3000", 10);
const BUILD = path.resolve(process.argv[3] || process.env.BUILD_DIR || path.join(__dirname, "..", "frontend", "build"));
const BACKEND_PORT = parseInt(process.argv[4] || process.env.BACKEND_PORT || "8000", 10);

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".ico": "image/x-icon",
  ".map": "application/json",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".txt": "text/plain",
};

const server = http.createServer((req, res) => {
  if (req.url.startsWith("/api/")) {
    const upstream = http.request(
      {
        host: "127.0.0.1",
        port: BACKEND_PORT,
        path: req.url,
        method: req.method,
        headers: { ...req.headers, host: `127.0.0.1:${BACKEND_PORT}` },
      },
      (r) => {
        res.writeHead(r.statusCode, r.headers);
        r.pipe(res);
      },
    );
    upstream.on("error", () => {
      res.writeHead(502, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: "Backend unreachable" }));
    });
    req.pipe(upstream);
    return;
  }

  let file = path.join(BUILD, decodeURIComponent(req.url.split("?")[0]));
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    file = path.join(BUILD, "index.html"); // SPA fallback
  }
  const ext = path.extname(file).toLowerCase();
  res.writeHead(200, { "Content-Type": MIME[ext] || "application/octet-stream" });
  fs.createReadStream(file).pipe(res);
});

server.listen(PORT, () =>
  console.log(`Control Tower UI  ->  http://localhost:${PORT}  (api -> :${BACKEND_PORT}, build: ${BUILD})`),
);
