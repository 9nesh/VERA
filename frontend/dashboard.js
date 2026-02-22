/**
 * VERA Observatory — NEPA Data Dashboard
 * D3 choropleth + Chart.js bar/donut
 */

/* ── Agency short names ─────────────────────────────────────────── */
const AGENCY_SHORT = {
  "Department of Energy": "DOE",
  "Department of the Interior - Bureau of Land Management": "BLM",
  "Department of Energy - Department of Energy": "DOE (sub-agency)",
  "Department of Agriculture": "USDA",
  "Department of Energy - Power Marketing Administration": "DOE – Power Mktg",
  "Department of Energy - National Nuclear Security Administration": "NNSA",
  "Department of Agriculture - Forest Service": "USFS",
  "Department of Energy - Energy Programs": "DOE – Energy Programs",
  "Department of Transportation": "DOT",
  "Major Independent Agencies - Corps of Engineers--Civil Works": "Army Corps",
  "Department of the Interior - Bureau of Reclamation": "Bureau of Reclamation",
  "Department of Defense--Military Programs": "DoD – Military",
};

/* ── State abbreviation → full name (for tooltip) ──────────────── */
const ABBREV_TO_NAME = {
  AL:"Alabama",AK:"Alaska",AZ:"Arizona",AR:"Arkansas",CA:"California",
  CO:"Colorado",CT:"Connecticut",DE:"Delaware",FL:"Florida",GA:"Georgia",
  HI:"Hawaii",ID:"Idaho",IL:"Illinois",IN:"Indiana",IA:"Iowa",KS:"Kansas",
  KY:"Kentucky",LA:"Louisiana",ME:"Maine",MD:"Maryland",MA:"Massachusetts",
  MI:"Michigan",MN:"Minnesota",MS:"Mississippi",MO:"Missouri",MT:"Montana",
  NE:"Nebraska",NV:"Nevada",NH:"New Hampshire",NJ:"New Jersey",NM:"New Mexico",
  NY:"New York",NC:"North Carolina",ND:"North Dakota",OH:"Ohio",OK:"Oklahoma",
  OR:"Oregon",PA:"Pennsylvania",RI:"Rhode Island",SC:"South Carolina",
  SD:"South Dakota",TN:"Tennessee",TX:"Texas",UT:"Utah",VT:"Vermont",
  VA:"Virginia",WA:"Washington",WV:"West Virginia",WI:"Wisconsin",WY:"Wyoming",
  DC:"District of Columbia",PR:"Puerto Rico",
};

/* ── TopoJSON FIPS → state abbreviation ────────────────────────── */
const FIPS_TO_ABBREV = {
  "01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT",
  "10":"DE","11":"DC","12":"FL","13":"GA","15":"HI","16":"ID","17":"IL",
  "18":"IN","19":"IA","20":"KS","21":"KY","22":"LA","23":"ME","24":"MD",
  "25":"MA","26":"MI","27":"MN","28":"MS","29":"MO","30":"MT","31":"NE",
  "32":"NV","33":"NH","34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND",
  "39":"OH","40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD",
  "47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA","54":"WV",
  "55":"WI","56":"WY",
};

/* ── Palette ────────────────────────────────────────────────────── */
const C = {
  bg:       "#080d18",
  surface:  "#0d1829",
  border:   "#1e3050",
  text:     "#e2e8f0",
  muted:    "#64748b",
  teal:     "#0d9488",
  tealLit:  "#14b8a6",
  amber:    "#f59e0b",
  red:      "#ef4444",
  blue:     "#3b82f6",
};

/* ── Utilities ──────────────────────────────────────────────────── */
function fmt(n) { return Number(n).toLocaleString(); }

function animateCount(el, target, duration = 1400) {
  const start = performance.now();
  function step(now) {
    const p = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = fmt(Math.round(target * eased));
    if (p < 1) requestAnimationFrame(step);
  }
  requestAnimationFrame(step);
}

function shortAgency(name) {
  return AGENCY_SHORT[name] || name.replace(/Department of /g, "Dept. ").substring(0, 30);
}

