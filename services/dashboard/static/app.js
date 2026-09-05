const pauseBtn = document.getElementById("pauseBtn");
const rateSlider = document.getElementById("rateSlider");
const rateLabel = document.getElementById("rateLabel");
const tpsValue = document.getElementById("tpsValue");
const approvedValue = document.getElementById("approvedValue");
const declinedValue = document.getElementById("declinedValue");
const approvedPct = document.getElementById("approvedPct");
const declinedPct = document.getElementById("declinedPct");
const feedBody = document.getElementById("feedBody");

let paused = false;

// Chart.js loads from a CDN; if that's blocked (offline env, restrictive
// network policy) the rest of the dashboard -- live stats, feed, pause/
// resume, rate control -- must still work. Never let a missing `Chart`
// take down the whole script.
const MAX_POINTS = 60;
let tpsChart = null;
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
      scales: {
        x: { display: false },
        y: { beginAtZero: true, ticks: { color: "#9aa7c2" } },
      },
      plugins: { legend: { display: false } },
    },
  });
} else {
  console.warn("Chart.js failed to load (CDN blocked?) -- TPS chart disabled, other stats still live.");
  document.querySelector(".chart-wrap").textContent = "Chart unavailable (Chart.js failed to load).";
}

function applyControl(control) {
  paused = !!control.paused;
  pauseBtn.textContent = paused ? "Resume" : "Pause";
  pauseBtn.classList.toggle("paused", paused);
  rateSlider.value = control.rate_multiplier ?? 1;
  rateLabel.textContent = `${Number(rateSlider.value).toFixed(1)}x`;
}

function applyStats(stats) {
  tpsValue.textContent = stats.tps.toFixed(1);
  approvedValue.textContent = stats.approved;
  declinedValue.textContent = stats.declined;
  approvedPct.textContent = stats.approve_pct.toFixed(1);
  declinedPct.textContent = stats.decline_pct.toFixed(1);

  if (!tpsChart) return;
  const label = new Date().toLocaleTimeString();
  tpsChart.data.labels.push(label);
  tpsChart.data.datasets[0].data.push(stats.tps);
  if (tpsChart.data.labels.length > MAX_POINTS) {
    tpsChart.data.labels.shift();
    tpsChart.data.datasets[0].data.shift();
  }
  tpsChart.update();
}

function addFeedRow(event) {
  const row = document.createElement("tr");
  const approved = event.response_status === "APPROVED";
  const time = new Date(event.ts).toLocaleTimeString();
  row.innerHTML = `
    <td>${time}</td>
    <td>${event.merchant_id ?? ""}</td>
    <td>${event.terminal_id ?? ""}</td>
    <td>${event.card_network ?? ""}</td>
    <td>${event.currency ?? ""} ${((event.amount ?? 0) / 100).toFixed(2)}</td>
    <td class="${approved ? "approved" : "declined"}">${event.response_status ?? ""}</td>
    <td>${event.response_code ?? ""}</td>
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

pauseBtn.addEventListener("click", async () => {
  const endpoint = paused ? "/api/control/resume" : "/api/control/pause";
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
