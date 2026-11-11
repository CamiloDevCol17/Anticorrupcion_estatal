#!/usr/bin/env python3
"""
api.py
------
API REST (FastAPI) que expone los datos ingeridos de SECOP II (tabla
`contratos` en secop.db) para consumo del dashboard frontend.

USO
---
    uvicorn api:app --reload --port 8000

La API busca secop.db en, en este orden:
  1. La ruta indicada por la variable de entorno SECOP_DB_PATH
  2. La carpeta actual (./secop.db)
  3. Un nivel arriba (../secop.db) — útil si corres esto desde dashboard/backend/
     y secop.db vive en la raíz del proyecto de ingesta.

Documentación interactiva automática una vez corriendo: http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from irregularity_engine import IrregularityEngine

# ---------------------------------------------------------------------------
# Localización de la base de datos
# ---------------------------------------------------------------------------

def _resolve_db_path() -> Path:
    env_path = os.environ.get("SECOP_DB_PATH")
    if env_path:
        return Path(env_path)
    
    # Busca secop.db en la misma carpeta que api.py (backend/)
    file_dir_db = Path(__file__).parent / "secop.db"
    if file_dir_db.exists():
        return file_dir_db
        
    here = Path("secop.db")
    if here.exists():
        return here
    parent = Path("../secop.db")
    if parent.exists():
        return parent
    return file_dir_db


DB_PATH = _resolve_db_path()
irregularity_engine = IrregularityEngine(DB_PATH)

app = FastAPI(title="SECOP Vigía API", version="0.1.0")

# CORS abierto para desarrollo local. Si despliegas esto públicamente,
# restringe allow_origins a los dominios reales del frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _init_db_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
    CREATE TABLE IF NOT EXISTS contratos (
        id_contrato TEXT PRIMARY KEY,
        nombre_entidad TEXT,
        departamento TEXT,
        proveedor_adjudicado TEXT,
        valor_del_contrato REAL,
        fecha_de_firma TEXT,
        modalidad_de_contratacion TEXT,
        estado_contrato TEXT,
        urlproceso TEXT,
        raw_json TEXT NOT NULL,
        ingested_at TEXT NOT NULL
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS ingest_state (
        dataset_id TEXT PRIMARY KEY,
        last_cutoff TEXT NOT NULL,
        last_run_at TEXT NOT NULL,
        records_last_run INTEGER
    )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entidad ON contratos(nombre_entidad)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_proveedor ON contratos(proveedor_adjudicado)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fecha_firma ON contratos(fecha_de_firma)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_departamento ON contratos(departamento)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_modalidad ON contratos(modalidad_de_contratacion)")
    conn.commit()


def get_db() -> sqlite3.Connection:
    needs_init = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if needs_init:
        _init_db_schema(conn)
    return conn


def _background_initial_ingest() -> None:
    try:
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) FROM contratos").fetchone()[0]
        conn.close()
        if count == 0:
            print("📦 Base de datos vacía. Iniciando descarga automática inicial de contratos SECOP...")
            import main
            main.run_ingest(
                dataset_id=main.DEFAULT_DATASET_ID,
                since=None,
                departamento=None,
                key_id=os.environ.get("SECOP_KEY_ID"),
                key_secret=os.environ.get("SECOP_KEY_SECRET"),
                db_path=DB_PATH,
                default_lookback_days=30,
            )
            print("🔍 Ejecutando escaneo inicial de irregularidades...")
            irregularity_engine.run_full_scan()
            print("✅ Ingesta y escaneo inicial completados con éxito.")
    except Exception as e:
        print(f"⚠️ Nota sobre descarga inicial: {e}")


@app.on_event("startup")
def startup_event() -> None:
    conn = get_db()
    _init_db_schema(conn)
    conn.close()
    threading.Thread(target=_background_initial_ingest, daemon=True).start()


# ---------------------------------------------------------------------------
# Helper de filtrado
# ---------------------------------------------------------------------------

def _build_where_clause(
    q: str | None = None,
    departamento: str | None = None,
    municipio: str | None = None,
    modalidad: str | None = None,
    fecha_inicio: str | None = None,
    fecha_fin: str | None = None,
    extra_conditions: list[str] | None = None,
) -> tuple[str, list[Any]]:
    where = []
    params: list[Any] = []
    if extra_conditions:
        where.extend(extra_conditions)
    if q:
        where.append("(nombre_entidad LIKE ? OR proveedor_adjudicado LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    if departamento:
        where.append("departamento = ?")
        params.append(departamento)
    if municipio:
        where.append("municipio = ?")
        params.append(municipio)
    if modalidad:
        where.append("modalidad_de_contratacion = ?")
        params.append(modalidad)
    if fecha_inicio:
        where.append("fecha_de_firma >= ?")
        params.append(fecha_inicio)
    if fecha_fin:
        where.append("fecha_de_firma <= ?")
        params.append(f"{fecha_fin}T23:59:59")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return where_sql, params


# ---------------------------------------------------------------------------
# Endpoints de Estadísticas (Reactivos a los Filtros)
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    exists = DB_PATH.exists()
    return {"status": "ok" if exists else "db_not_found", "db_path": str(DB_PATH.resolve())}


@app.get("/api/stats/summary")
def stats_summary(
    q: str | None = Query(None),
    departamento: str | None = None,
    municipio: str | None = None,
    modalidad: str | None = None,
    fecha_inicio: str | None = Query(None),
    fecha_fin: str | None = Query(None),
) -> dict[str, Any]:
    conn = get_db()
    where_sql, params = _build_where_clause(q, departamento, municipio, modalidad, fecha_inicio, fecha_fin)
    row = conn.execute(
        f"""
        SELECT
            COUNT(*) AS total_contratos,
            COALESCE(SUM(valor_del_contrato), 0) AS valor_total,
            COUNT(DISTINCT nombre_entidad) AS num_entidades,
            COUNT(DISTINCT proveedor_adjudicado) AS num_proveedores,
            MIN(fecha_de_firma) AS fecha_min,
            MAX(fecha_de_firma) AS fecha_max
        FROM contratos {where_sql}
        """,
        params,
    ).fetchone()
    conn.close()
    return dict(row)


@app.get("/api/stats/top-entidades")
def top_entidades(
    q: str | None = Query(None),
    departamento: str | None = None,
    municipio: str | None = None,
    modalidad: str | None = None,
    fecha_inicio: str | None = Query(None),
    fecha_fin: str | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
) -> list[dict[str, Any]]:
    conn = get_db()
    where_sql, params = _build_where_clause(
        q, departamento, municipio, modalidad, fecha_inicio, fecha_fin, extra_conditions=["nombre_entidad IS NOT NULL"]
    )
    rows = conn.execute(
        f"""
        SELECT nombre_entidad AS nombre, COUNT(*) AS num_contratos,
               COALESCE(SUM(valor_del_contrato), 0) AS valor_total
        FROM contratos {where_sql}
        GROUP BY nombre_entidad
        ORDER BY num_contratos DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/stats/top-proveedores")