/* ── Color scale for map (sqrt-compressed) ──────────────────────── */
function makeColorScale(maxCount) {
  const stops = ["#1a3a6b", "#1d5c9e", "#0ea5e9", "#f59e0b", "#ef4444"];
  const interp = d3.interpolateRgbBasis(stops);
  return (count) => {
    if (!count || count === 0) return "#0d1829";
    const t = Math.sqrt(count / maxCount);
    return interp(t);
  };
}

/* ── Render: stat cards ─────────────────────────────────────────── */
function renderStats(stats) {
  animateCount(document.getElementById("stat-projects"), stats.total_projects);
  animateCount(document.getElementById("stat-docs"), stats.total_documents);
  animateCount(document.getElementById("stat-pages"), stats.total_pages);
  animateCount(document.getElementById("stat-agencies"), stats.total_agencies);

  const byType = Object.fromEntries(
    (stats.by_process_type || []).map(r => [r.process_type, r.count])
  );
  const split = document.getElementById("stat-type-split");
  split.innerHTML =
    `<span style="color:#3b82f6">CE ${fmt(byType.CE||0)}</span> · ` +
    `<span style="color:#f59e0b">EA ${fmt(byType.EA||0)}</span> · ` +
    `<span style="color:#ef4444">EIS ${fmt(byType.EIS||0)}</span>`;
}

/* ── Render: US choropleth map ──────────────────────────────────── */
async function renderMap(stateData, totalProjects) {
  const lookup = Object.fromEntries(stateData.map(d => [d.state, d]));
  const maxCount = stateData[0]?.total || 1;

  document.getElementById("legend-max").textContent = fmt(maxCount) + " projects";
  const colorScale = makeColorScale(maxCount);

  let us;
  try {
    us = await d3.json("/us-states-10m.json");
  } catch {
    console.error("Failed to load US Atlas TopoJSON");
    return;
  }

  const states = topojson.feature(us, us.objects.states);

  const W = 960, H = 580;
  const projection = d3.geoAlbersUsa().scale(1200).translate([W / 2, H / 2]);
  const path = d3.geoPath().projection(projection);

  const svg = d3.select("#us-map")
    .append("svg")
    .attr("viewBox", `0 0 ${W} ${H}`)
    .attr("preserveAspectRatio", "xMidYMid meet");

  // Subtle grid/background
  svg.append("rect").attr("width", W).attr("height", H).attr("fill", C.surface);

  const tooltip = document.getElementById("map-tooltip");

  // State fills
  svg.append("g")
    .selectAll("path")
    .data(states.features)
    .join("path")
    .attr("d", path)
    .attr("fill", d => {
      const fips = String(d.id).padStart(2, "0");
      const abbrev = FIPS_TO_ABBREV[fips];
      const info = abbrev ? lookup[abbrev] : null;
      return colorScale(info ? info.total : 0);
    })
    .attr("stroke", "#1a2e4a")
    .attr("stroke-width", 0.6)
    .style("cursor", "pointer")
    .on("mousemove", function (event, d) {
      const fips = String(d.id).padStart(2, "0");
      const abbrev = FIPS_TO_ABBREV[fips];
      const info = abbrev ? lookup[abbrev] : null;
      const stateName = ABBREV_TO_NAME[abbrev] || abbrev || "Unknown";
      const total = info ? info.total : 0;
      const pct = totalProjects ? ((total / totalProjects) * 100).toFixed(1) : "0.0";

      tooltip.style.display = "block";
      tooltip.style.left = (event.clientX + 14) + "px";
      tooltip.style.top = (event.clientY - 10) + "px";
      tooltip.innerHTML = `
        <div class="tt-state">${stateName}${abbrev ? ` <span style="color:var(--muted);font-weight:400;font-size:0.75rem">(${abbrev})</span>` : ""}</div>
        <div class="tt-total">${fmt(total)} projects</div>
        ${info ? `
          <div class="tt-row"><span style="color:#3b82f6">■</span> CE: ${fmt(info.CE)}</div>
          <div class="tt-row"><span style="color:#f59e0b">■</span> EA: ${fmt(info.EA)}</div>
          <div class="tt-row"><span style="color:#ef4444">■</span> EIS: ${fmt(info.EIS)}</div>
        ` : '<div class="tt-row" style="color:var(--muted)">No data</div>'}
        <div class="tt-pct">${pct}% of national total</div>
      `;

      // Highlight hovered state
      d3.select(this).attr("stroke", "#ffffff").attr("stroke-width", 1.5);
    })
    .on("mouseleave", function () {
      tooltip.style.display = "none";
      d3.select(this).attr("stroke", "#1a2e4a").attr("stroke-width", 0.6);
    });

  // State borders mesh
  svg.append("path")
    .datum(topojson.mesh(us, us.objects.states, (a, b) => a !== b))
    .attr("fill", "none")
    .attr("stroke", "#1a2e4a")
    .attr("stroke-width", 0.5)
    .attr("d", path);

  // State abbreviation labels (only for large-enough states)
  const LABEL_STATES = new Set([
    "CA","TX","MT","NM","AZ","NV","CO","OR","WY","ID","UT","KS",
    "NE","SD","ND","OK","MN","MO","WA","IA","WI","FL","GA","AL",
    "MS","AR","LA","TN","KY","OH","IN","IL","MI","PA","NY","VA",
    "NC","SC","WV","ME","AK","HI",
  ]);

  svg.append("g")
    .selectAll("text")
    .data(states.features)
    .join("text")
    .attr("transform", d => {
      const c = path.centroid(d);
      return c ? `translate(${c})` : "translate(-999,-999)";
    })
    .attr("text-anchor", "middle")
    .attr("dominant-baseline", "central")
    .attr("font-size", "8px")
    .attr("font-weight", "600")
    .attr("fill", d => {
      const fips = String(d.id).padStart(2, "0");
      const abbrev = FIPS_TO_ABBREV[fips];
      const info = abbrev ? lookup[abbrev] : null;
      return (info && info.total > 500) ? "rgba(255,255,255,0.9)" : "rgba(255,255,255,0.4)";
    })
    .attr("pointer-events", "none")
    .text(d => {
      const fips = String(d.id).padStart(2, "0");
      const abbrev = FIPS_TO_ABBREV[fips];
      return (abbrev && LABEL_STATES.has(abbrev)) ? abbrev : "";
    });
}

