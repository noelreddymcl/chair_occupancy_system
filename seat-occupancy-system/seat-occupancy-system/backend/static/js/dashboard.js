const seatGrid = document.getElementById("seat-grid");
const chairCountEl = document.getElementById("chair-count");
const sessionCountEl = document.getElementById("session-count");
const sessionsTbody = document.querySelector("#sessions-table tbody");

let chartByDay, chartByChair;

function fmtDuration(seconds) {
  if (seconds == null) return "—";
  seconds = Math.round(seconds);
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function parseIso(iso) {
  if (!iso) return null;
  return new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
}

function fmtTime(iso) {
  const d = parseIso(iso);
  if (!d) return "—";
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function updateLiveDurations() {
  document.querySelectorAll(".seat-tile[data-occupied='true'] .seat-since").forEach((el) => {
    const since = el.dataset.since;
    const start = parseIso(since);
    if (!start) return;
    const seconds = Math.max(0, Math.floor((Date.now() - start.getTime()) / 1000));
    el.textContent = `Occupied for ${fmtDuration(seconds)}`;
  });
}

async function refreshStatus() {
  try {
    const res = await fetch("/api/status");
    const chairs = await res.json();
    chairCountEl.textContent = `${chairs.length} chair${chairs.length === 1 ? "" : "s"} tracked`;

    if (chairs.length === 0) {
      seatGrid.innerHTML = `<p class="empty-note">Waiting for the first detection…</p>`;
      return;
    }

    seatGrid.innerHTML = chairs.map(c => `
      <div class="seat-tile ${c.occupied ? "is-occupied" : ""}" data-occupied="${c.occupied}">
        <div class="seat-tile-top">
          <span class="seat-dot"></span>
          <span class="seat-label">${c.label}</span>
        </div>
        <span class="seat-state">${c.occupied ? "Occupied" : "Empty"}</span>
        <span class="seat-since" data-since="${c.since || ""}">${c.occupied && c.since ? "Occupied for 0s" : ""}</span>
      </div>
    `).join("");
    updateLiveDurations();
  } catch (e) {
    console.error("status refresh failed", e);
  }
}

async function refreshSessions() {
  try {
    const res = await fetch("/api/sessions?limit=50");
    const sessions = await res.json();
    sessionCountEl.textContent = `${sessions.length} shown`;
    sessionsTbody.innerHTML = sessions.map(s => `
      <tr>
        <td>${s.label}</td>
        <td>${fmtTime(s.start_time)}</td>
        <td>${fmtTime(s.end_time)}</td>
        <td>${fmtDuration(s.duration_seconds)}</td>
      </tr>
    `).join("");
  } catch (e) {
    console.error("sessions refresh failed", e);
  }
}

function chartTheme() {
  return {
    grid: "#232c38",
    text: "#8a96a8",
  };
}

async function refreshAnalytics() {
  try {
    const res = await fetch("/api/analytics");
    const data = await res.json();
    const theme = chartTheme();

    document.getElementById("stat-total-sessions").textContent = data.total_sessions;
    document.getElementById("stat-avg-duration").textContent = fmtDuration(data.avg_duration_seconds);
    document.getElementById("stat-total-duration").textContent = fmtDuration(data.total_duration_seconds);

    const dayLabels = data.by_day.map(d => d.day.slice(5));
    const dayValues = data.by_day.map(d => Math.round(d.total_dur / 60));

    const chairLabels = data.by_chair.map(c => c.label);
    const chairValues = data.by_chair.map(c => Math.round(c.total_dur / 60));

    const commonScales = {
      x: { ticks: { color: theme.text, font: { family: "IBM Plex Mono", size: 10 } }, grid: { color: theme.grid } },
      y: { ticks: { color: theme.text, font: { family: "IBM Plex Mono", size: 10 } }, grid: { color: theme.grid } },
    };

    if (!chartByDay) {
      chartByDay = new Chart(document.getElementById("chart-by-day"), {
        type: "bar",
        data: { labels: dayLabels, datasets: [{ label: "Minutes occupied", data: dayValues, backgroundColor: "#4fa8ff", borderRadius: 4 }] },
        options: { plugins: { legend: { display: false } }, scales: commonScales },
      });
      chartByChair = new Chart(document.getElementById("chart-by-chair"), {
        type: "bar",
        data: { labels: chairLabels, datasets: [{ label: "Minutes occupied", data: chairValues, backgroundColor: "#33d17a", borderRadius: 4 }] },
        options: { indexAxis: "y", plugins: { legend: { display: false } }, scales: commonScales },
      });
    } else {
      chartByDay.data.labels = dayLabels;
      chartByDay.data.datasets[0].data = dayValues;
      chartByDay.update();
      chartByChair.data.labels = chairLabels;
      chartByChair.data.datasets[0].data = chairValues;
      chartByChair.update();
    }
  } catch (e) {
    console.error("analytics refresh failed", e);
  }
}

function refreshAll() {
  refreshStatus();
  refreshSessions();
  refreshAnalytics();
}

refreshAll();
setInterval(refreshStatus, 1000);
setInterval(updateLiveDurations, 1000);
setInterval(refreshSessions, 8000);
setInterval(refreshAnalytics, 8000);
