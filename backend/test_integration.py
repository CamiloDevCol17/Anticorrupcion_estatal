"""Quick integration test: starts the API, runs queries, runs scan."""
import uvicorn, threading, time, urllib.request, json, sys

sys.path.insert(0, ".")
from api import app

# Start server in background thread
t = threading.Thread(
    target=lambda: uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning"),
    daemon=True,
)
t.start()
time.sleep(3)

def get(path):
    r = urllib.request.urlopen(f"http://127.0.0.1:8001{path}")
    return json.loads(r.read())

def post(path):
    req = urllib.request.Request(f"http://127.0.0.1:8001{path}", method="POST", data=b"")
    r = urllib.request.urlopen(req)
    return json.loads(r.read())

print("=" * 60)
print("INTEGRATION TEST")
print("=" * 60)

h = get("/api/health")
print(f"1. Health:  {h}")
assert h["status"] == "ok", "DB not found!"

s = get("/api/stats/summary")
print(f"2. Summary: {s['total_contratos']} contratos, valor total ${s['valor_total']:,.0f}")
print(f"   Entidades: {s['num_entidades']}, Proveedores: {s['num_proveedores']}")
assert s["total_contratos"] > 0, "No contracts in DB!"

c = get("/api/contratos?page=1&page_size=3")
print(f"3. Contratos API: total={c['total']}, page={c['page']}, results_shown={len(c['results'])}")

deptos = get("/api/departamentos")
print(f"4. Departamentos: {len(deptos)} encontrados — {deptos[:5]}...")

mods = get("/api/stats/modalidades")
print(f"5. Modalidades: {len(mods)} tipos")
for m in mods[:3]:
    print(f"   - {m['modalidad']}: {m['num_contratos']}")

tl = get("/api/stats/timeline?group_by=month")
print(f"6. Timeline (mensual): {len(tl)} periodos")

print("\n--- Ejecutando escaneo de irregularidades (15 reglas) ---")
scan = post("/api/scan/run")
print(f"7. Scan result:")
print(f"   Contratos escaneados : {scan['total_scanned']}")
print(f"   Alertas generadas   : {scan['total_alerts']}")
print(f"   Contratos con alerta: {scan['contracts_with_alerts']}")
print(f"   Tiempo              : {scan['time_seconds']}s")

irr = get("/api/stats/irregularidades")
print(f"8. Irregularidades resumen:")
print(f"   Por nivel de riesgo : {irr.get('by_risk_level', {})}")
print(f"   Por regla           : {irr.get('by_rule', {})}")

enr = get("/api/enriquecimiento/status")
print(f"9. Enriquecimiento status: {enr}")

# Test contract detail with alerts
c2 = get("/api/contratos?page=1&page_size=1")
if c2["results"]:
    cid = c2["results"][0]["id_contrato"]
    detail = get(f"/api/contratos/{cid}")
    print(f"10. Detalle contrato {cid[:30]}...: OK")
    alerts = get(f"/api/contratos/{cid}/alertas")
    print(f"    Alertas: {len(alerts.get('alertas', []))}, Risk: {alerts.get('risk_level', 'N/A')}")

print("\n" + "=" * 60)
print("ALL TESTS PASSED")
print("=" * 60)

