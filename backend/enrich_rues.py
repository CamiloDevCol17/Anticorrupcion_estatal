"""
enrich_rues.py — Enriches contractor data from RUES (Registro Único Empresarial y Social)
Queries the public RUES API for each unique NIT/document found in the contracts DB.
Results are cached in the 'proveedores_enriquecidos' table to avoid repeated requests.

Usage:
    python enrich_rues.py                 # Enrich all new providers not yet in cache
    python enrich_rues.py --limit 500     # Process at most 500 providers
    python enrich_rues.py --reset         # Clear cache and re-enrich everything
"""

import sqlite3
import json
import time
import argparse
import re
from pathlib import Path
from datetime import datetime

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).parent / 'secop.db'
RUES_API_BASE = "https://www.rues.org.co/api"
BATCH_SIZE = 50      # Providers per batch
RATE_LIMIT_SECS = 1.2  # Seconds to wait between requests (respect rate limiting)

# ---------------------------------------------------------------------------
# DB Setup
# ---------------------------------------------------------------------------

def get_connection():
    return sqlite3.connect(str(DB_PATH))

def ensure_table():
    with get_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS proveedores_enriquecidos (
            documento_proveedor TEXT PRIMARY KEY,
            nombre TEXT,
            fecha_matricula TEXT,
            municipio_matricula TEXT,
            tipo_empresa TEXT,
            estado_matricula TEXT,
            consultado_at TEXT DEFAULT (datetime('now')),
            fuente TEXT DEFAULT 'RUES'
        )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prov_doc ON proveedores_enriquecidos(documento_proveedor)")
        conn.commit()

# ---------------------------------------------------------------------------
# RUES API Query
# ---------------------------------------------------------------------------

def query_rues(documento: str) -> dict:
    """
    Query RUES for a given NIT or document number.
    Returns a dict with empresa info or None if not found.
    
    RUES API Endpoint: GET /api/RM?nit={documento}
    """
    import urllib.request
    import urllib.error
    
    # Clean document: strip dots, hyphens, spaces, verification digit
    doc_clean = re.sub(r'[.\-\s]', '', str(documento))
    # Remove verification digit if it looks like NIT format (9+ digits)
    if len(doc_clean) >= 9 and doc_clean.isdigit():
        doc_query = doc_clean[:9]  # RUES uses 9-digit NIT without verification digit
    else:
        doc_query = doc_clean
    
    url = f"https://www.rues.org.co/RM?nit={doc_query}"
    
    try:
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (compatible; VigiaAnticorrupcion/1.0)',
                'Accept': 'text/html,application/xhtml+xml',
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
        
        # Parse relevant fields from HTML response
        result = _parse_rues_html(html, doc_query)
        return result
        
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {'found': False, 'error': 'not_found'}
        return {'found': False, 'error': f'http_{e.code}'}
    except Exception as e:
        return {'found': False, 'error': str(e)[:100]}


def _parse_rues_html(html: str, nit: str) -> dict:
    """Parse RUES HTML page to extract company registration data."""
    result = {'found': False, 'nit': nit}
    
    # Look for fecha de matricula
    fecha_match = re.search(
        r'[Ff]echa\s+[Dd]e?\s*[Mm]atr[íi]cula[:\s]*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{2}/[0-9]{2}/[0-9]{4})',
        html
    )
    if not fecha_match:
        # Try alternate format
        fecha_match = re.search(
            r'[Mm]atr[íi]cula[^\d]*(\d{1,2})[/-](\d{1,2})[/-](\d{4})',
            html
        )
        if fecha_match:
            d, m, y = fecha_match.group(1), fecha_match.group(2), fecha_match.group(3)
            result['fecha_matricula'] = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    else:
        raw_fecha = fecha_match.group(1)
        if '/' in raw_fecha:
            parts = raw_fecha.split('/')
            if len(parts[2]) == 4:
                result['fecha_matricula'] = f"{parts[2]}-{parts[1].zfill(2)}-{parts[0].zfill(2)}"
            else:
                result['fecha_matricula'] = raw_fecha
        else:
            result['fecha_matricula'] = raw_fecha
    
    # Municipio
    municipio_match = re.search(
        r'[Mm]unicipio[:\s]+([A-ZÁÉÍÓÚÑ][a-záéíóúñA-ZÁÉÍÓÚÑ\s]+?)[\n<]',
        html
    )
    if municipio_match:
        result['municipio_matricula'] = municipio_match.group(1).strip()
    
    # Tipo de organización
    tipo_match = re.search(
        r'[Tt]ipo\s+[Oo]rganizaci[oó]n[:\s]+([^\n<]+)',
        html
    )
    if tipo_match:
        result['tipo_empresa'] = tipo_match.group(1).strip()[:100]
    
    # Estado matrícula
    estado_match = re.search(
        r'[Ee]stado\s+[Mm]atr[íi]cula[:\s]+([^\n<]+)',
        html
    )
    if estado_match:
        result['estado_matricula'] = estado_match.group(1).strip()[:50]
    
    if 'fecha_matricula' in result:
        result['found'] = True
    
    return result


