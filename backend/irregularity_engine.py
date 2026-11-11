import sqlite3
import json
from pathlib import Path
import math
from datetime import datetime
import itertools

class IrregularityEngine:
    def __init__(self, db_path=None):
        if db_path is None:
            # Resolve DB path: look in same dir as __file__, then '.', then '..'
            base_dir = Path(__file__).parent
            candidates = [
                base_dir / 'secop.db',
                Path('.') / 'secop.db',
                Path('..') / 'secop.db',
                Path('.') / 'backend' / 'secop.db'
            ]
            for c in candidates:
                if c.exists():
                    db_path = c
                    break
            if not db_path:
                db_path = base_dir / 'secop.db'  # default
        
        self.db_path = str(db_path)
        self._ensure_tables()
        self.context = {}

    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=60, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _ensure_tables(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS alertas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_contrato TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                severity TEXT NOT NULL,
                score INTEGER NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS risk_scores (
                id_contrato TEXT PRIMARY KEY,
                total_score INTEGER NOT NULL,
                risk_level TEXT NOT NULL,
                alert_count INTEGER NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_alertas_id_contrato ON alertas(id_contrato);")
            conn.commit()

    def _compute_context(self, conn=None):
        # Precompute stats: percentile 95 for 'Contratacion directa' and mean/stddev for modalidad+departamento
        close_after = conn is None
        if conn is None:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            # Rule 2: percentile 95 for 'Contratacion directa'
            cursor.execute("""
                SELECT valor_del_contrato 
                FROM contratos 
                WHERE modalidad_de_contratacion IN ('Contratación directa', 'Contratacion directa')
                ORDER BY valor_del_contrato ASC
            """)
            values = [row[0] for row in cursor.fetchall() if row[0] is not None]
            if values:
                p95_idx = int(len(values) * 0.95)
                self.context['p95_directa'] = values[p95_idx]
            else:
                self.context['p95_directa'] = float('inf')

            # Rule 7: mean and stddev by modalidad+departamento
            cursor.execute("""
                SELECT modalidad_de_contratacion, departamento, 
                       AVG(valor_del_contrato) as mean_val, 
                       COUNT(valor_del_contrato) as cnt
                FROM contratos
                WHERE valor_del_contrato IS NOT NULL
                GROUP BY modalidad_de_contratacion, departamento
            """)
            means = {}
            for row in cursor.fetchall():
                mod, dep, mean_val, cnt = row
                means[(mod, dep)] = {'mean': mean_val, 'cnt': cnt, 'variance_sum': 0.0}

            cursor.execute("""
                SELECT modalidad_de_contratacion, departamento, valor_del_contrato
                FROM contratos
                WHERE valor_del_contrato IS NOT NULL
            """)
            for row in cursor.fetchall():
                mod, dep, val = row
                if (mod, dep) in means:
                    means[(mod, dep)]['variance_sum'] += (val - means[(mod, dep)]['mean']) ** 2

            stats = {}
            for k, v in means.items():
                if v['cnt'] > 1:
                    stddev = math.sqrt(v['variance_sum'] / (v['cnt'] - 1))
                else:
                    stddev = 0
                stats[k] = {'mean': v['mean'], 'stddev': stddev}
                
        self.context['stats_mod_dep'] = stats
        if close_after:
            conn.close()

    def _check_rule_1(self, conn):
        # Rule 1 - Contract Splitting (Fraccionamiento)
        alerts = []
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id_contrato, nombre_entidad, proveedor_adjudicado, fecha_de_firma
            FROM contratos
            WHERE modalidad_de_contratacion IN ('Mínima cuantía', 'Minima cuantia', 'Contratación directa', 'Contratacion directa')
            AND fecha_de_firma IS NOT NULL
            ORDER BY nombre_entidad, proveedor_adjudicado, fecha_de_firma
        """)
        rows = cursor.fetchall()
        
        def parse_date(d_str):
            try:
                if 'T' in d_str:
                    return datetime.fromisoformat(d_str.split('T')[0])
                return datetime.fromisoformat(d_str.split()[0])
            except:
                return None

        grouped = itertools.groupby(rows, key=lambda x: (x[1], x[2]))
        for (entidad, proveedor), group in grouped:
            contracts = list(group)
            if len(contracts) > 3:
                contracts.sort(key=lambda x: x[3])
                for i in range(len(contracts)):
                    date_i = parse_date(contracts[i][3])
                    if not date_i:
                        continue
                    
                    window = [contracts[i]]
                    for j in range(i+1, len(contracts)):
                        date_j = parse_date(contracts[j][3])
                        if not date_j:
                            continue
                        if (date_j - date_i).days <= 30:
                            window.append(contracts[j])
                        else:
                            break
                    
                    if len(window) > 3:
                        for c in window:
                            alerts.append({
                                'id_contrato': c[0],
                                'rule_id': 'R1_FRACCIONAMIENTO',
                                'severity': 'alta',
                                'score': 20,
                                'description': f"Posible fraccionamiento: {len(window)} contratos con {proveedor} en menos de 30 días."
                            })
                        break
        
        unique_alerts = {a['id_contrato']: a for a in alerts}
        return list(unique_alerts.values())

    def _check_rule_2(self, conn):
        # Rule 2 - Direct Contracting with Atypical Value
        alerts = []
        p95 = self.context.get('p95_directa', float('inf'))
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, valor_del_contrato 
            FROM contratos 
            WHERE modalidad_de_contratacion IN ('Contratación directa', 'Contratacion directa')
            AND valor_del_contrato > 0
        """)
        for row in cursor.fetchall():
            id_c, valor = row
            if valor > 500000000:
                alerts.append({
                    'id_contrato': id_c,
                    'rule_id': 'R2_VALOR_ATIPICO_DIRECTA',
                    'severity': 'alta',
                    'score': 20,
                    'description': f"Contrato directo por valor muy alto: ${valor:,.2f}"
                })
            elif valor > p95:
                alerts.append({
                    'id_contrato': id_c,
                    'rule_id': 'R2_VALOR_ATIPICO_DIRECTA',
                    'severity': 'media',
                    'score': 10,
                    'description': f"Contrato directo por valor mayor al p95: ${valor:,.2f}"
                })
        return alerts

    def _check_rule_3(self, conn):
        # Rule 3 - Weekend Signing
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, fecha_de_firma
            FROM contratos
            WHERE fecha_de_firma IS NOT NULL
        """)
        for row in cursor.fetchall():
            id_c, fecha = row
            try:
                dt = datetime.fromisoformat(fecha.split('T')[0] if 'T' in fecha else fecha.split()[0])
                if dt.weekday() in (5, 6):
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R3_FIRMA_FIN_SEMANA',
                        'severity': 'baja',
                        'score': 5,
                        'description': "Contrato firmado en fin de semana."
                    })
            except Exception:
                pass
        return alerts

    def _check_rule_4(self, conn):
        # Rule 4 - Entity-Provider Concentration
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT nombre_entidad, proveedor_adjudicado, COUNT(*) as cnt
            FROM contratos
            GROUP BY nombre_entidad, proveedor_adjudicado
            HAVING cnt > 5
        """)
        groups = cursor.fetchall()
        
        if not groups:
            return alerts
        
        group_map = {(r[0], r[1]): r[2] for r in groups}
        
        cursor.execute("""
            SELECT id_contrato, nombre_entidad, proveedor_adjudicado
            FROM contratos
        """)
        for row in cursor.fetchall():
            id_c, ent, prov = row
            cnt = group_map.get((ent, prov))
            if cnt:
                if cnt > 10:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R4_CONCENTRACION_PROVEEDOR',
                        'severity': 'alta',
                        'score': 15,
                        'description': f"Alta concentración: el proveedor tiene {cnt} contratos con la entidad."
                    })
                elif 6 <= cnt <= 10:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R4_CONCENTRACION_PROVEEDOR',
                        'severity': 'media',
                        'score': 10,
                        'description': f"Concentración media: el proveedor tiene {cnt} contratos con la entidad."
                    })
        return alerts

    def _check_rule_5(self, conn):
        # Rule 5 - Date Anomaly
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, fecha_de_firma, raw_json
            FROM contratos
            WHERE fecha_de_firma IS NOT NULL AND raw_json IS NOT NULL
        """)
        
        for row in cursor.fetchall():
            id_c, fecha_firma_str, raw_json = row
            try:
                data = json.loads(raw_json)
                fecha_inicio_str = data.get('fecha_de_inicio_del_contrato')
                
                if fecha_inicio_str and fecha_firma_str:
                    d_firma = datetime.fromisoformat(fecha_firma_str.split('T')[0])
                    d_inicio = datetime.fromisoformat(fecha_inicio_str.split('T')[0][:10])
                    if d_inicio < d_firma:
                        alerts.append({
                            'id_contrato': id_c,
                            'rule_id': 'R5_ANOMALIA_FECHAS',
                            'severity': 'critica',
                            'score': 25,
                            'description': "La fecha de inicio del contrato es anterior a la fecha de firma."
                        })
            except Exception:
                pass
        return alerts

    def _check_rule_6(self, conn):
        # Rule 6 - Suspiciously Round Value
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, valor_del_contrato
            FROM contratos
            WHERE valor_del_contrato > 50000000
        """)
        for row in cursor.fetchall():
            id_c, valor = row
            if valor % 1000000 == 0:
                alerts.append({
                    'id_contrato': id_c,
                    'rule_id': 'R6_VALOR_REDONDO',
                    'severity': 'baja',
                    'score': 5,
                    'description': f"Valor del contrato sospechosamente redondo: ${valor:,.0f}"
                })
        return alerts

    def _check_rule_7(self, conn):
        # Rule 7 - Statistical Outlier
        alerts = []
        stats = self.context.get('stats_mod_dep', {})
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, modalidad_de_contratacion, departamento, valor_del_contrato
            FROM contratos
            WHERE valor_del_contrato IS NOT NULL
        """)
        for row in cursor.fetchall():
            id_c, mod, dep, val = row
            s = stats.get((mod, dep))
            if s and s['stddev'] > 0:
                if val > s['mean'] + 3 * s['stddev']:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R7_VALOR_ATIPICO_ESTADISTICO',
                        'severity': 'media',
                        'score': 15,
                        'description': "Valor excede 3 desviaciones estándar de la media para su grupo (Modalidad/Departamento)."
                    })
        return alerts

    def _check_rule_8(self, conn):
        # R8_ADICION_TIEMPO
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, raw_json
            FROM contratos
            WHERE raw_json IS NOT NULL
        """)
        for row in cursor.fetchall():
            id_c, raw_json = row
            try:
                data = json.loads(raw_json)
                dias_adicionados = int(data.get('dias_adicionados', 0))
                if dias_adicionados > 180:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R8_ADICION_TIEMPO',
                        'severity': 'alta',
                        'score': 15,
                        'description': f"Adición de tiempo excesiva: {dias_adicionados} días adicionados al contrato original."
                    })
                elif dias_adicionados > 90:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R8_ADICION_TIEMPO',
                        'severity': 'media',
                        'score': 10,
                        'description': f"Adición de tiempo considerable: {dias_adicionados} días adicionados."
                    })
            except Exception:
                pass
        return alerts

    def _check_rule_9(self, conn):
        # R9_PAGO_ADELANTADO
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, raw_json
            FROM contratos
            WHERE raw_json IS NOT NULL
        """)
        for row in cursor.fetchall():
            id_c, raw_json = row
            try:
                data = json.loads(raw_json)
                habilita = data.get('habilita_pago_adelantado', 'No')
                valor_adelantado = int(data.get('valor_de_pago_adelantado', 0))
                if str(habilita).strip().lower() == 'si' or valor_adelantado > 0:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R9_PAGO_ADELANTADO',
                        'severity': 'media',
                        'score': 10,
                        'description': f"El contrato contempla pago adelantado o anticipos, incrementando el riesgo."
                    })
            except Exception:
                pass
        return alerts

    def _check_rule_10(self, conn):
        # R10_OBJETO_GENERICO
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, valor_del_contrato, raw_json
            FROM contratos
            WHERE raw_json IS NOT NULL AND valor_del_contrato > 10000000
        """)
        for row in cursor.fetchall():
            id_c, valor, raw_json = row
            try:
                data = json.loads(raw_json)
                objeto = data.get('objeto_del_contrato', '')
                if objeto and len(str(objeto).strip()) < 30:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R10_OBJETO_GENERICO',
                        'severity': 'alta',
                        'score': 20,
                        'description': f"Objeto del contrato demasiado corto o genérico ('{str(objeto)[:30]}...') para un valor de ${valor:,.0f}."
                    })
            except Exception:
                pass
        return alerts

    def _check_rule_11(self, conn):
        # R11_CARRUSEL_CONTRATACION
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT proveedor_adjudicado, COUNT(DISTINCT nombre_entidad) as cant_entidades
            FROM contratos
            WHERE proveedor_adjudicado IS NOT NULL AND proveedor_adjudicado != 'No Definido'
            GROUP BY proveedor_adjudicado
            HAVING cant_entidades >= 5
        """)
        groups = cursor.fetchall()
        
        if not groups:
            return alerts
            
        prov_map = {r[0]: r[1] for r in groups}
        
        cursor.execute("""
            SELECT id_contrato, proveedor_adjudicado
            FROM contratos
        """)
        for row in cursor.fetchall():
            id_c, prov = row
            cant = prov_map.get(prov)
            if cant:
                alerts.append({
                    'id_contrato': id_c,
                    'rule_id': 'R11_CARRUSEL_CONTRATACION',
                    'severity': 'alta',
                    'score': 20,
                    'description': f"Posible monopolio: el proveedor tiene contratos simultáneos con {cant} entidades diferentes."
                })
        return alerts

    def _check_rule_12(self, conn):
        # R12_EJECUCION_CORTA_VALOR_ALTO
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, valor_del_contrato, raw_json
            FROM contratos
            WHERE raw_json IS NOT NULL AND valor_del_contrato >= 50000000
        """)
        for row in cursor.fetchall():
            id_c, valor, raw_json = row
            try:
                data = json.loads(raw_json)
                duracion_str = data.get('duraci_n_del_contrato', '')
                if duracion_str and 'dia' in str(duracion_str).lower():
                    dias = int(''.join(filter(str.isdigit, str(duracion_str))))
                    if valor > 100000000 and 0 < dias < 30:
                        alerts.append({
                            'id_contrato': id_c,
                            'rule_id': 'R12_EJECUCION_CORTA_VALOR_ALTO',
                            'severity': 'critica',
                            'score': 25,
                            'description': f"Tiempo de ejecución irrealmente corto ({dias} días) para un alto valor (${valor:,.0f})."
                        })
                    elif valor > 50000000 and 0 < dias < 15:
                        alerts.append({
                            'id_contrato': id_c,
                            'rule_id': 'R12_EJECUCION_CORTA_VALOR_ALTO',
                            'severity': 'critica',
                            'score': 25,
                            'description': f"Tiempo de ejecución irrealmente corto ({dias} días) para un alto valor (${valor:,.0f})."
                        })
            except Exception:
                pass
        return alerts

    def _check_rule_13(self, conn):
        # R13_DESAJUSTE_GEOGRAFICO
        # Detects contractors whose registered address is in a different region than the contracting entity
        alerts = []
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id_contrato, departamento, valor_del_contrato, raw_json
            FROM contratos
            WHERE raw_json IS NOT NULL AND departamento IS NOT NULL
        """)
        
        for row in cursor.fetchall():
            id_c, depto_entidad, valor, raw_json = row
            try:
                data = json.loads(raw_json)
                domicilio = str(data.get('domicilio_representante_legal', '') or '')
                ciudad_entidad = str(data.get('ciudad', '') or '')
                
                # Skip blank or undefined addresses
                if not domicilio or domicilio.lower() in ('no definido', 'sin descripcion', '', 'none'):
                    continue
                
                depto_lower = (depto_entidad or '').lower()
                ciudad_lower = (ciudad_entidad or '').lower()
                domicilio_lower = domicilio.lower()
                
                # Normalize common departamento names for matching
                depto_aliases = [depto_lower]
                alias_map = {
                    'bogotá': ['bogota', 'bogot'],
                    'cundinamarca': ['cundinamarca'],
                    'antioquia': ['antioquia', 'medellin', 'medellín'],
                    'valle del cauca': ['valle del cauca', 'cali'],
                    'santander': ['santander', 'bucaramanga'],
                    'atlántico': ['atlantico', 'barranquilla'],
                    'bolívar': ['bolivar', 'cartagena'],
                    'nariño': ['narino', 'pasto'],
                    'cauca': ['cauca', 'popayán'],
                    'huila': ['huila', 'neiva'],
                    'tolima': ['tolima', 'ibagué'],
                    'meta': ['meta', 'villavicencio'],
                    'córdoba': ['cordoba', 'montería'],
                    'magdalena': ['magdalena', 'santa marta'],
                    'cesar': ['cesar', 'valledupar'],
                    'norte de santander': ['norte de santander', 'cúcuta'],
                }
                for canonical, aliases in alias_map.items():
                    if canonical in depto_lower:
                        depto_aliases.extend(aliases)
                        break
                
                # Check if contractor's domicile mentions the entity's region
                region_present = any(alias in domicilio_lower for alias in depto_aliases)
                if ciudad_lower and ciudad_lower != 'no definido':
                    region_present = region_present or ciudad_lower in domicilio_lower
                
                if not region_present:
                    # Extra weight if the contract value is high
                    score = 15 if (valor or 0) > 50000000 else 10
                    severity = 'alta' if (valor or 0) > 50000000 else 'media'
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R13_DESAJUSTE_GEOGRAFICO',
                        'severity': severity,
                        'score': score,
                        'description': f"Contratista domiciliado fuera de la región de la entidad. Entidad en {ciudad_entidad or depto_entidad}, domicilio del contratista: '{domicilio[:80]}'."
                    })
            except Exception:
                pass
        return alerts

    def _check_rule_14(self, conn):
        # R14_EMPRESA_NUEVA — populated after RUES enrichment
        alerts = []
        cursor = conn.cursor()
        # Check if enrichment table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='proveedores_enriquecidos'")
        if not cursor.fetchone():
            return alerts
        
        cursor.execute("""
            SELECT c.id_contrato, c.fecha_de_firma, c.valor_del_contrato, c.proveedor_adjudicado,
                   p.fecha_matricula, p.municipio_matricula
            FROM contratos c
            JOIN proveedores_enriquecidos p ON c.documento_proveedor = p.documento_proveedor
            WHERE c.fecha_de_firma IS NOT NULL AND p.fecha_matricula IS NOT NULL
              AND p.fecha_matricula != '' AND p.fecha_matricula != 'No encontrado'
        """)
        for row in cursor.fetchall():
            id_c, fecha_firma, valor, proveedor, fecha_matricula, municipio_mat = row
            try:
                d_firma = datetime.fromisoformat(fecha_firma.split('T')[0])
                d_matricula = datetime.fromisoformat(str(fecha_matricula)[:10])
                meses = (d_firma - d_matricula).days / 30
                if meses < 0:
                    continue  # matricula after firma, skip
                if meses < 6:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R14_EMPRESA_NUEVA',
                        'severity': 'critica',
                        'score': 30,
                        'description': f"Empresa '{proveedor}' creada solo {int(meses)} meses antes de obtener el contrato. Matrícula: {fecha_matricula}."
                    })
                elif meses < 12:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R14_EMPRESA_NUEVA',
                        'severity': 'alta',
                        'score': 20,
                        'description': f"Empresa '{proveedor}' con solo {int(meses)} meses de existencia al obtener el contrato. Matrícula: {fecha_matricula}."
                    })
            except Exception:
                pass
        return alerts

    def _check_rule_15(self, conn):
        # R15_VENTANA_LICITACION_CORTA — populated after SECOP Procesos ingestion
        alerts = []
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='procesos'")
        if not cursor.fetchone():
            return alerts
        
        cursor.execute("""
            SELECT c.id_contrato, c.proceso_de_compra, c.valor_del_contrato,
                   p.fecha_publicacion, p.fecha_cierre, p.numero_oferentes
            FROM contratos c
            JOIN procesos p ON c.proceso_de_compra = p.proceso_de_compra
            WHERE p.fecha_publicacion IS NOT NULL AND p.fecha_cierre IS NOT NULL
        """)
        for row in cursor.fetchall():
            id_c, proceso, valor, fecha_pub, fecha_cierre, num_oferentes = row
            try:
                d_pub = datetime.fromisoformat(str(fecha_pub)[:10])
                d_cierre = datetime.fromisoformat(str(fecha_cierre)[:10])
                ventana_dias = (d_cierre - d_pub).days
                
                if ventana_dias < 1:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R15_VENTANA_LICITACION_CORTA',
                        'severity': 'critica',
                        'score': 30,
                        'description': f"Ventana de licitación de {ventana_dias} día(s) — prácticamente imposible que competidores presenten propuestas."
                    })
                elif ventana_dias <= 3:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R15_VENTANA_LICITACION_CORTA',
                        'severity': 'alta',
                        'score': 20,
                        'description': f"Ventana de licitación muy corta: solo {ventana_dias} día(s) para presentar ofertas."
                    })
                
                # Bonus: single bidder in competitive process
                if num_oferentes == 1 and valor and valor > 10000000:
                    alerts.append({
                        'id_contrato': id_c,
                        'rule_id': 'R15_OFERENTE_UNICO',
                        'severity': 'alta',
                        'score': 15,
                        'description': f"Solo un oferente en proceso competitivo para contrato de ${valor:,.0f}. Alta probabilidad de proceso dirigido."
                    })
            except Exception:
                pass
        return alerts

    def run_full_scan(self):
        import time
        t0 = time.time()
        
        # Use ONE connection for the entire scan to avoid lock contention
        conn = self.get_connection()
        try:
            self._compute_context(conn)
            cursor = conn.cursor()
            
            # Clear old data
            cursor.execute("DELETE FROM alertas")
            cursor.execute("DELETE FROM risk_scores")
            conn.commit()
            all_alerts = []
            all_alerts.extend(self._check_rule_1(conn))
            all_alerts.extend(self._check_rule_2(conn))
            all_alerts.extend(self._check_rule_3(conn))
            all_alerts.extend(self._check_rule_4(conn))
            all_alerts.extend(self._check_rule_5(conn))
            all_alerts.extend(self._check_rule_6(conn))
            all_alerts.extend(self._check_rule_7(conn))
            all_alerts.extend(self._check_rule_8(conn))
            all_alerts.extend(self._check_rule_9(conn))
            all_alerts.extend(self._check_rule_10(conn))
            all_alerts.extend(self._check_rule_11(conn))
            all_alerts.extend(self._check_rule_12(conn))
            all_alerts.extend(self._check_rule_13(conn))
            all_alerts.extend(self._check_rule_14(conn))
            all_alerts.extend(self._check_rule_15(conn))
            
            if all_alerts:
                cursor.executemany("""
                    INSERT INTO alertas (id_contrato, rule_id, severity, score, description)
                    VALUES (:id_contrato, :rule_id, :severity, :score, :description)
                """, all_alerts)
            
            scores = {}
            for a in all_alerts:
                c_id = a['id_contrato']
                if c_id not in scores:
                    scores[c_id] = {'total_score': 0, 'alert_count': 0}
                scores[c_id]['total_score'] += a['score']
                scores[c_id]['alert_count'] += 1
                
            risk_data = []
            for c_id, data in scores.items():
                s = min(data['total_score'], 100)
                if s <= 10: level = 'bajo'
                elif s <= 30: level = 'medio'
                elif s <= 60: level = 'alto'
                else: level = 'critico'
                
                risk_data.append((c_id, s, level, data['alert_count']))
                
            if risk_data:
                cursor.executemany("""
                    INSERT INTO risk_scores (id_contrato, total_score, risk_level, alert_count)
                    VALUES (?, ?, ?, ?)
                """, risk_data)
                
            conn.commit()
            
            cursor.execute("SELECT COUNT(*) FROM contratos")
            total_contracts = cursor.fetchone()[0]
            t1 = time.time()
        finally:
            conn.close()
        
        return {
            'total_scanned': total_contracts,
            'total_alerts': len(all_alerts),
            'contracts_with_alerts': len(scores),
            'time_seconds': round(t1 - t0, 2)
        }

    def get_contract_alerts(self, id_contrato):
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM alertas WHERE id_contrato = ?", (id_contrato,))
            alerts = [dict(row) for row in cursor.fetchall()]
            
            cursor.execute("SELECT * FROM risk_scores WHERE id_contrato = ?", (id_contrato,))
            score = cursor.fetchone()
            
            return {
                'id_contrato': id_contrato,
                'risk_score': dict(score) if score else None,
                'alerts': alerts
            }

    def get_summary(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) FROM contratos")
            total_scanned = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM alertas")
            total_alerts = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM risk_scores")
            contracts_with_alerts = cursor.fetchone()[0]
            
            cursor.execute("SELECT risk_level, COUNT(*) FROM risk_scores GROUP BY risk_level")
            by_risk_level = {row[0]: row[1] for row in cursor.fetchall()}
            
            cursor.execute("SELECT rule_id, COUNT(*) FROM alertas GROUP BY rule_id")
            by_rule = {row[0]: row[1] for row in cursor.fetchall()}
            
            cursor.execute("SELECT MAX(updated_at) FROM risk_scores")
            last_scan = cursor.fetchone()[0]
            
            return {
                'total_scanned': total_scanned,
                'total_alerts': total_alerts,
                'contracts_with_alerts': contracts_with_alerts,
                'by_risk_level': by_risk_level,
                'by_rule': by_rule,
                'last_scan': last_scan
            }