/* ── Render: process type donut ─────────────────────────────────── */
function renderDonut(byProcessType) {
  const data = Object.fromEntries((byProcessType || []).map(r => [r.process_type, r.count]));
  const labels = ["CE", "EA", "EIS"];
  const values = labels.map(l => data[l] || 0);
  const colors = [C.blue, C.amber, C.red];
  const total = values.reduce((a, b) => a + b, 0);

  new Chart(document.getElementById("donut-chart"), {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: colors,
        borderColor: C.surface,
        borderWidth: 3,
        hoverOffset: 6,
      }],
    },
    options: {
      responsive: true,
      cutout: "68%",
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: "rgba(8,13,24,0.95)",
          borderColor: C.border,
          borderWidth: 1,
          titleColor: C.text,
          bodyColor: C.muted,
          callbacks: {
            label: (ctx) => ` ${fmt(ctx.parsed)} (${((ctx.parsed/total)*100).toFixed(1)}%)`,
          },
        },
      },
    },
  });

  // Custom legend
  const legend = document.getElementById("donut-legend");
  labels.forEach((l, i) => {
    const pct = total ? ((values[i] / total) * 100).toFixed(1) : "0.0";
    legend.innerHTML += `
      <div style="text-align:center">
        <div style="width:10px;height:10px;border-radius:50%;background:${colors[i]};margin:0 auto 3px;"></div>
        <div style="font-size:0.7rem;font-weight:700;color:var(--text)">${l}</div>
        <div style="font-size:0.65rem;color:var(--muted)">${pct}%</div>
      </div>`;
  });
}

