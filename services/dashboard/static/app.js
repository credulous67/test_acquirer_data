const merchantPauseBtn = document.getElementById("merchantPauseBtn");
const issuerPauseBtn = document.getElementById("issuerPauseBtn");
const rateSlider = document.getElementById("rateSlider");
const rateLabel = document.getElementById("rateLabel");
const tpsValue = document.getElementById("tpsValue");
const approvedValue = document.getElementById("approvedValue");
const declinedValue = document.getElementById("declinedValue");
const approvedPct = document.getElementById("approvedPct");
const declinedPct = document.getElementById("declinedPct");
const outstandingValue = document.getElementById("outstandingValue");
const latencyValue = document.getElementById("latencyValue");
const feedBody = document.getElementById("feedBody");

let merchantPaused = false;
let issuerPaused = false;

// Chart.js is vendored locally (static/vendor/chart.umd.min.js) precisely
// so the browser never needs outbound internet access to render this page
// -- everything it loads comes from this same dashboard container. The
// typeof guard below is just defense in depth (e.g. the file failing to
// copy into an image build): if it's ever missing, the rest of the
// dashboard -- live stats, feed, pause/resume, rate control -- must still
// work rather than the whole script dying on a missing `Chart` global.
const MAX_POINTS = 1800; // one stats tick/sec (see dashboard/app.py's lifespan loop) -> 30 minutes

const X_AXIS = {
  ticks: { color: "#9aa7c2", maxTicksLimit: 8, autoSkip: true },
  grid: { color: "rgba(154,167,194,0.1)" },
};

let tpsChart = null;
let outcomeChart = null;
if (typeof Chart !== "undefined") {
  tpsChart = new Chart(document.getElementById("tpsChart"), {
    type: "line",
    data: {
      labels: [],
      datasets: [{
        label: "TPS",
        data: [],
        borderColor: "#5b8def",
        backgroundColor: "rgba(91,141,239,0.15)",
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      }],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: X_AXIS,
        y: { beginAtZero: true, ticks: { color: "#9aa7c2" } },
      },
      plugins: { legend: { display: false } },
    },
  });

  outcomeChart = new Chart(document.getElementById("outcomeChart"), {
    type: "line",
    data: {
      labels: [],
      datasets: [
        {
          label: "Approved",
          data: [],
          borderColor: "#35c98f",
          backgroundColor: "rgba(53,201,143,0.55)",
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          stack: "pct",
        },
        {
          label: "Declined",
          data: [],
          borderColor: "#f2555a",
          backgroundColor: "rgba(242,85,90,0.55)",
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          stack: "pct",
        },
      ],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: X_AXIS,
        y: {
          stacked: true,
          min: 0,
          max: 100,
          ticks: { color: "#9aa7c2", callback: (v) => `${v}%` },
        },
      },
      plugins: { legend: { display: true, labels: { color: "#9aa7c2" } } },
    },
  });
} else {
  console.warn("Chart.js (static/vendor/chart.umd.min.js) failed to load -- charts disabled, other stats still live.");
  document.querySelectorAll(".chart-wrap").forEach((el) => {
    el.textContent = "Chart unavailable (Chart.js failed to load).";
  });
}

function applyControl(control) {
  merchantPaused = !!control.merchant_paused;
  issuerPaused = !!control.issuer_paused;
  merchantPauseBtn.textContent = merchantPaused ? "Resume merchant" : "Pause merchant";
  merchantPauseBtn.classList.toggle("paused", merchantPaused);
  issuerPauseBtn.textContent = issuerPaused ? "Resume issuer" : "Pause issuer";
  issuerPauseBtn.classList.toggle("paused", issuerPaused);
  rateSlider.value = control.rate_multiplier ?? 1;
  rateLabel.textContent = `${Number(rateSlider.value).toFixed(1)}x`;
}

function applyStats(stats) {
  tpsValue.textContent = stats.tps.toFixed(1);
  approvedValue.textContent = stats.approved;
  declinedValue.textContent = stats.declined;
  approvedPct.textContent = stats.approve_pct.toFixed(1);
  declinedPct.textContent = stats.decline_pct.toFixed(1);
  outstandingValue.textContent = stats.outstanding ?? 0;
  latencyValue.textContent = Math.round(stats.avg_latency_ms ?? 0);

  if (!tpsChart || !outcomeChart) return;
  const label = new Date().toLocaleTimeString();

  tpsChart.data.labels.push(label);
  tpsChart.data.datasets[0].data.push(stats.tps);
  if (tpsChart.data.labels.length > MAX_POINTS) {
    tpsChart.data.labels.shift();
    tpsChart.data.datasets[0].data.shift();
  }
  tpsChart.update();

  outcomeChart.data.labels.push(label);
  outcomeChart.data.datasets[0].data.push(stats.window_approve_pct ?? 0);
  outcomeChart.data.datasets[1].data.push(stats.window_decline_pct ?? 0);
  if (outcomeChart.data.labels.length > MAX_POINTS) {
    outcomeChart.data.labels.shift();
    outcomeChart.data.datasets[0].data.shift();
    outcomeChart.data.datasets[1].data.shift();
  }
  outcomeChart.update();
}

function addFeedRow(event) {
  const row = document.createElement("tr");
  const approved = event.response_status === "APPROVED";
  const time = new Date(event.ts).toLocaleTimeString();
  const duration = event.duration_ms != null ? `${event.duration_ms} ms` : "";
  row.innerHTML = `
    <td>${time}</td>
    <td>${event.merchant_id ?? ""}</td>
    <td>${event.terminal_id ?? ""}</td>
    <td>${event.card_network ?? ""}</td>
    <td>${event.currency ?? ""} ${((event.amount ?? 0) / 100).toFixed(2)}</td>
    <td class="${approved ? "approved" : "declined"}">${event.response_status ?? ""}</td>
    <td>${event.response_code ?? ""}</td>
    <td>${duration}</td>
  `;
  feedBody.prepend(row);
  while (feedBody.rows.length > 200) {
    feedBody.deleteRow(feedBody.rows.length - 1);
  }
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.type === "stats_init") {
      applyControl(data.control);
      applyStats(data);
    } else if (data.type === "stats") {
      applyStats(data);
    } else if (data.type === "event" && data.stage === "completed") {
      addFeedRow(data);
    }
  };

  ws.onclose = () => setTimeout(connect, 1000);
}
connect();

merchantPauseBtn.addEventListener("click", async () => {
  const endpoint = merchantPaused ? "/api/control/merchant/resume" : "/api/control/merchant/pause";
  const resp = await fetch(endpoint, { method: "POST" });
  applyControl(await resp.json());
});

issuerPauseBtn.addEventListener("click", async () => {
  const endpoint = issuerPaused ? "/api/control/issuer/resume" : "/api/control/issuer/pause";
  const resp = await fetch(endpoint, { method: "POST" });
  applyControl(await resp.json());
});

rateSlider.addEventListener("input", async () => {
  rateLabel.textContent = `${Number(rateSlider.value).toFixed(1)}x`;
});
rateSlider.addEventListener("change", async () => {
  const resp = await fetch("/api/control/rate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ multiplier: Number(rateSlider.value) }),
  });
  applyControl(await resp.json());
});
