// lib/api.js - thin HTTP client for the HaaS backend.
// All data comes from the backend. No mock data is ever shown as real.

const BASE = (function () {
  if (typeof window !== "undefined") {
    // Browser: use same origin or env override
    return process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  }
  // Server: default to backend service in Docker, else localhost
  return process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
})();

async function request(path, options = {}) {
  const url = BASE + path;
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`HTTP ${resp.status} ${path}: ${text || resp.statusText}`);
  }
  return resp.json();
}

export async function health() {
  return request("/health");
}

export async function getStats() {
  return request("/api/stats");
}

export async function getEvents(params = {}) {
  const qs = new URLSearchParams();
  if (params.limit) qs.set("limit", params.limit);
  if (params.src_ip) qs.set("src_ip", params.src_ip);
  if (params.session) qs.set("session", params.session);
  const suffix = qs.toString() ? `?${qs}` : "";
  return request("/api/events" + suffix);
}

export async function getCharts(limit = 12) {
  return request(`/api/charts?limit=${limit}`);
}

export async function getPipeline() {
  return request("/api/pipeline");
}

export async function getConfig() {
  return request("/api/config");
}

export function uploadLog(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", BASE + "/api/uploads");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      let body;
      try { body = JSON.parse(xhr.responseText); } catch { body = {}; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(body.detail || `Upload failed (HTTP ${xhr.status}).`));
    };
    xhr.onerror = () => reject(new Error("Upload connection failed."));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

export async function getUploadStatus(uploadId) {
  return request(`/api/uploads/${encodeURIComponent(uploadId)}`);
}

export default { health, getStats, getEvents, getCharts, getPipeline, getConfig, uploadLog, getUploadStatus };
