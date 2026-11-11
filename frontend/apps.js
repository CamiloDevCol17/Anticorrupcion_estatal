// =====================================================================
// VIGÍA — lógica del dashboard
// Consume la API FastAPI (api.py) y renderiza fichas resumen, gráficas
// (Chart.js) y la tabla paginada de contratos.
// =====================================================================

// Cambia esto si tu API corre en otra dirección/puerto.
let API_BASE = "http://127.0.0.1:8001";

const COLORS = {
  gold: "#B9922F",
  rust: "#B33F32",
  slate: "#96A0AC",
  paper: "#ECE6D6",
  gridLine: "#2A333F",
};

const CHART_PALETTE = ["#B9922F", "#B33F32", "#6E9C6B", "#7A8FB0", "#C77D3C", "#8C6FA8"];

const state = {
  page: 1,
  pageSize: 25,
  timelineGroup: "day",
  filters: { q: "", departamento: "", municipio: "", modalidad: "", fechaInicio: "", fechaFin: "" },
  charts: {},
};

// ---------------------------------------------------------------------
// Utilidades
// ---------------------------------------------------------------------

const fmtCOP = (value) =>
  new Intl.NumberFormat("es-CO", { style: "currency", currency: "COP", maximumFractionDigits: 0 }).format(value || 0);

const fmtNum = (value) => new Intl.NumberFormat("es-CO").format(value || 0);

const fmtDate = (isoStr) => {
  if (!isoStr) return "—";
  const d = new Date(isoStr);
  if (isNaN(d)) return isoStr;
  return d.toLocaleDateString("es-CO", { year: "numeric", month: "short", day: "2-digit" });
};