# ---------------------------------------------------------------------------
# Main enrichment logic
# ---------------------------------------------------------------------------

def enrich(limit=None, reset=False):
    ensure_table()
    
    with get_connection() as conn:
        if reset:
            conn.execute("DELETE FROM proveedores_enriquecidos")
            conn.commit()
            print("🗑️  Cache limpiado.")
        
        # Get unique documents NOT yet enriched (cedulas and NITs - digits only)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT DISTINCT c.documento_proveedor, c.proveedor_adjudicado
            FROM contratos c
            LEFT JOIN proveedores_enriquecidos pe ON c.documento_proveedor = pe.documento_proveedor
            WHERE pe.documento_proveedor IS NULL
              AND c.documento_proveedor IS NOT NULL
              AND c.documento_proveedor != ''
              AND c.tipodocproveedor NOT LIKE '%C%dula%'
            ORDER BY c.documento_proveedor
        """)
        
        pending = cursor.fetchall()
    
    if limit:
        pending = pending[:limit]
    
    total = len(pending)
    print(f"📋 {total} proveedores pendientes de enriquecimiento RUES")
    
    found_count = 0
    not_found_count = 0
    error_count = 0
    
    for i, row in enumerate(pending):
        doc = row['documento_proveedor']
        nombre = row['proveedor_adjudicado']
        
        print(f"  [{i+1}/{total}] Consultando RUES: {doc} ({nombre[:40] if nombre else '?'})... ", end='', flush=True)
        
        result = query_rues(doc)
        
        if result.get('found'):
            found_count += 1
            print(f"✅ Matrícula: {result.get('fecha_matricula')} | {result.get('municipio_matricula', '?')}")
        elif result.get('error') == 'not_found':
            not_found_count += 1
            print("— No encontrado")
        else:
            error_count += 1
            print(f"⚠️  Error: {result.get('error', 'desconocido')}")
        
        # Always save result to cache (even if not found)
        with get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO proveedores_enriquecidos
                    (documento_proveedor, nombre, fecha_matricula, municipio_matricula, tipo_empresa, estado_matricula, consultado_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                doc,
                nombre,
                result.get('fecha_matricula', 'No encontrado'),
                result.get('municipio_matricula', ''),
                result.get('tipo_empresa', ''),
                result.get('estado_matricula', ''),
            ))
            conn.commit()
        
        # Rate limiting
        time.sleep(RATE_LIMIT_SECS)
    
    print(f"\n✅ Enriquecimiento completado:")
    print(f"   Encontrados en RUES : {found_count}")
    print(f"   No encontrados      : {not_found_count}")
    print(f"   Errores             : {error_count}")
    print(f"\n💡 Ejecuta ahora el escaneo para activar la regla R14_EMPRESA_NUEVA:")
    print(f"   python -m uvicorn api:app --port 8001")
    print(f"   # Luego POST /api/scan/run")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Enriquece proveedores con datos de RUES')
    parser.add_argument('--limit', type=int, default=None, help='Máximo de proveedores a procesar')
    parser.add_argument('--reset', action='store_true', help='Limpiar caché y reiniciar')
    args = parser.parse_args()
    enrich(limit=args.limit, reset=args.reset)
