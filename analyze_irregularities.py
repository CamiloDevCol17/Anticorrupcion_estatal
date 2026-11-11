"""Análisis exploratorio de irregularidades en contratos SECOP II."""
import sqlite3, json
from collections import defaultdict

conn = sqlite3.connect("backend/secop.db")
conn.row_factory = sqlite3.Row

print("=" * 60)
print("ANÁLISIS DE PATRONES DE IRREGULARIDAD")
print("=" * 60)

# 1. Contratos firmados en fin de semana
print("\n📅 1. CONTRATOS FIRMADOS EN FIN DE SEMANA")
weekend = conn.execute("""
    SELECT COUNT(*) FROM contratos 
    WHERE fecha_de_firma IS NOT NULL 
    AND CAST(strftime('%w', fecha_de_firma) AS INT) IN (0, 6)
""").fetchone()[0]
total = conn.execute("SELECT COUNT(*) FROM contratos WHERE fecha_de_firma IS NOT NULL").fetchone()[0]
print(f"   {weekend} de {total} ({weekend/total*100:.1f}%)")

# 2. Valores exactamente redondos (múltiplos de 1M)
print("\n💰 2. VALORES REDONDOS (múltiplos exactos de $1.000.000)")
round_vals = conn.execute("""
    SELECT COUNT(*) FROM contratos 
    WHERE valor_del_contrato > 1000000 
    AND valor_del_contrato = CAST(valor_del_contrato / 1000000 AS INTEGER) * 1000000
""").fetchone()[0]
total_with_val = conn.execute("SELECT COUNT(*) FROM contratos WHERE valor_del_contrato > 1000000").fetchone()[0]
print(f"   {round_vals} de {total_with_val} ({round_vals/total_with_val*100:.1f}%)")

# 3. Concentración de proveedores por entidad
print("\n🏢 3. CONCENTRACIÓN: ENTIDAD-PROVEEDOR (>5 contratos)")
pairs = conn.execute("""
    SELECT nombre_entidad, proveedor_adjudicado, COUNT(*), SUM(valor_del_contrato)
    FROM contratos 
    WHERE proveedor_adjudicado IS NOT NULL
    GROUP BY nombre_entidad, proveedor_adjudicado
    HAVING COUNT(*) > 5
    ORDER BY COUNT(*) DESC
    LIMIT 15
""").fetchall()
for p in pairs:
    print(f"   {p[0][:40]} → {p[1][:30]}: {p[2]} contratos, ${p[3]:,.0f}")

# 4. Posible fraccionamiento (mismo proveedor + entidad, múltiples contratos en 30 días)
print("\n✂️ 4. POSIBLE FRACCIONAMIENTO (misma entidad+proveedor, >3 contratos en 30 días)")
frac = conn.execute("""
    SELECT c1.nombre_entidad, c1.proveedor_adjudicado, COUNT(DISTINCT c1.id_contrato) as cnt,
           SUM(c1.valor_del_contrato) as total_val
    FROM contratos c1
    JOIN contratos c2 ON c1.nombre_entidad = c2.nombre_entidad 
        AND c1.proveedor_adjudicado = c2.proveedor_adjudicado
        AND c1.id_contrato != c2.id_contrato
        AND ABS(julianday(c1.fecha_de_firma) - julianday(c2.fecha_de_firma)) <= 30
    WHERE c1.modalidad_de_contratacion IN ('Mínima cuantía', 'Contratación directa')
    GROUP BY c1.nombre_entidad, c1.proveedor_adjudicado
    HAVING cnt > 3
    ORDER BY cnt DESC
    LIMIT 15
""").fetchall()
for f in frac:
    print(f"   {f[0][:40]} → {f[1][:30]}: {f[2]} contratos, ${f[3]:,.0f}")

# 5. Contratación directa con valores altos
print("\n⚠️ 5. CONTRATACIÓN DIRECTA CON VALORES > $500M")
direct_high = conn.execute("""
    SELECT nombre_entidad, proveedor_adjudicado, valor_del_contrato, fecha_de_firma
    FROM contratos
    WHERE modalidad_de_contratacion = 'Contratación directa'
    AND valor_del_contrato > 500000000
    ORDER BY valor_del_contrato DESC
    LIMIT 15
""").fetchall()
for d in direct_high:
    print(f"   {d[0][:35]} → {d[1][:25]}: ${d[2]:,.0f} ({d[3][:10] if d[3] else 'N/A'})")

# 6. Proveedor = Representante Legal
print("\n🔄 6. PROVEEDOR = REPRESENTANTE LEGAL (conflicto de interés)")
cnt_conflict = 0
samples = []
for row in conn.execute("SELECT id_contrato, nombre_entidad, proveedor_adjudicado, raw_json, valor_del_contrato FROM contratos").fetchall():
    d = json.loads(row[3])
    prov = (row[2] or "").lower().strip()
    rep = (d.get("nombre_representante_legal") or "").lower().strip()
    if prov and rep and prov == rep and prov not in ("no definido", "sin descripcion", ""):
        cnt_conflict += 1
        if len(samples) < 5:
            samples.append((row[1], row[2], row[4]))
print(f"   Total: {cnt_conflict} contratos")
for s in samples:
    print(f"   {s[0][:40]} → {s[1][:30]}: ${s[2]:,.0f}")

# 7. Anomalías de fechas
print("\n📆 7. INICIO ANTES DE FIRMA")
date_anom = conn.execute("""
    SELECT COUNT(*) FROM contratos c
    JOIN (SELECT id_contrato, json_extract(raw_json, '$.fecha_de_inicio_del_contrato') as fecha_inicio 
          FROM contratos WHERE raw_json IS NOT NULL) r ON c.id_contrato = r.id_contrato
    WHERE r.fecha_inicio < c.fecha_de_firma AND r.fecha_inicio IS NOT NULL AND c.fecha_de_firma IS NOT NULL
""").fetchone()[0]
print(f"   {date_anom} contratos con fecha de inicio anterior a la firma")

# 8. Modalidades
print("\n📊 8. DISTRIBUCIÓN POR MODALIDAD")
for r in conn.execute("SELECT modalidad_de_contratacion, COUNT(*), AVG(valor_del_contrato) FROM contratos GROUP BY modalidad_de_contratacion ORDER BY COUNT(*) DESC").fetchall():
    avg = f"${r[2]:,.0f}" if r[2] else "N/A"
    print(f"   {r[0]}: {r[1]} contratos, promedio {avg}")

conn.close()
print("\n✅ Análisis completo")