async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Error ${res.status} consultando ${path}`);
  }
  return res.json();
}

function buildQuery(params) {
  const usp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== "" && v !== null && v !== undefined) usp.set(k, v);
  });
  const s = usp.toString();
  return s ? `?${s}` : "";
}

// ---------------------------------------------------------------------
// Estado de conexión
// ---------------------------------------------------------------------

async function checkHealth() {
  const el = document.getElementById("db-status");
  const text = document.getElementById("db-status-text");

  const origin = (typeof window !== "undefined" && window.location && window.location.origin) 
    ? window.location.origin 
    : "";
  
  const customApi = (typeof window !== "undefined" && window.SECOP_API_URL) 
    || (typeof localStorage !== "undefined" && localStorage.getItem("SECOP_API_URL"));

  const portsToTry = [];
  if (customApi) portsToTry.push(customApi);
  if (origin && origin.startsWith("http") && !origin.includes("localhost") && !origin.includes("127.0.0.1")) {
    portsToTry.push(origin);
  }
  portsToTry.push("http://127.0.0.1:8001", "http://127.0.0.1:8000", "http://127.0.0.1:8080");
  if (origin && !portsToTry.includes(origin)) portsToTry.push(origin);
  let connected = false;

  for (const base of portsToTry) {
    try {
      const res = await fetch(`${base}/api/health`);
      if (res.ok) {
        const health = await res.json();
        if (health.status === "ok") {
          API_BASE = base;
          connected = true;
          break;
        }
      }
    } catch (e) {
      // Sigue intentando en el siguiente puerto
    }
  }

  if (connected) {
    el.className = "masthead__status is-ok";
    text.textContent = "Conectado a secop.db";
  } else {
    el.className = "masthead__status is-error";
    text.textContent = "Sin conexión a la API — revisa que uvicorn esté corriendo";
    throw new Error("Base de datos no encontrada o API desconectada");
  }
}

// ---------------------------------------------------------------------
// Fichas resumen
// ---------------------------------------------------------------------

async function loadSummary() {
  const s = await apiGet("/api/stats/summary");
  document.getElementById("s-total").textContent = fmtNum(s.total_contratos);
  document.getElementById("s-valor").textContent = fmtCOP(s.valor_total);
  document.getElementById("s-entidades").textContent = fmtNum(s.num_entidades);
  document.getElementById("s-proveedores").textContent = fmtNum(s.num_proveedores);
}

// ---------------------------------------------------------------------
// Gráficas
// ---------------------------------------------------------------------

function upsertChart(key, ctx, config) {
  if (state.charts[key]) {
    state.charts[key].destroy();
  }
  state.charts[key] = new Chart(ctx, config);
}

const baseGridOptions = {
  scales: {
    x: { grid: { color: COLORS.gridLine }, ticks: { color: COLORS.slate, font: { family: "IBM Plex Mono", size: 10 } } },
    y: { grid: { color: COLORS.gridLine }, ticks: { color: COLORS.slate, font: { family: "IBM Plex Mono", size: 10 } } },
  },
  plugins: { legend: { display: false } },
};

async function loadTimeline() {
  const data = await apiGet(`/api/stats/timeline?group_by=${state.timelineGroup}`);
  const ctx = document.getElementById("chart-timeline");
  upsertChart("timeline", ctx, {
    type: "line",
    data: {
      labels: data.map((d) => d.periodo),
      datasets: [
        {
          label: "Contratos",
          data: data.map((d) => d.num_contratos),
          borderColor: COLORS.gold,
          backgroundColor: "rgba(185,146,47,0.15)",
          fill: true,
          tension: 0.25,
          pointRadius: 0,
          borderWidth: 2,
        },
      ],
    },
    options: {
      ...baseGridOptions,
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `${fmtNum(c.raw)} contratos` } } },
    },
  });
}

async function loadModalidades() {
  const data = await apiGet("/api/stats/modalidades");
  const ctx = document.getElementById("chart-modalidades");
  upsertChart("modalidades", ctx, {
    type: "doughnut",
    data: {
      labels: data.map((d) => d.modalidad),
      datasets: [{ data: data.map((d) => d.num_contratos), backgroundColor: CHART_PALETTE, borderColor: "#1B222C", borderWidth: 2 }],
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "bottom", labels: { color: COLORS.slate, font: { family: "IBM Plex Sans", size: 11 }, boxWidth: 12 } },
      },
    },
  });
}

async function loadTopEntidades() {
  const data = await apiGet("/api/stats/top-entidades?limit=8");
  const ctx = document.getElementById("chart-entidades");
  upsertChart("entidades", ctx, {
    type: "bar",
    data: {
      labels: data.map((d) => truncate(d.nombre, 22)),
      datasets: [{ data: data.map((d) => d.num_contratos), backgroundColor: COLORS.gold }],
    },
    options: { ...baseGridOptions, indexAxis: "y", maintainAspectRatio: false },
  });
}

async function loadTopProveedores() {
  const data = await apiGet("/api/stats/top-proveedores?limit=8");
  const ctx = document.getElementById("chart-proveedores");
  upsertChart("proveedores", ctx, {
    type: "bar",
    data: {
      labels: data.map((d) => truncate(d.nombre, 22)),
      datasets: [{ data: data.map((d) => d.num_contratos), backgroundColor: COLORS.rust }],
    },
    options: { ...baseGridOptions, indexAxis: "y", maintainAspectRatio: false },
  });
}

function truncate(str, n) {
  if (!str) return "Sin dato";
  return str.length > n ? str.slice(0, n - 1) + "…" : str;
}

// ---------------------------------------------------------------------
// Filtros (departamento / municipio / modalidad)
// ---------------------------------------------------------------------

async function loadFilterOptions() {
  const [departamentos, modalidades] = await Promise.all([
    apiGet("/api/departamentos"),
    apiGet("/api/stats/modalidades"),
  ]);

  const dSel = document.getElementById("f-departamento");
  departamentos.forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    dSel.appendChild(opt);
  });

  const mSel = document.getElementById("f-modalidad");
  modalidades.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.modalidad;
    opt.textContent = m.modalidad;
    mSel.appendChild(opt);
  });
}

async function loadMunicipios(departamento) {
  const munSel = document.getElementById("f-municipio");
  munSel.innerHTML = '<option value="">Todos</option>';
  const url = departamento ? `/api/municipios?departamento=${encodeURIComponent(departamento)}` : "/api/municipios";
  try {
    const municipios = await apiGet(url);
    municipios.forEach((m) => {
      const opt = document.createElement("option");
      opt.value = m;
      opt.textContent = m;
      munSel.appendChild(opt);
    });
  } catch (err) {
    console.error("Error cargando municipios:", err);
  }
}

// ---------------------------------------------------------------------
// Tabla de contratos
// ---------------------------------------------------------------------

async function loadTable() {
  const tbody = document.getElementById("table-body");
  tbody.innerHTML = `<tr><td colspan="7" class="empty">Cargando expedientes…</td></tr>`;

  const query = buildQuery({
    q: state.filters.q,
    departamento: state.filters.departamento,
    municipio: state.filters.municipio,
    modalidad: state.filters.modalidad,
    fecha_inicio: state.filters.fechaInicio,
    fecha_fin: state.filters.fechaFin,
    page: state.page,
    page_size: state.pageSize,
  });

  const data = await apiGet(`/api/contratos${query}`);

  document.getElementById("table-count").textContent = `${fmtNum(data.total)} expedientes encontrados`;
  document.getElementById("p-info").textContent = `Página ${data.page} de ${data.total_pages}`;
  document.getElementById("p-prev").disabled = data.page <= 1;
  document.getElementById("p-next").disabled = data.page >= data.total_pages;

  if (data.results.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">No se encontraron contratos con estos filtros.</td></tr>`;
    return;
  }

  tbody.innerHTML = data.results
    .map((r) => {
      const pillClass = r.modalidad_de_contratacion === "Contratación Directa" ? "pill pill--directa" : "pill";
      const link = r.urlproceso
        ? `<a class="link-out" href="${escapeAttr(r.urlproceso)}" target="_blank" rel="noopener">Ver proceso →</a>`
        : "";
      return `
        <tr>
          <td>${fmtDate(r.fecha_de_firma)}</td>
          <td>${escapeHtml(r.nombre_entidad) || "—"}</td>
          <td>${escapeHtml(r.proveedor_adjudicado) || "—"}</td>
          <td class="num">${fmtCOP(r.valor_del_contrato)}</td>
          <td><span class="${pillClass}">${escapeHtml(r.modalidad_de_contratacion) || "Sin dato"}</span></td>
          <td>${escapeHtml(r.estado_contrato) || "—"}</td>
          <td>${link}</td>
        </tr>`;
    })
    .join("");
}

