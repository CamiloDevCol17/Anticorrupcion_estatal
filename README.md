# 🛡️ Vigía — Anticorrupción Estatal

**Sistema de monitoreo ciudadano sobre datos abiertos de contratación pública (SECOP II)**

Vigía descarga, analiza y califica contratos del Estado colombiano usando datos abiertos de [SECOP II (datos.gov.co)](https://www.datos.gov.co), detectando automáticamente **15 patrones de irregularidad** que incluyen fraccionamiento de contratos, valores atípicos, conflictos de interés, empresas de fachada, ventanas de licitación sospechosamente cortas, y más.

---

## 📂 Estructura del proyecto

```
├── main.py                        # Ingesta incremental de contratos SECOP II (Socrata API)
├── estadisticas.py                # Consultas rápidas de verificación sobre la base local
├── analyze_irregularities.py      # Análisis exploratorio de patrones de irregularidad
├── backend/
│   ├── api.py                     # API REST (FastAPI) — dashboard + motor de alertas
│   ├── irregularity_engine.py     # Motor de detección: 15 reglas de riesgo
│   ├── enrich_rues.py             # Enriquecimiento con datos de RUES (Cámara de Comercio)
│   ├── ingest_procesos.py         # Ingesta de procesos SECOP II (ventanas de licitación)
│   └── requirements.txt          # Dependencias Python
├── frontend/
│   ├── index.html                 # Dashboard — interfaz de expediente digital
│   ├── apps.js                    # Lógica del dashboard (Chart.js + fetch a la API)
│   └── styles.css                 # Estilos (tema gaceta/expediente oficial)
└── .gitignore
```

## 🚀 Instalación y uso

### 1. Requisitos previos

- **Python 3.10+**
- **pip**

### 2. Instalar dependencias

```bash
pip install -r backend/requirements.txt
pip install requests
```

### 3. Ingesta de datos (contratos SECOP II)

```bash
# Primera corrida — trae los últimos 90 días por defecto
python main.py

# Corrida incremental (usa el último cutoff guardado)
python main.py

# Forzar desde una fecha específica
python main.py --since 2026-01-01

# Filtrar por departamento
python main.py --departamento "Antioquia"
```

> **Nota sobre credenciales**: El script funciona sin credenciales, pero con límites de tasa más bajos. Para mayor velocidad, crea un API Key gratuito en [Socrata](https://datos.gov.co) y expórtalo:
> ```bash
> export SECOP_KEY_ID="tu_key_id"
> export SECOP_KEY_SECRET="tu_key_secret"
> ```

### 4. (Opcional) Ingesta de procesos y enriquecimiento RUES

```bash
# Descargar procesos de licitación (habilita regla R15)
python backend/ingest_procesos.py

# Enriquecer proveedores con datos de Cámara de Comercio (habilita regla R14)
python backend/enrich_rues.py
```

### 5. Levantar la API y el dashboard

```bash
cd backend
uvicorn api:app --reload --port 8001
```

Luego abre en el navegador: **http://127.0.0.1:8001**

### 6. Ejecutar escaneo de irregularidades

Una vez con datos cargados, ejecuta el escaneo completo vía API:

```bash
curl -X POST http://127.0.0.1:8001/api/scan/run
```

O accede a la documentación interactiva: **http://127.0.0.1:8001/docs**

---

## 🔍 Reglas de detección de irregularidades (15 reglas)

| ID | Regla | Severidad | Qué detecta |
|----|-------|-----------|-------------|
| R1 | Fraccionamiento | 🔴 Alta | Mismo proveedor + entidad con >3 contratos en 30 días (mínima cuantía / directa) |
| R2 | Valor atípico en directa | 🔴 Alta | Contratación directa con valores > $500M o > percentil 95 |
| R3 | Firma en fin de semana | 🟡 Baja | Contratos firmados sábado o domingo |
| R4 | Concentración proveedor | 🔴 Alta | Proveedor con >10 contratos en la misma entidad |
| R5 | Anomalía de fechas | 🔴 Crítica | Fecha de inicio anterior a la firma |
| R6 | Valor redondo | 🟡 Baja | Contratos >$50M con valor exacto en millones |
| R7 | Outlier estadístico | 🟠 Media | Valor >3 desviaciones estándar para su modalidad/departamento |
| R8 | Adición de tiempo | 🔴 Alta | Contratos con >180 días adicionados |
| R9 | Pago adelantado | 🟠 Media | Contratos con anticipos habilitados |
| R10 | Objeto genérico | 🔴 Alta | Objeto del contrato <30 caracteres con valor >$10M |
| R11 | Carrusel de contratación | 🔴 Alta | Proveedor con contratos en ≥5 entidades diferentes |
| R12 | Ejecución corta + valor alto | 🔴 Crítica | Contratos >$100M con <30 días de ejecución |
| R13 | Desajuste geográfico | 🟠 Media | Contratista domiciliado fuera de la región de la entidad |
| R14 | Empresa nueva | 🔴 Crítica | Empresa creada <6 meses antes de obtener el contrato (requiere RUES) |
| R15 | Ventana de licitación corta | 🔴 Crítica | <3 días entre publicación y cierre del proceso (requiere Procesos) |

---

## 🌐 API Endpoints principales

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/api/health` | Estado de la conexión a la BD |
| GET | `/api/stats/summary` | Resumen general (con filtros) |
| GET | `/api/stats/top-entidades` | Top entidades por número de contratos |
| GET | `/api/stats/top-proveedores` | Top proveedores por número de contratos |
| GET | `/api/stats/modalidades` | Distribución por modalidad |
| GET | `/api/stats/timeline` | Serie de tiempo de contratos |
| GET | `/api/contratos` | Listado paginado de contratos |
| GET | `/api/contratos/{id}` | Detalle de un contrato |
| GET | `/api/contratos/{id}/alertas` | Alertas de un contrato específico |
| GET | `/api/stats/irregularidades` | Resumen de alertas detectadas |
| POST | `/api/scan/run` | Ejecutar escaneo completo de irregularidades |
| GET | `/api/departamentos` | Lista de departamentos |
| GET | `/api/municipios` | Lista de municipios (filtrable por depto) |
| GET | `/api/enriquecimiento/status` | Estado de enriquecimiento RUES y procesos |

Todos los endpoints de stats aceptan filtros: `q`, `departamento`, `municipio`, `modalidad`, `fecha_inicio`, `fecha_fin`.

---

## 📊 Fuentes de datos

- **SECOP II — Contratos Electrónicos**: Dataset `jbjy-vk9h` en [datos.gov.co](https://www.datos.gov.co/resource/jbjy-vk9h)
- **SECOP II — Procesos de Contratación**: Dataset `p6dx-8zbt` en [datos.gov.co](https://www.datos.gov.co/resource/p6dx-8zbt)
- **RUES**: [Registro Único Empresarial y Social](https://www.rues.org.co) — consulta pública de matrículas mercantiles

---

## ⚖️ Disclaimer

> Vigía es un prototipo de monitoreo ciudadano sobre datos abiertos de SECOP II. Las cifras mostradas provienen directamente del dataset público y **no constituyen una acusación** sobre ninguna entidad o proveedor. Las alertas señalan **patrones estadísticos** que ameritan verificación humana.

---

## 📜 Licencia

MIT