/* ── Render: agency horizontal bar ─────────────────────────────── */
function renderAgencyChart(agencies) {
  const labels = agencies.map(a => shortAgency(a.agency));
  const ceVals = agencies.map(a => a.CE || 0);
  const eaVals = agencies.map(a => a.EA || 0);
  const eisVals = agencies.map(a => a.EIS || 0);

  const canvas = document.getElementById("agency-chart");
  // Adjust canvas height based on item count
  canvas.style.height = `${Math.max(280, agencies.length * 30)}px`;

  new Chart(canvas, {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "CE",  data: ceVals,  backgroundColor: "rgba(59,130,246,0.75)",  stack: "s" },
        { label: "EA",  data: eaVals,  backgroundColor: "rgba(245,158,11,0.75)",  stack: "s" },
        { label: "EIS", data: eisVals, backgroundColor: "rgba(239,68,68,0.75)",   stack: "s" },
      ],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        x: {
          stacked: true,
          grid: { color: "rgba(30,48,80,0.6)" },
          ticks: {
            color: C.muted,
            font: { size: 10 },
            callback: (v) => v >= 1000 ? (v/1000).toFixed(0)+"k" : v,
          },
        },
        y: {
          stacked: true,
          grid: { display: false },
          ticks: { color: C.muted2, font: { size: 10.5, weight: "500" } },
        },
      },
      plugins: {
        legend: {
          position: "top",
          labels: { color: C.muted2, font: { size: 10 }, boxWidth: 10, boxHeight: 10, padding: 12 },
        },
        tooltip: {
          backgroundColor: "rgba(8,13,24,0.95)",
          borderColor: C.border,
          borderWidth: 1,
          titleColor: C.text,
          bodyColor: C.muted2,
          callbacks: {
            label: (ctx) => ` ${ctx.dataset.label}: ${fmt(ctx.parsed.x)}`,
          },
        },
      },
    },
  });
}

/* ── Render: state leaderboard ──────────────────────────────────── */
function renderLeaderboard(stateData) {
  const maxTotal = stateData[0]?.total || 1;
  const lb = document.getElementById("leaderboard");
  const badge = document.getElementById("lb-count-badge");
  badge.textContent = `${stateData.length} states`;

  lb.innerHTML = stateData.map((d, i) => `
    <div class="lb-row">
      <span class="lb-rank">${i + 1}</span>
      <span class="lb-state">${d.state}</span>
      <div class="lb-bar-wrap">
        <div class="lb-bar" style="width:${Math.round((d.total / maxTotal) * 100)}%"></div>
      </div>
      <span class="lb-count">${fmt(d.total)}</span>
    </div>
  `).join("");
}

/* ── Chart.js global defaults ───────────────────────────────────── */
Chart.defaults.color = C.muted;
Chart.defaults.borderColor = C.border;

/* ── Main init ──────────────────────────────────────────────────── */
async function init() {
  const loadingEl = document.getElementById("loading");
  const errorEl = document.getElementById("error-banner");

  try {
    const [stats, byState, byAgency] = await Promise.all([
      fetch("/api/dashboard/stats").then(r => {
        if (!r.ok) throw new Error(`Stats API ${r.status}`);
        return r.json();
      }),
      fetch("/api/dashboard/by-state").then(r => {
        if (!r.ok) throw new Error(`By-state API ${r.status}`);
        return r.json();
      }),
      fetch("/api/dashboard/by-agency").then(r => {
        if (!r.ok) throw new Error(`By-agency API ${r.status}`);
        return r.json();
      }),
    ]);

    // Hide loading, render everything
    loadingEl.style.display = "none";

    renderStats(stats);
    renderDonut(stats.by_process_type);
    renderAgencyChart(byAgency);
    renderLeaderboard(byState);
    await renderMap(byState, stats.total_projects);

  } catch (err) {
    loadingEl.style.display = "none";
    errorEl.style.display = "block";
    errorEl.textContent = `Failed to load dashboard data: ${err.message}. Make sure the VERA backend is running.`;
    console.error(err);
  }
}

init();