function escapeHtml(str) {
  if (str == null) return "";
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(str) {
  return escapeHtml(str);
}

// ---------------------------------------------------------------------
// Eventos
// ---------------------------------------------------------------------

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function applyFilters(e) {
  if (e && typeof e.preventDefault === "function") {
    e.preventDefault();
  }
  state.filters.q = document.getElementById("f-search").value.trim();
  state.filters.departamento = document.getElementById("f-departamento").value;
  state.filters.municipio = document.getElementById("f-municipio").value;
  state.filters.modalidad = document.getElementById("f-modalidad").value;
  state.filters.fechaInicio = document.getElementById("f-fecha-inicio").value;
  state.filters.fechaFin = document.getElementById("f-fecha-fin").value;
  state.page = 1;
  loadTable();
}

function wireEvents() {
  const formEl = document.getElementById("filters-form");
  if (formEl) {
    formEl.addEventListener("submit", (e) => applyFilters(e));
  }

  const submitBtn = document.getElementById("f-submit");
  if (submitBtn) {
    submitBtn.addEventListener("click", (e) => applyFilters(e));
  }

  const scanBtn = document.getElementById("f-scan");
  if (scanBtn) {
    scanBtn.addEventListener("click", async () => {
      const scanBar = document.getElementById("scan-bar");
      const scanText = document.getElementById("scan-text");
      if (scanBar) scanBar.classList.add("is-active");
      if (scanText) scanText.textContent = "Sincronizando contratos recientes de SECOP II y analizando anomalías...";
      try {
        await fetch(`${API_BASE}/api/ingest/run?lookback_days=30`, { method: "POST" });
        setTimeout(async () => {
          if (scanBar) scanBar.classList.remove("is-active");
          await Promise.all([loadSummary(), loadTimeline(), loadTable(), loadTopEntidades(), loadTopProveedores()]);
        }, 8000);
      } catch (err) {
        if (scanBar) scanBar.classList.remove("is-active");
        alert("Error al iniciar actualización: " + err.message);
      }
    });
  }

  document.getElementById("f-search").addEventListener(
    "input",
    debounce(() => applyFilters(), 350)
  );

  document.getElementById("f-departamento").addEventListener("change", (e) => {
    const depto = document.getElementById("f-departamento").value;
    document.getElementById("f-municipio").value = "";
    state.filters.municipio = "";
    loadMunicipios(depto);
    applyFilters(e);
  });
  document.getElementById("f-municipio").addEventListener("change", (e) => applyFilters(e));
  document.getElementById("f-modalidad").addEventListener("change", (e) => applyFilters(e));
  document.getElementById("f-fecha-inicio").addEventListener("change", (e) => applyFilters(e));
  document.getElementById("f-fecha-fin").addEventListener("change", (e) => applyFilters(e));

  document.getElementById("f-clear").addEventListener("click", (e) => {
    if (e && typeof e.preventDefault === "function") e.preventDefault();
    state.filters = { q: "", departamento: "", municipio: "", modalidad: "", fechaInicio: "", fechaFin: "" };
    document.getElementById("f-search").value = "";
    document.getElementById("f-departamento").value = "";
    document.getElementById("f-municipio").innerHTML = '<option value="">Todos</option>';
    document.getElementById("f-modalidad").value = "";
    document.getElementById("f-fecha-inicio").value = "";
    document.getElementById("f-fecha-fin").value = "";
    state.page = 1;
    loadTable();
  });

  document.getElementById("p-prev").addEventListener("click", () => {
    if (state.page > 1) {
      state.page -= 1;
      loadTable();
    }
  });

  document.getElementById("p-next").addEventListener("click", () => {
    state.page += 1;
    loadTable();
  });

  document.querySelectorAll(".toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".toggle-btn").forEach((b) => b.classList.remove("is-active"));
      btn.classList.add("is-active");
      state.timelineGroup = btn.dataset.group;
      loadTimeline();
    });
  });
}

// ---------------------------------------------------------------------
// Arranque
// ---------------------------------------------------------------------

async function init() {
  wireEvents();
  await checkHealth();
  try {
    await Promise.all([
      loadSummary(),
      loadTimeline(),
      loadModalidades(),
      loadTopEntidades(),
      loadTopProveedores(),
      loadFilterOptions(),
      loadTable(),
    ]);
  } catch (err) {
    console.error(err);
  }
}

init();