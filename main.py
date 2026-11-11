#!/usr/bin/env python3
"""
secop_ingest.py
----------------
Prototipo de ingesta de datos de SECOP II (Contratos Electrónicos) desde la
API abierta de Socrata (datos.gov.co) hacia una base de datos local SQLite.

Diseñado para correr de forma incremental: en cada ejecución solo trae los
registros nuevos o modificados desde la última corrida exitosa.

USO BÁSICO
----------
    # Primera corrida (trae los últimos 90 días por defecto)
    python secop_ingest.py

    # Corrida incremental (usa automáticamente la última fecha guardada)
    python secop_ingest.py

    # Forzar un rango de fechas específico
    python secop_ingest.py --since 2026-01-01

    # Filtrar por departamento
    python secop_ingest.py --departamento "Antioquia"

    # Usar un dataset distinto (ej. Proveedores Registrados)
    python secop_ingest.py --dataset-id sqpp-4gyj

CONFIGURACIÓN
--------------
Socrata autentica con un par Key ID / Key Secret (funcionan como usuario y
contraseña vía HTTP Basic Auth). Se crean en:
https://evergreen.data.socrata.com/login  (o el portal de developer settings
del dominio, ej. https://datos.gov.co)  → Developer Settings → Create New API Key

Expórtalos como variables de entorno (recomendado, nunca los pegues en el código):
    export SECOP_KEY_ID="tu_key_id"
    export SECOP_KEY_SECRET="tu_key_secret"

El script funciona sin credenciales, pero con límites de tasa mucho más bajos
(las peticiones se cuentan por IP compartida en vez de por cuenta).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import requests

# ---------------------------------------------------------------------------
# Configuración por defecto
# ---------------------------------------------------------------------------

# Dataset "SECOP II - Contratos Electrónicos" en datos.gov.co (Socrata)
DEFAULT_DATASET_ID = "jbjy-vk9h"
SOCRATA_BASE_URL = "https://www.datos.gov.co/resource"

DB_PATH = Path(__file__).parent / "secop.db"
STATE_TABLE = "ingest_state"
CONTRATOS_TABLE = "contratos"

# Campo de fecha usado para la ingesta incremental. SECOP II expone varios
# campos de fecha (fecha_de_firma, fecha_de_inicio_del_contrato, etc.) — se
# usa fecha_de_firma como referencia por ser el momento en que el contrato
# se formaliza. Ajusta según lo que necesites monitorear.
DATE_FIELD = "fecha_de_firma"

PAGE_SIZE = 1000          # registros por página (máx recomendado por Socrata)
MAX_RETRIES = 5
RETRY_BACKOFF_SECONDS = 2
REQUEST_TIMEOUT = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("secop_ingest")


# ---------------------------------------------------------------------------
# Capa de base de datos
# ---------------------------------------------------------------------------

def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Crea las tablas si no existen.

    La tabla `contratos` guarda el registro crudo como JSON (columna `raw_json`)
    además de un puñado de columnas indexadas para consultas rápidas. Guardar
    el JSON completo evita perder campos cuando SECOP agrega columnas nuevas,
    y permite que el motor de detección de anomalías (siguiente etapa del
    proyecto) trabaje directamente sobre el dato completo.
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CONTRATOS_TABLE} (
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
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
            dataset_id TEXT PRIMARY KEY,
            last_cutoff TEXT NOT NULL,
            last_run_at TEXT NOT NULL,
            records_last_run INTEGER
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_entidad ON {CONTRATOS_TABLE}(nombre_entidad)"
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_proveedor ON {CONTRATOS_TABLE}(proveedor_adjudicado)"
    )
    conn.commit()


def get_last_cutoff(conn: sqlite3.Connection, dataset_id: str) -> str | None:
    row = conn.execute(
        f"SELECT last_cutoff FROM {STATE_TABLE} WHERE dataset_id = ?",
        (dataset_id,),
    ).fetchone()
    return row[0] if row else None


def set_last_cutoff(
    conn: sqlite3.Connection, dataset_id: str, cutoff: str, records: int
) -> None:
    conn.execute(
        f"""
        INSERT INTO {STATE_TABLE} (dataset_id, last_cutoff, last_run_at, records_last_run)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(dataset_id) DO UPDATE SET
            last_cutoff = excluded.last_cutoff,
            last_run_at = excluded.last_run_at,
            records_last_run = excluded.records_last_run
        """,
        (dataset_id, cutoff, datetime.utcnow().isoformat(), records),
    )
    conn.commit()


def upsert_contrato(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    """Inserta o actualiza un contrato. Usa .get() en todos los campos porque
    SECOP no siempre garantiza que todas las columnas vengan pobladas."""
    conn.execute(
        f"""
        INSERT INTO {CONTRATOS_TABLE} (
            id_contrato, nombre_entidad, departamento, proveedor_adjudicado,
            valor_del_contrato, fecha_de_firma, modalidad_de_contratacion,
            estado_contrato, urlproceso, raw_json, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id_contrato) DO UPDATE SET
            nombre_entidad = excluded.nombre_entidad,
            departamento = excluded.departamento,
            proveedor_adjudicado = excluded.proveedor_adjudicado,
            valor_del_contrato = excluded.valor_del_contrato,
            fecha_de_firma = excluded.fecha_de_firma,
            modalidad_de_contratacion = excluded.modalidad_de_contratacion,
            estado_contrato = excluded.estado_contrato,
            urlproceso = excluded.urlproceso,
            raw_json = excluded.raw_json,
            ingested_at = excluded.ingested_at
        """,
        (
            record.get("id_contrato"),
            record.get("nombre_entidad"),
            record.get("departamento"),
            record.get("proveedor_adjudicado"),
            _safe_float(record.get("valor_del_contrato")),
            record.get(DATE_FIELD),
            record.get("modalidad_de_contratacion"),
            record.get("estado_contrato"),
            _safe_str(record.get("urlproceso")),
            json.dumps(record, ensure_ascii=False),
            datetime.utcnow().isoformat(),
        ),
    )


def _safe_str(value: Any) -> str | None:
    """Convierte a texto de forma segura. Algunas columnas de Socrata (tipo
    'url', como urlproceso) vienen como objeto JSON en vez de string, ej.
    {"url": "https://...", "description": "..."} — SQLite no acepta dicts
    como parámetro, así que aquí extraemos el string útil."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("url") or value.get("description") or json.dumps(value, ensure_ascii=False)
    return str(value)


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Cliente de la API de Socrata
# ---------------------------------------------------------------------------

