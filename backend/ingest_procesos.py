"""
ingest_procesos.py — Downloads and ingests the SECOP II Processes dataset
Source: datos.gov.co — SECOP II - Procesos de Contratación
Dataset ID: p6dx-8zbt  (Socrata Open Data API)

This dataset contains licitación window dates (publication vs. closing),
number of bidders, and process metadata that maps to our contracts via
the 'proceso_de_compra' field.

Usage:
    python ingest_procesos.py              # Download and ingest all available processes
    python ingest_procesos.py --limit 5000 # Only first 5000 records (for testing)
    python ingest_procesos.py --resume     # Skip records already in DB
"""

import sqlite3
import json
import urllib.request
import urllib.error
import time
import argparse
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / 'secop.db'

# Socrata SODA API - SECOP II Processes dataset
SECOP_PROCESOS_API = "https://www.datos.gov.co/resource/p6dx-8zbt.json"
PAGE_SIZE = 5000  # Max allowed by Socrata

FIELD_MAP = {
    # Socrata field → our column name
    'proceso_de_compra': 'proceso_de_compra',
    'nombre_entidad': 'nombre_entidad',
    'departamento': 'departamento',
    'ciudad_municipio': 'municipio',
    'modalidad_de_contratacion': 'modalidad',
    'estado_proceso': 'estado_proceso',
    'cuant_a_a_contratar': 'cuantia',
    'fecha_de_publicacion_del': 'fecha_publicacion',   # Publicacion del proceso
    'fecha_limite_de_recepcion': 'fecha_cierre',        # Fecha limite ofertas
    'numero_de_oferentes': 'numero_oferentes',
    'tipo_de_contrato': 'tipo_contrato',
    'codigo_proceso': 'codigo_proceso',
}

# ---------------------------------------------------------------------------
# DB Setup
# ---------------------------------------------------------------------------

def get_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

def ensure_table():
    with get_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS procesos (
            proceso_de_compra TEXT PRIMARY KEY,
            nombre_entidad TEXT,
            departamento TEXT,
            municipio TEXT,
            modalidad TEXT,
            estado_proceso TEXT,
            cuantia REAL,
            fecha_publicacion TEXT,
            fecha_cierre TEXT,
            numero_oferentes INTEGER,
            tipo_contrato TEXT,
            codigo_proceso TEXT,
            ingested_at TEXT DEFAULT (datetime('now'))
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_proc_compra ON procesos(proceso_de_compra)")
        conn.commit()
        print("✅ Tabla 'procesos' lista.")

# ---------------------------------------------------------------------------
# Download from Socrata API
# ---------------------------------------------------------------------------

def fetch_page(offset: int, limit: int) -> list:
    """Fetch one page of records from the Socrata API."""
    import urllib.parse
    params = urllib.parse.urlencode({
        '$limit': limit,
        '$offset': offset,
        '$order': 'id_del_proceso',
    })
    url = f"{SECOP_PROCESOS_API}?{params}"
    
    req = urllib.request.Request(
        url,
        headers={
            'Accept': 'application/json',
            'User-Agent': 'VigiaAnticorrupcion/2.0',
        }
    )
    
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))


def parse_record(rec: dict) -> dict | None:
    """Map a Socrata record to our DB schema."""
    try:
        row = {}
        
        # The join key: id_del_portafolio in processes = proceso_de_compra in contracts
        row['proceso_de_compra'] = rec.get('id_del_portafolio', rec.get('id_del_proceso', ''))
        if not row['proceso_de_compra']:
            return None
        
        row['nombre_entidad'] = rec.get('entidad', '')
        row['departamento'] = rec.get('departamento_entidad', '')
        row['municipio'] = rec.get('ciudad_entidad', '')
        row['modalidad'] = rec.get('modalidad_de_contratacion', '')
        row['estado_proceso'] = rec.get('estado_del_procedimiento', rec.get('estado_resumen', ''))
        
        # Cuantía
        cuantia_raw = rec.get('precio_base', rec.get('valor_total_adjudicacion', '0'))
        try:
            row['cuantia'] = float(str(cuantia_raw).replace(',', '').replace('$', '')) if cuantia_raw else None
        except:
            row['cuantia'] = None
        
        # Fechas clave para R15 - publication date and closing date
        # In SECOP II Procesos: fecha_de_publicacion_del is published, fecha_de_ultima_publicaci is updated
        # The "cierre" (closing) is not a direct field, but we can estimate from duracion + fecha_publicacion
        row['fecha_publicacion'] = rec.get('fecha_de_publicacion_del', '')
        # fecha_de_publicacion_fase_3 is often the offer deadline (fase 3 = presentación de oferta)
        row['fecha_cierre'] = rec.get('fecha_de_publicacion_fase_3', rec.get('fecha_de_ultima_publicaci', ''))
        
        # Normalize ISO dates
        for df in ('fecha_publicacion', 'fecha_cierre'):
            if row[df] and 'T' in str(row[df]):
                row[df] = str(row[df]).split('T')[0]
        
        # Número de oferentes (conteo_de_respuestas_a_ofertas)
        oferentes_raw = rec.get('conteo_de_respuestas_a_ofertas', rec.get('respuestas_al_procedimiento', None))
        try:
            row['numero_oferentes'] = int(float(str(oferentes_raw))) if oferentes_raw is not None else None
        except:
            row['numero_oferentes'] = None
        
        row['tipo_contrato'] = rec.get('tipo_de_contrato', '')
        row['codigo_proceso'] = rec.get('id_del_proceso', '')
        
        return row
    except Exception as e:
        return None