def top_proveedores(
    q: str | None = Query(None),
    departamento: str | None = None,
    municipio: str | None = None,
    modalidad: str | None = None,
    fecha_inicio: str | None = Query(None),
    fecha_fin: str | None = Query(None),
    limit: int = Query(10, ge=1, le=100),
) -> list[dict[str, Any]]:
    conn = get_db()
    where_sql, params = _build_where_clause(
        q, departamento, municipio, modalidad, fecha_inicio, fecha_fin, extra_conditions=["proveedor_adjudicado IS NOT NULL"]
    )
    rows = conn.execute(
        f"""
        SELECT proveedor_adjudicado AS nombre, COUNT(*) AS num_contratos,
               COALESCE(SUM(valor_del_contrato), 0) AS valor_total
        FROM contratos {where_sql}
        GROUP BY proveedor_adjudicado
        ORDER BY num_contratos DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/stats/modalidades")
def stats_modalidades(
    q: str | None = Query(None),
    departamento: str | None = None,
    municipio: str | None = None,
    modalidad: str | None = None,
    fecha_inicio: str | None = Query(None),
    fecha_fin: str | None = Query(None),
) -> list[dict[str, Any]]:
    conn = get_db()
    where_sql, params = _build_where_clause(q, departamento, municipio, modalidad, fecha_inicio, fecha_fin)
    rows = conn.execute(
        f"""
        SELECT COALESCE(modalidad_de_contratacion, 'Sin dato') AS modalidad,
               COUNT(*) AS num_contratos
        FROM contratos {where_sql}
        GROUP BY modalidad
        ORDER BY num_contratos DESC
        """,
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/stats/timeline")
def stats_timeline(
    group_by: str = Query("day", pattern="^(day|month)$"),
    q: str | None = Query(None),
    departamento: str | None = None,
    municipio: str | None = None,
    modalidad: str | None = None,
    fecha_inicio: str | None = Query(None),
    fecha_fin: str | None = Query(None),
) -> list[dict[str, Any]]:
    """Serie de tiempo de contratos firmados, agrupada por día o por mes."""
    fmt = "%Y-%m-%d" if group_by == "day" else "%Y-%m"
    conn = get_db()
    where_sql, params = _build_where_clause(
        q, departamento, municipio, modalidad, fecha_inicio, fecha_fin, extra_conditions=["fecha_de_firma IS NOT NULL"]
    )
    rows = conn.execute(
        f"""
        SELECT strftime('{fmt}', fecha_de_firma) AS periodo,
               COUNT(*) AS num_contratos,
               COALESCE(SUM(valor_del_contrato), 0) AS valor_total
        FROM contratos {where_sql}
        GROUP BY periodo
        ORDER BY periodo ASC
        """,
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/contratos")
def list_contratos(
    q: str | None = Query(None, description="Búsqueda libre en entidad o proveedor"),
    departamento: str | None = None,
    municipio: str | None = None,
    modalidad: str | None = None,
    fecha_inicio: str | None = Query(None, description="Fecha desde YYYY-MM-DD"),
    fecha_fin: str | None = Query(None, description="Fecha hasta YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> dict[str, Any]:
    conn = get_db()

    where_sql, params = _build_where_clause(q, departamento, municipio, modalidad, fecha_inicio, fecha_fin)

    total = conn.execute(
        f"SELECT COUNT(*) FROM contratos {where_sql}", params
    ).fetchone()[0]

    offset = (page - 1) * page_size
    rows = conn.execute(
        f"""
        SELECT c.id_contrato, c.nombre_entidad, c.departamento, c.proveedor_adjudicado,
               c.valor_del_contrato, c.fecha_de_firma, c.modalidad_de_contratacion,
               c.estado_contrato, c.urlproceso,
               COALESCE(rs.total_score, 0) as risk_score,
               COALESCE(rs.risk_level, 'bajo') as risk_level,
               COALESCE(rs.alert_count, 0) as alert_count
        FROM contratos c
        LEFT JOIN risk_scores rs ON c.id_contrato = rs.id_contrato
        {where_sql}
        ORDER BY c.fecha_de_firma DESC
        LIMIT ? OFFSET ?
        """,
        (*params, page_size, offset),
    ).fetchall()
    conn.close()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, -(-total // page_size)),  # ceil division
        "results": [dict(r) for r in rows],
    }


@app.get("/api/contratos/{id_contrato}")
def get_contrato(id_contrato: str) -> dict[str, Any]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM contratos WHERE id_contrato = ?", (id_contrato,)
    ).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Contrato no encontrado")
    result = dict(row)
    # Adjunta también el registro crudo completo, parseado, para inspección
    try:
        result["raw"] = json.loads(result.get("raw_json") or "{}")
    except json.JSONDecodeError:
        result["raw"] = None
    return result


@app.get("/api/departamentos")
def list_departamentos() -> list[str]:
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT departamento FROM contratos WHERE departamento IS NOT NULL ORDER BY departamento"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


@app.get("/api/municipios")
def list_municipios(departamento: str | None = None) -> list[str]:
    conn = get_db()
    if departamento:
        rows = conn.execute(
            "SELECT DISTINCT municipio FROM contratos WHERE municipio IS NOT NULL AND departamento = ? ORDER BY municipio",
            (departamento,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT municipio FROM contratos WHERE municipio IS NOT NULL ORDER BY municipio"
        ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Endpoints de irregularidades
# ---------------------------------------------------------------------------

@app.post("/api/scan/run")
def run_scan() -> dict[str, Any]:
    return irregularity_engine.run_full_scan()


@app.post("/api/ingest/run")
def trigger_ingest(lookback_days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    """Descarga contratos recientes de SECOP II en segundo plano y ejecuta el escaneo."""
    def _do_ingest():
        try:
            import main
            main.run_ingest(
                dataset_id=main.DEFAULT_DATASET_ID,
                since=None,
                departamento=None,
                key_id=os.environ.get("SECOP_KEY_ID"),
                key_secret=os.environ.get("SECOP_KEY_SECRET"),
                db_path=DB_PATH,
                default_lookback_days=lookback_days,
            )
            irregularity_engine.run_full_scan()
        except Exception as e:
            print(f"Error en ingesta manual: {e}")

    threading.Thread(target=_do_ingest, daemon=True).start()
    return {
        "status": "started",
        "message": f"Ingesta en segundo plano iniciada para los últimos {lookback_days} días.",
    }


@app.get("/api/stats/irregularidades")
def stats_irregularidades(
    q: str | None = Query(None),
    departamento: str | None = None,
    municipio: str | None = None,
    modalidad: str | None = None,
    fecha_inicio: str | None = Query(None),
    fecha_fin: str | None = Query(None),
) -> dict[str, Any]:
    where_sql, params = _build_where_clause(q, departamento, municipio, modalidad, fecha_inicio, fecha_fin)
    if not where_sql:
        return irregularity_engine.get_summary()

    conn = get_db()
    row_alerts = conn.execute(
        f"""
        SELECT COUNT(a.id), COUNT(DISTINCT a.id_contrato)
        FROM alertas a
        JOIN contratos c ON a.id_contrato = c.id_contrato
        {where_sql}
        """,
        params,
    ).fetchone()

    rows_risk = conn.execute(
        f"""
        SELECT rs.risk_level, COUNT(*)
        FROM risk_scores rs
        JOIN contratos c ON rs.id_contrato = c.id_contrato
        {where_sql}
        GROUP BY rs.risk_level
        """,
        params,
    ).fetchall()
    conn.close()

    return {
        "total_alerts": row_alerts[0] if row_alerts else 0,
        "contracts_with_alerts": row_alerts[1] if row_alerts else 0,
        "by_risk_level": {r[0]: r[1] for r in rows_risk},
    }


@app.get("/api/contratos/{id_contrato}/alertas")
def get_contract_alerts(id_contrato: str) -> dict[str, Any]:
    result = irregularity_engine.get_contract_alerts(id_contrato)
    # Also fetch contract basic info including proveedor enrichment
    conn = get_db()
    row = conn.execute(
        "SELECT nombre_entidad, proveedor_adjudicado, valor_del_contrato, fecha_de_firma, modalidad_de_contratacion, documento_proveedor, proceso_de_compra FROM contratos WHERE id_contrato = ?",
        (id_contrato,),
    ).fetchone()
    conn.close()
    contrato_info = dict(row) if row else {}

    # Enrich with RUES data if available
    rues_info = None
    if contrato_info.get("documento_proveedor"):
        try:
            conn2 = get_db()
            rues_row = conn2.execute(
                "SELECT fecha_matricula, municipio_matricula, tipo_empresa, estado_matricula FROM proveedores_enriquecidos WHERE documento_proveedor = ?",
                (contrato_info["documento_proveedor"],)
            ).fetchone()
            conn2.close()
            if rues_row:
                rues_info = dict(rues_row)
        except Exception:
            pass

    return {
        "id_contrato": id_contrato,
        "contrato": contrato_info,
        "risk_score": result["risk_score"]["total_score"] if result["risk_score"] else 0,
        "risk_level": result["risk_score"]["risk_level"] if result["risk_score"] else "bajo",
        "alertas": result["alerts"],
        "rues": rues_info,
    }


@app.get("/api/enriquecimiento/status")
def enrichment_status() -> dict[str, Any]:
    """Returns status of RUES enrichment and SECOP Processes ingestion."""
    conn = get_db()
    status: dict[str, Any] = {}

    # RUES enrichment
    try:
        rues_row = conn.execute("SELECT COUNT(*) FROM proveedores_enriquecidos").fetchone()
        found_row = conn.execute(
            "SELECT COUNT(*) FROM proveedores_enriquecidos WHERE fecha_matricula != 'No encontrado' AND fecha_matricula != ''"
        ).fetchone()
        total_providers = conn.execute("SELECT COUNT(DISTINCT documento_proveedor) FROM contratos").fetchone()
        status["rues"] = {
            "enriched": rues_row[0] if rues_row else 0,
            "found": found_row[0] if found_row else 0,
            "total_providers": total_providers[0] if total_providers else 0,
        }
    except Exception:
        status["rues"] = {"enriched": 0, "found": 0, "total_providers": 0}

    # SECOP Procesos ingestion
    try:
        proc_row = conn.execute("SELECT COUNT(*) FROM procesos").fetchone()
        proc_dates = conn.execute(
            "SELECT COUNT(*) FROM procesos WHERE fecha_publicacion != '' AND fecha_cierre != ''"
        ).fetchone()
        joinable = conn.execute("""
            SELECT COUNT(DISTINCT c.proceso_de_compra)
            FROM contratos c JOIN procesos p ON c.proceso_de_compra = p.proceso_de_compra
        """).fetchone()
        status["procesos"] = {
            "ingested": proc_row[0] if proc_row else 0,
            "with_dates": proc_dates[0] if proc_dates else 0,
            "joinable_with_contracts": joinable[0] if joinable else 0,
        }
    except Exception:
        status["procesos"] = {"ingested": 0, "with_dates": 0, "joinable_with_contracts": 0}

    conn.close()
    return status



# ---------------------------------------------------------------------------
# Frontend estático — se monta al final para no interceptar rutas /api/*
# ---------------------------------------------------------------------------
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

if FRONTEND_DIR.exists():
    from starlette.responses import FileResponse as StarletteFileResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    # Servir archivos estáticos (css, js) bajo /static/
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static-files")

    @app.get("/")
    def serve_index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/{full_path:path}")
    def serve_frontend_files(full_path: str) -> FileResponse:
        file_path = FRONTEND_DIR / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        # Si no existe el archivo, devolver index.html (SPA fallback)
        return FileResponse(FRONTEND_DIR / "index.html")


