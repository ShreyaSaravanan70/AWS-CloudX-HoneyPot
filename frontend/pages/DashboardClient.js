import { useCallback, useEffect, useState } from "react";
import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  BarElement,
  Tooltip,
} from "chart.js";
import { Line, Bar } from "react-chartjs-2";
import {
  health,
  getStats,
  getEvents,
  getCharts,
  getConfig,
  uploadLog,
  getUploadStatus,
} from "../lib/api";

const MAX_UPLOAD_BYTES = 16 * 1024;
const MAX_UPLOAD_EVENTS = 25;
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, BarElement, Tooltip);

const chartOptions = {
  responsive: true,
  maintainAspectRatio: false,
  plugins: {
    legend: { display: false },
    tooltip: { callbacks: { label: (item) => `${item.parsed.y ?? item.parsed.x} events` } },
  },
  scales: {
    x: {
      title: { display: true, color: "#526575", font: { weight: "600" } },
      ticks: { color: "#526575", maxRotation: 0, autoSkip: true },
      grid: { color: "#e5e0d6" },
    },
    y: {
      beginAtZero: true,
      title: { display: true, text: "Events", color: "#526575", font: { weight: "600" } },
      ticks: { color: "#526575", precision: 0 },
      grid: { color: "#e5e0d6" },
    },
  },
};

const number = (value) =>
  Number.isFinite(Number(value)) ? Number(value).toLocaleString("en-US") : "—";

function formatTimestamp(value) {
  if (!value) return "—";
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return `${new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "UTC",
  }).format(date)} UTC`;
}

