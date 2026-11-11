#!/usr/bin/env python3
"""
query_stats.py
----------------
Consultas rápidas de verificación sobre la base SQLite poblada por
secop_ingest.py. Útil para confirmar que la ingesta funcionó y para tener
un primer vistazo a los datos antes de construir el motor de detección de
anomalías.

USO
---
    python query_stats.py
    python query_stats.py --db-path ./secop.db
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Estadísticas rápidas de la base SECOP local.")
    parser.add_argument("--db-path", default="secop.db", help="Ruta al archivo SQLite")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"No se encontró {db_path}. Corre secop_ingest.py primero.")
        return

    conn = sqlite3.connect(db_path)

    total = conn.execute("SELECT COUNT(*) FROM contratos").fetchone()[0]
    print(f"Total de contratos en la base: {total}\n")

    if total == 0:
        conn.close()
        return

    print("Estado de la ingesta:")
    for row in conn.execute("SELECT dataset_id, last_cutoff, last_run_at, records_last_run FROM ingest_state"):
        print(f"  dataset={row[0]}  ultimo_cutoff={row[1]}  ultima_corrida={row[2]}  registros_ultima_corrida={row[3]}")

    print("\nTop 10 entidades por número de contratos:")
    for row in conn.execute(
        """
        SELECT nombre_entidad, COUNT(*) AS n, SUM(valor_del_contrato) AS total_valor
        FROM contratos
        WHERE nombre_entidad IS NOT NULL
        GROUP BY nombre_entidad
        ORDER BY n DESC
        LIMIT 10
        """
    ):
        valor = f"${row[2]:,.0f}" if row[2] else "N/D"
        print(f"  {row[0]:<50} {row[1]:>6} contratos   valor total: {valor}")

    print("\nTop 10 proveedores por número de contratos:")
    for row in conn.execute(
        """
        SELECT proveedor_adjudicado, COUNT(*) AS n, SUM(valor_del_contrato) AS total_valor
        FROM contratos
        WHERE proveedor_adjudicado IS NOT NULL
        GROUP BY proveedor_adjudicado
        ORDER BY n DESC
        LIMIT 10
        """
    ):
        valor = f"${row[2]:,.0f}" if row[2] else "N/D"
        print(f"  {row[0]:<50} {row[1]:>6} contratos   valor total: {valor}")

    print("\nDistribución por modalidad de contratación:")
    for row in conn.execute(
        """
        SELECT modalidad_de_contratacion, COUNT(*) AS n
        FROM contratos
        GROUP BY modalidad_de_contratacion
        ORDER BY n DESC
        """
    ):
        print(f"  {row[0] or 'N/D':<40} {row[1]:>6}")

    conn.close()


if __name__ == "__main__":
    main()