def ingest(limit_total=None, resume=False):
    ensure_table()
    
    with get_connection() as conn:
        existing_count = conn.execute("SELECT COUNT(*) FROM procesos").fetchone()[0]
    
    if resume and existing_count > 0:
        print(f"📊 {existing_count:,} procesos ya en BD. Continuando desde donde quedó...")
        start_offset = (existing_count // PAGE_SIZE) * PAGE_SIZE
    else:
        if not resume:
            with get_connection() as conn:
                conn.execute("DELETE FROM procesos")
                conn.commit()
            print("🗑️  Tabla 'procesos' limpiada.")
        start_offset = 0
    
    total_inserted = existing_count if resume else 0
    offset = start_offset
    
    print(f"🌐 Descargando dataset SECOP II Procesos desde datos.gov.co...")
    print(f"   API: {SECOP_PROCESOS_API}")
    
    while True:
        if limit_total and total_inserted >= limit_total:
            break
        
        page_limit = min(PAGE_SIZE, limit_total - total_inserted) if limit_total else PAGE_SIZE
        
        print(f"  📥 Página offset={offset} ({total_inserted:,} procesados)...", end='', flush=True)
        
        try:
            records = fetch_page(offset, page_limit)
        except Exception as e:
            print(f" ❌ Error: {e}")
            print("  Reintentando en 5 segundos...")
            time.sleep(5)
            try:
                records = fetch_page(offset, page_limit)
            except Exception as e2:
                print(f" ❌ Error definitivo: {e2}. Deteniendo ingesta.")
                break
        
        if not records:
            print(" ✅ Sin más registros. Ingesta completada.")
            break
        
        rows = [parse_record(r) for r in records]
        rows = [r for r in rows if r is not None]
        
        if rows:
            with get_connection() as conn:
                conn.executemany("""
                    INSERT OR REPLACE INTO procesos
                        (proceso_de_compra, nombre_entidad, departamento, municipio,
                         modalidad, estado_proceso, cuantia, fecha_publicacion,
                         fecha_cierre, numero_oferentes, tipo_contrato, codigo_proceso)
                    VALUES
                        (:proceso_de_compra, :nombre_entidad, :departamento, :municipio,
                         :modalidad, :estado_proceso, :cuantia, :fecha_publicacion,
                         :fecha_cierre, :numero_oferentes, :tipo_contrato, :codigo_proceso)
                """, rows)
                conn.commit()
        
        total_inserted += len(records)
        offset += len(records)
        print(f" ✅ +{len(rows)} procesos ({total_inserted:,} total)")
        
        if len(records) < page_limit:
            print("✅ Ingesta completada (última página).")
            break
        
        time.sleep(0.3)  # Small pause between pages
    
    # Final stats
    with get_connection() as conn:
        final_count = conn.execute("SELECT COUNT(*) FROM procesos").fetchone()[0]
        with_dates = conn.execute(
            "SELECT COUNT(*) FROM procesos WHERE fecha_publicacion != '' AND fecha_cierre != ''"
        ).fetchone()[0]
        
        # How many can be joined with contratos?
        joinable = conn.execute("""
            SELECT COUNT(DISTINCT c.proceso_de_compra)
            FROM contratos c
            JOIN procesos p ON c.proceso_de_compra = p.proceso_de_compra
        """).fetchone()[0]
    
    print(f"\n✅ Resultado final:")
    print(f"   Total procesos en BD   : {final_count:,}")
    print(f"   Con fechas pub/cierre  : {with_dates:,}")
    print(f"   Joinables con contratos: {joinable:,}")
    print(f"\n💡 Ejecuta el escaneo para activar R15_VENTANA_LICITACION_CORTA:")
    print(f"   POST http://127.0.0.1:8001/api/scan/run")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Ingesta de SECOP II Procesos')
    parser.add_argument('--limit', type=int, default=None, help='Límite de registros a descargar')
    parser.add_argument('--resume', action='store_true', help='Reanudar ingesta incompleta')
    args = parser.parse_args()
    ingest(limit_total=args.limit, resume=args.resume)