class SocrataClient:
    """Cliente mínimo para la API SoQL de Socrata, con paginación y reintentos.

    No depende de la librería `sodapy` para mantener el prototipo con el
    mínimo de dependencias externas (solo `requests`).
    """

    def __init__(
        self,
        dataset_id: str,
        key_id: str | None = None,
        key_secret: str | None = None,
    ):
        self.dataset_id = dataset_id
        self.url = f"{SOCRATA_BASE_URL}/{dataset_id}.json"
        self.auth = (key_id, key_secret) if key_id and key_secret else None
        if not self.auth:
            log.warning(
                "Corriendo sin credenciales (SECOP_KEY_ID/SECOP_KEY_SECRET) — "
                "la API aplicará límites de tasa más estrictos por IP compartida. "
                "Se recomienda crear un API Key gratuito en Socrata."
            )

    def fetch_pages(
        self, where_clause: str, order_by: str = DATE_FIELD
    ) -> Iterator[list[dict[str, Any]]]:
        """Generador que produce páginas de resultados (listas de dicts)
        aplicando el filtro SoQL `where_clause`, ordenado y paginado con
        $limit/$offset hasta agotar los resultados."""
        offset = 0
        while True:
            params = {
                "$where": where_clause,
                "$order": f"{order_by} ASC",
                "$limit": PAGE_SIZE,
                "$offset": offset,
            }
            page = self._get_with_retries(params)
            if not page:
                break
            yield page
            offset += len(page)
            if len(page) < PAGE_SIZE:
                break  # última página

    def _get_with_retries(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(
                    self.url,
                    params=params,
                    auth=self.auth,
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code == 429:
                    wait = RETRY_BACKOFF_SECONDS * attempt
                    log.warning("Rate limited (429). Esperando %ss...", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                wait = RETRY_BACKOFF_SECONDS * attempt
                log.warning(
                    "Error de red (intento %s/%s): %s. Reintentando en %ss...",
                    attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
        raise RuntimeError(
            f"No se pudo consultar la API tras {MAX_RETRIES} intentos"
        ) from last_exc


# ---------------------------------------------------------------------------
# Lógica principal de ingesta
# ---------------------------------------------------------------------------

def build_where_clause(since: str, departamento: str | None) -> str:
    """Construye la cláusula $where de SoQL. Escapa comillas simples de forma
    básica para el filtro de departamento (evita romper la sintaxis SoQL)."""
    clauses = [f"{DATE_FIELD} > '{since}'"]
    if departamento:
        depto_escaped = departamento.replace("'", "''")
        clauses.append(f"departamento = '{depto_escaped}'")
    return " AND ".join(clauses)


def run_ingest(
    dataset_id: str,
    since: str | None,
    departamento: str | None,
    key_id: str | None,
    key_secret: str | None,
    db_path: Path,
    default_lookback_days: int,
) -> None:
    conn = get_connection(db_path)
    init_db(conn)

    if since is None:
        since = get_last_cutoff(conn, dataset_id)
    if since is None:
        # Primera corrida: por defecto trae los últimos N días para no
        # descargar el histórico completo (millones de registros) sin querer.
        since_dt = datetime.utcnow() - timedelta(days=default_lookback_days)
        since = since_dt.strftime("%Y-%m-%dT00:00:00")
        log.info(
            "No hay corrida previa. Usando ventana inicial de %s días (desde %s).",
            default_lookback_days, since,
        )
    else:
        log.info("Ingesta incremental desde %s", since)

    where_clause = build_where_clause(since, departamento)
    log.info("Filtro SoQL: %s", where_clause)

    client = SocrataClient(dataset_id, key_id, key_secret)

    total = 0
    max_date_seen = since
    try:
        for page_num, page in enumerate(client.fetch_pages(where_clause), start=1):
            for record in page:
                upsert_contrato(conn, record)
                record_date = record.get(DATE_FIELD)
                if record_date and record_date > max_date_seen:
                    max_date_seen = record_date
            conn.commit()
            total += len(page)
            log.info("Página %s: %s registros procesados (total: %s)", page_num, len(page), total)
    except Exception:
        log.exception("La ingesta falló a mitad de camino. No se actualiza el cutoff.")
        conn.close()
        raise

    if total > 0:
        set_last_cutoff(conn, dataset_id, max_date_seen, total)
        log.info(
            "Ingesta completa: %s registros nuevos/actualizados. Próximo cutoff: %s",
            total, max_date_seen,
        )
    else:
        log.info("No hay registros nuevos desde %s.", since)

    conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingesta incremental de datos de SECOP II desde la API de Socrata."
    )
    parser.add_argument(
        "--dataset-id",
        default=DEFAULT_DATASET_ID,
        help=f"ID del dataset en datos.gov.co (default: {DEFAULT_DATASET_ID} = SECOP II Contratos Electrónicos)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Fecha ISO (YYYY-MM-DD) desde la cual traer registros. Si no se indica, usa el último cutoff guardado.",
    )
    parser.add_argument(
        "--departamento",
        default=None,
        help='Filtrar por departamento, ej. "Antioquia". Opcional.',
    )
    parser.add_argument(
        "--key-id",
        default=os.environ.get("SECOP_KEY_ID"),
        help="Key ID de Socrata (usuario de Basic Auth). También vía variable de entorno SECOP_KEY_ID.",
    )
    parser.add_argument(
        "--key-secret",
        default=os.environ.get("SECOP_KEY_SECRET"),
        help="Key Secret de Socrata (contraseña de Basic Auth). También vía variable de entorno SECOP_KEY_SECRET.",
    )
    parser.add_argument(
        "--db-path",
        default=str(DB_PATH),
        help=f"Ruta al archivo SQLite (default: {DB_PATH})",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        help="Días hacia atrás a traer en la PRIMERA corrida si no hay --since ni cutoff previo (default: 90)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_ingest(
            dataset_id=args.dataset_id,
            since=args.since,
            departamento=args.departamento,
            key_id=args.key_id,
            key_secret=args.key_secret,
            db_path=Path(args.db_path),
            default_lookback_days=args.lookback_days,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("Ingesta abortada: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())