export default function DashboardClient() {
  const [data, setData] = useState({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [updated, setUpdated] = useState("");
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadState, setUploadState] = useState({ status: "idle", progress: 0, message: "" });

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const results = await Promise.allSettled([
        getConfig(),
        health(),
        getStats(),
        getEvents({ limit: 25 }),
        getCharts(12),
      ]);
      const keys = ["config", "health", "stats", "events", "charts"];
      const next = {};
      const failures = [];
      results.forEach((result, index) => {
        if (result.status === "fulfilled") next[keys[index]] = result.value;
        else failures.push(`${keys[index]}: ${result.reason.message}`);
      });
      setData((previous) => ({ ...previous, ...next }));
      setError(failures.join(" | "));
      setUpdated(new Date().toLocaleTimeString());
    } catch (err) {
      setError(err.message || "Unable to load dashboard data.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, [refresh]);

  const stats = data.stats || {};
  const events = data.events?.events || [];
  const charts = data.charts || {};
  const trend = charts.events_over_time || [];
  const ips = charts.top_ips || [];

  async function uploadAndAnalyze() {
    if (!uploadFile) return;
    if (uploadFile.size > MAX_UPLOAD_BYTES) {
      setUploadState({ status: "error", progress: 0, message: "Choose a file no larger than 16 KiB." });
      return;
    }

    setUploadState({ status: "uploading", progress: 0, message: "Uploading log…" });
    let upload;
    try {
      upload = await uploadLog(uploadFile, (progress) =>
        setUploadState({ status: "uploading", progress, message: `Uploading… ${progress}%` })
      );
    } catch (err) {
      setUploadState({ status: "error", progress: 0, message: err.message });
      return;
    }

    setUploadState({ status: "processing", progress: 100, message: "Upload complete. Waiting for parser confirmation…" });
    try {
      for (let attempt = 0; attempt < 24; attempt += 1) {
        await delay(5000);
        const status = await getUploadStatus(upload.upload_id);
        if (status.status === "processed") {
          setUploadState({
            status: "success",
            progress: 100,
            message: `DynamoDB confirmed ${status.events_detected} events. Dashboard data refreshed.`,
          });
          await refresh();
          return;
        }
      }
      setUploadState({ status: "pending", progress: 100, message: "Upload succeeded; processing is not confirmed yet. Check again later." });
    } catch (err) {
      setUploadState({ status: "pending", progress: 100, message: `Upload succeeded, but status could not be checked: ${err.message}` });
    }
  }

  const sourceMode = data.config?.mode;
  const metrics = [
    { label: "Events in sample", value: stats.total_events, note: "Rows returned by the bounded scan" },
    { label: "Source IP addresses", value: stats.unique_source_ips, note: "Distinct addresses in the sample" },
    { label: "Sessions", value: stats.unique_sessions, note: "Distinct session IDs in the sample" },
    { label: "Credential attempts", value: stats.credential_attempts, note: "Rows with captured input" },
  ];


  return (
    <div className="app-shell">
      <header className="masthead">
        <div className="masthead-inner">
          <a className="brand-lockup" href="#overview" aria-label="HaaS dashboard home">
            <span className="brand-kicker">AWS HaaS · Security monitoring</span>
            <span className="brand-title">Cowrie activity</span>
          </a>
          <div className="masthead-actions">
            <button className="button button-light" type="button" onClick={refresh} disabled={refreshing}>
              {refreshing ? "Refreshing…" : "Refresh data"}
            </button>
          </div>
        </div>
      </header>

      <nav className="section-nav" aria-label="Dashboard sections">
        <div className="section-nav-inner">
          {[["overview", "Overview"], ["upload", "Upload log"], ["activity", "Activity"], ["events", "Events"]].map(([id, label]) => (
            <a className="nav-link" href={`#${id}`} key={id}>{label}</a>
          ))}
          <span className="refresh-time">{updated ? `Updated ${updated}` : "Waiting for data"}</span>
        </div>
      </nav>

      <main className="main-content">
        {error && (
          <div className="notice notice-error" role="alert">
            <strong>Some data could not be loaded.</strong>
            <span>{error}</span>
          </div>
        )}

        <section className="content-section" id="overview" aria-labelledby="overview-title">
          <div className="section-heading">
            <div>
              <h1 id="overview-title">Overview</h1>
              <p>Current Cowrie activity from the configured data source.</p>
            </div>
            {data.config?.data_source && <span className="source-description">{data.config.data_source}</span>}
          </div>
          <div className="metric-grid">
            {metrics.map((metric) => (
              <article className="metric-panel" key={metric.label}>
                <h2>{metric.label}</h2>
                <p className="metric-value">{loading && metric.value == null ? "…" : number(metric.value)}</p>
                <p className="metric-note">{metric.note}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="content-section" id="upload" aria-labelledby="upload-title">
          <div className="section-heading">
            <div>
              <h2 id="upload-title">Upload a Cowrie log</h2>
              <p>Processing is confirmed only after parsed events appear in DynamoDB.</p>
            </div>
          </div>
          <div className="upload-panel">
            <div className="upload-controls">
              <div className="file-field">
                <label htmlFor="cowrie-file">Log file <span>(.log, UTF-8)</span></label>
                <input
                  id="cowrie-file"
                  className="file-input"
                  type="file"
                  accept=".log,text/plain"
                  disabled={uploadState.status === "uploading" || uploadState.status === "processing"}
                  onChange={(event) => setUploadFile(event.target.files?.[0] || null)}
                  aria-describedby="upload-guidance"
                />
              </div>
              <button
                className="button button-coral"
                type="button"
                onClick={uploadAndAnalyze}
                disabled={!uploadFile || uploadState.status === "uploading" || uploadState.status === "processing"}
              >
                {uploadState.status === "uploading" ? "Uploading…" : uploadState.status === "processing" ? "Analyzing…" : "Upload and analyze"}
              </button>
            </div>
            {uploadFile && <p className="selected-file">Selected: {uploadFile.name} · {number(uploadFile.size)} bytes</p>}
            {uploadState.status !== "idle" && (
              <div className={`upload-feedback feedback-${uploadState.status}`} role={uploadState.status === "error" ? "alert" : "status"} aria-live="polite">
                {(uploadState.status === "uploading" || uploadState.status === "processing") && (
                  <progress className="upload-progress" max="100" value={uploadState.progress} aria-label="Upload progress" />
                )}
                <span>{uploadState.message}</span>
              </div>
            )}
          </div>
        </section>

        <section className="content-section" id="activity" aria-labelledby="activity-title">
          <div className="section-heading">
            <div>
              <h2 id="activity-title">Activity</h2>
              <p>Event counts from the dashboard sample; timestamps are UTC.</p>
            </div>
          </div>
          <div className="chart-grid">
            <article className="panel chart-panel">
              <h3>Events over time</h3>
              <div className="chart-box">
                {trend.length ? (
                  <Line
                    data={{
                      labels: trend.map((row) => row.timestamp),
                      datasets: [{ label: "Events", data: trend.map((row) => row.events), borderColor: "#18324B", backgroundColor: "#18324B", borderWidth: 2, pointRadius: 2, tension: 0.15 }],
                    }}
                    options={{ ...chartOptions, scales: { ...chartOptions.scales, x: { ...chartOptions.scales.x, title: { ...chartOptions.scales.x.title, text: "Time (UTC)" } } } }}
                    aria-label="Line chart of Cowrie events over time"
                  />
                ) : <p className="empty-chart">{loading ? "Loading chart data…" : "No activity data available."}</p>}
              </div>
            </article>
            <article className="panel chart-panel">
              <h3>Events by source IP</h3>
              <div className="chart-box">
                {ips.length ? (
                  <Bar
                    data={{
                      labels: ips.map((row) => row.src_ip),
                      datasets: [{ label: "Events", data: ips.map((row) => row.events), backgroundColor: "#42627C", borderRadius: 2 }],
                    }}
                    options={{ ...chartOptions, scales: { ...chartOptions.scales, x: { ...chartOptions.scales.x, title: { ...chartOptions.scales.x.title, text: "Source IP" } } } }}
                    aria-label="Bar chart of Cowrie events by source IP"
                  />
                ) : <p className="empty-chart">{loading ? "Loading chart data…" : "No source IP data available."}</p>}
              </div>
            </article>
          </div>
        </section>

        <section className="content-section" id="events" aria-labelledby="events-title">
          <div className="section-heading">
            <div>
              <h2 id="events-title">Recent events</h2>
              <p>Up to 25 records returned by the events API.</p>
            </div>
            <span className="result-count">{number(events.length)} shown</span>
          </div>
          <div className="panel table-panel">
            {events.length ? (
              <div className="table-scroll">
                <table className="events-table">
                  <thead>
                    <tr>
                      {["Timestamp (UTC)", "Source IP", "Event type", "Captured input / command", "Session ID"].map((heading) => <th key={heading}>{heading}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {events.map((event, index) => (
                      <tr key={`${event.session}-${event.timestamp}-${index}`}>
                        <td className="timestamp-cell">{formatTimestamp(event.timestamp)}</td>
                        <td>{event.src_ip || "—"}</td>
                        <td>{event.eventid || "—"}</td>
                        <td className="payload-cell">{event.input && event.input !== "none" ? event.input : event.command && event.command !== "none" ? event.command : "—"}</td>
                        <td className="session-cell">{event.session || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="empty-state">{loading ? "Loading events…" : "No events are available in the current sample."}</p>
            )}
          </div>
        </section>

        <footer className="page-footer">
          <span>{sourceMode === "real" ? "Live AWS data" : sourceMode === "local" ? "LocalStack demo data" : "Connecting to data source"}</span>
          <span>{data.config?.region ? `Region ${data.config.region}` : "Region unavailable"}</span>
          <span>{updated ? `Last refreshed ${updated}` : "Not yet refreshed"}</span>
        </footer>
      </main>
    </div>
  );
}
