import os
import time as tm
from datetime import datetime, date, timedelta, timezone
from flask import Flask, jsonify, request, g
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from sqlalchemy import text, and_, or_, bindparam
from flask_mail import Mail, Message
import threading
import requests
import logging
import psycopg
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import jwt
import json
from pathlib import Path

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", os.getenv("FLASK_SECRET_KEY", "cambiar_esta_llave_en_prod_2026"))
JWT_EXPIRATION_HOURS = 12


EVOLUTION_LOCAL_URL = os.environ.get('EVOLUTION_URL', 'http://localhost:8080')
API_KEY = os.environ.get('API_KEY', 'Ideal2025')
INSTANCE_NAME = os.environ.get('INSTANCE_NAME', 'cliente_idealmente')
NUMERO_DESTINO = "5213222887047" # El número propio o del administrador donde quieres recibirlo

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

def enviar_whatsapp_en_segundo_plan(telefono, mensaje, pac_phone, pac_mens):
    def tarea():
        try:
            print("preparando url")
            print(INSTANCE_NAME,API_KEY)
            url = f"{EVOLUTION_LOCAL_URL}/message/sendText/{INSTANCE_NAME}"
            print("URL:",url)
            headers = {
                "Content-Type": "application/json",
                "apikey": API_KEY
            }
            print("preparando payload")
            payload = {
                "number": telefono,
                "text": mensaje
            }
            print("response")
            print("payload:",payload)
            response = requests.post(url, json=payload, headers=headers, timeout=5)
            print(f"Notificación WhatsApp enviada: {response.status_code}")
            if response.status_code not in (200, 201):
                try:
                    print("Detalle del error (JSON):", response.json())
                except Exception:
                    print("Detalle del error (Texto):", response.text)

            tm.sleep(5)

            phone = "52"+pac_phone
            print("Numero:",phone)
            payload = {
                "number": phone,
                "text": pac_mens
            }
            print("payload:",payload)
            response = requests.post(url, json=payload, headers=headers, timeout=5)
            print(f"Notificación WhatsApp paciente enviada: {response.status_code}")

        except Exception as e:
            print(f"Error enviando WhatsApp en segundo plano: {str(e)}")

    # Dispara el hilo para que corra de forma independiente
    print("disparando el hilo")
    threading.Thread(target=tarea).start()
    print("envio correcto")

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")



def init_admin_schema():
    """Crea admin_users y admin_audit_log e inserta los 3 usuarios iniciales usando SQLAlchemy."""
    initial_pwd = os.getenv("ADMIN_INITIAL_PASSWORD", "Ideal2025*!")

    ddl_statements = [
        "CREATE EXTENSION IF NOT EXISTS pgcrypto;",
        """
        CREATE TABLE IF NOT EXISTS admin_users (
            id SERIAL PRIMARY KEY,
            username VARCHAR(60) NOT NULL UNIQUE,
            display_name VARCHAR(120) NOT NULL,
            password_hash TEXT NOT NULL,
            role VARCHAR(30) NOT NULL CHECK (role IN ('superadmin', 'admin', 'asistente')),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TIMESTAMP WITH TIME ZONE NULL,
            password_changed_at TIMESTAMP WITH TIME ZONE NULL,
            last_login_at TIMESTAMP WITH TIME ZONE NULL,
            created_by INTEGER NULL REFERENCES admin_users(id),
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_admin_users_username ON admin_users (username);",
        """
        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id BIGSERIAL PRIMARY KEY,
            admin_user_id INTEGER NOT NULL REFERENCES admin_users(id),
            action VARCHAR(80) NOT NULL,
            entity_type VARCHAR(50) NOT NULL,
            entity_id VARCHAR(60) NULL,
            details JSONB NULL,
            ip_address VARCHAR(45) NULL,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_audit_log_user ON admin_audit_log (admin_user_id);",
        "CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON admin_audit_log (entity_type, entity_id);",
    ]

    seed_users = [
        ("idealmente", "Administración General Idealmente", generate_password_hash(initial_pwd), "superadmin"),
        ("adminideal", "Administrador Operativo", generate_password_hash(initial_pwd), "admin"),
        ("asistenteideal", "Asistente de Recepción", generate_password_hash(initial_pwd), "asistente"),
    ]

    try:
        for stmt in ddl_statements:
            db.session.execute(text(stmt))

        for username, display_name, pwd_hash, role in seed_users:
            db.session.execute(
                text("""
                    INSERT INTO admin_users (username, display_name, password_hash, role, must_change_password)
                    VALUES (:username, :display_name, :password_hash, :role, TRUE)
                    ON CONFLICT (username) DO NOTHING;
                """),
                {
                    "username": username,
                    "display_name": display_name,
                    "password_hash": pwd_hash,
                    "role": role,
                },
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

def admin_required(allowed_roles=None):
    """Protege endpoints administrativos validando el JWT y opcionalmente el rol."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.method == "OPTIONS":
                return jsonify({}), 200

            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return jsonify({"error": "Token de acceso requerido"}), 401

            token = auth_header.split(" ", 1)[1].strip()
            try:
                payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=["HS256"])
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "La sesión ha expirado. Inicia sesión nuevamente."}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "Token inválido"}), 401

            if allowed_roles and payload.get("role") not in allowed_roles:
                return jsonify({"error": "No tienes permisos suficientes para realizar esta acción."}), 403

            g.admin_user = {
                "id": payload["user_id"],
                "username": payload["username"],
                "role": payload["role"],
            }
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def log_admin_action(admin_user_id, action, entity_type, entity_id=None, details=None):
    """Registra una acción en admin_audit_log dentro de la sesión actual de SQLAlchemy."""
    ip_addr = request.headers.get("X-Forwarded-For", request.remote_addr)
    db.session.execute(
        text("""
            INSERT INTO admin_audit_log (admin_user_id, action, entity_type, entity_id, details, ip_address)
            VALUES (:admin_user_id, :action, :entity_type, :entity_id, CAST(:details AS jsonb), :ip_address)
        """),
        {
            "admin_user_id": admin_user_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id) if entity_id is not None else None,
            "details": json.dumps(details) if details else None,
            "ip_address": ip_addr,
        },
    )

def _obtener_id2_estado_sucursal(suc_id: str) -> str:
    """Obtiene id2 (2 letras) del estado de la sucursal via sucursal -> ciudad -> estado."""
    row = db.session.execute(
        text("""
            SELECT est.id2
            FROM idm_tc.idm_sucursal_tc suc
            INNER JOIN idm_tc.idm_ciudad_tc ciu ON suc.id_cit = ciu.id
            INNER JOIN idm_tc.idm_estado_tc est ON ciu.id_est = est.idiso
            WHERE suc.id = :suc_id
        """),
        {"suc_id": suc_id},
    ).mappings().first()

    if not row or not row["id2"]:
        raise ValueError("No se pudo determinar el estado (id2) para la sucursal seleccionada.")
    return str(row["id2"]).strip().upper()[:2]


def _generar_consecutivo_prefijo(tabla_fqn: str, prefijo: str, ancho_total: int = 10) -> str:
    """
    Genera ID de `ancho_total` caracteres: prefijo + dígitos consecutivos rellenos con ceros.
    Ej. prefijo='INT', ancho_total=10 -> 3 letras + 7 dígitos (INT0000012).
    Ej. prefijo='NT', ancho_total=10  -> 2 letras + 8 dígitos (NT00000014).
    """
    longitud_num = ancho_total - len(prefijo)
    patron = f"{prefijo}%"

    ultimo_id = db.session.execute(
        text(f"""
            SELECT id
            FROM {tabla_fqn}
            WHERE id LIKE :patron
            ORDER BY id DESC
            LIMIT 1
        """),
        {"patron": patron},
    ).scalar()

    siguiente = 1
    if ultimo_id and len(str(ultimo_id)) >= ancho_total:
        sufijo_num = str(ultimo_id)[-longitud_num:]
        if sufijo_num.isdigit():
            siguiente = int(sufijo_num) + 1

    return f"{prefijo}{str(siguiente).zfill(longitud_num)}"

def _resolver_carpeta_fotos_especialistas() -> Path:
    """
    Resuelve la ruta .../src/assets/especialistas sin depender del directorio
    actual de ejecución, y la crea si aún no existe.
    """
    base_dir = Path(__file__).resolve().parent
    candidatos = [
        base_dir / "src" / "assets" / "especialistas",
        base_dir.parent / "src" / "assets" / "especialistas",
        base_dir.parent / "frontend" / "src" / "assets" / "especialistas",
        Path.cwd() / "src" / "assets" / "especialistas",
    ]
    for ruta in candidatos:
        if ruta.parent.parent.exists():
            ruta.mkdir(parents=True, exist_ok=True)
            return ruta

    default_dir = candidatos[0]
    default_dir.mkdir(parents=True, exist_ok=True)
    return default_dir


def _extraer_datos_y_foto():
    """Soporta envío JSON tradicional y FormData (payload JSON + archivo foto)."""
    foto = request.files.get("foto") if request.files else None
    es_multipart = bool(
        (request.content_type and "multipart/form-data" in request.content_type.lower())
        or request.form
        or request.files
    )

    if es_multipart:
        raw_payload = request.form.get("payload")
        print("data:", raw_payload)
        if raw_payload:
            try:
                data = json.loads(raw_payload)
            except json.JSONDecodeError:
                return None, foto, "El campo payload no contiene un JSON válido."
        else:
            data = {}
            for k, v in request.form.items():
                if k in ("formacion_academica", "formacion_continua", "areas_enfoque", "servicios_ids"):
                    try:
                        data[k] = json.loads(v)
                    except Exception:
                        data[k] = []
                else:
                    data[k] = v
        return data or {}, foto, None

    data = request.get_json(silent=True) or {}
    print("data2:", data)
    return data, foto, None

def _siguiente_id_servicio(id_epd: str, ser_name: str) -> str:
    """Genera un ID varchar(5) único para idm_tc.idm_servicio_tc (ej. IM001)."""
    base_letters = "".join(ch for ch in (id_epd or ser_name or "SR").upper() if ch.isalpha())
    prefijo = (base_letters[:2] or "SR").ljust(2, "S")  # 2 letras + 3 dígitos = 5 chars

    rows = db.session.execute(
        text("SELECT id FROM idm_tc.idm_servicio_tc WHERE id LIKE :pat"),
        {"pat": f"{prefijo}%"},
    ).scalars().all()

    max_num = 0
    for r_id in rows:
        s_id = str(r_id or "")
        suf = "".join(ch for ch in s_id[len(prefijo):] if ch.isdigit())
        if suf:
            max_num = max(max_num, int(suf))

    return f"{prefijo}{max_num + 1:03d}"[:5]


def _normalizar_hora_time(val: str) -> str:
    """Normaliza HH:MM o HH:MM:SS a HH:MM:SS para columna TIME."""
    txt = (val or "00:50").strip()
    if len(txt) == 5:
        return f"{txt}:00"
    return txt


def _sincronizar_servicios_especialista(esp_id: str, servicios_ids: list):
    """Sincroniza idm_tr.idm_ser_esp_tr para el especialista indicado."""
    limpios = []
    vistos = set()
    for s_id in servicios_ids or []:
        sid = str(s_id or "").strip()
        if sid and sid not in vistos:
            existe_ser = db.session.execute(
                text("SELECT id FROM idm_tc.idm_servicio_tc WHERE id = :id"),
                {"id": sid},
            ).scalar()
            if not existe_ser:
                raise ValueError(f"El servicio '{sid}' no existe en el catálogo.")
            vistos.add(sid)
            limpios.append(sid)

    db.session.execute(
        text("DELETE FROM idm_tr.idm_ser_esp_tr WHERE id_esp = :id_esp"),
        {"id_esp": esp_id},
    )

    for sid in limpios:
        rel_id = _siguiente_id_entero("idm_tr.idm_ser_esp_tr")
        db.session.execute(
            text("""
                INSERT INTO idm_tr.idm_ser_esp_tr (id, id_esp, id_ser)
                VALUES (:id, :id_esp, :id_ser)
            """),
            {"id": int(rel_id), "id_esp": esp_id, "id_ser": sid},
        )

def _obtener_mapa_servicios_por_especialista() -> dict:
    """Devuelve { esp_id: { 'servicios': [...], 'servicios_ids': [...] } }."""
    rows = db.session.execute(
        text("""
            SELECT
                se.id_esp,
                s.id AS ser_id,
                s.ser_name,
                TO_CHAR(s.ser_dur, 'HH24:MI') AS ser_dur,
                s.ser_ses,
                COALESCE(s.ser_onl, false) AS ser_onl,
                s.id_epd,
                COALESCE(e.epd_nombre, '') AS epd_nombre,
                COALESCE(s.ser_inf, false) AS ser_inf
            FROM idm_tr.idm_ser_esp_tr se
            INNER JOIN idm_tc.idm_servicio_tc s
                ON CAST(s.id AS TEXT) = CAST(se.id_ser AS TEXT)
            LEFT JOIN idm_tc.idm_especialidades_tc e
                ON CAST(e.id AS TEXT) = CAST(s.id_epd AS TEXT)
            ORDER BY s.ser_name ASC
        """)
    ).mappings().all()

    mapa = {}
    for r in rows:
        esp_id = str(r["id_esp"])
        if esp_id not in mapa:
            mapa[esp_id] = {"servicios": [], "servicios_ids": []}

        ser_id = str(r["ser_id"])
        if ser_id not in mapa[esp_id]["servicios_ids"]:
            mapa[esp_id]["servicios_ids"].append(ser_id)
            mapa[esp_id]["servicios"].append({
                "id": ser_id,
                "ser_name": r["ser_name"],
                "ser_dur": r["ser_dur"],
                "ser_ses": r["ser_ses"],
                "ser_onl": bool(r["ser_onl"]),
                "id_epd": r["id_epd"],
                "epd_nombre": r["epd_nombre"],
                "ser_inf": bool(r["ser_inf"]),
            })
    return mapa

S3_BUCKET = os.getenv('S3_BUCKET_NAME')
AWS_KEY = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION')

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})
app.url_map.strict_slashes = False
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'isolation_level': 'READ COMMITTED'
}

db = SQLAlchemy(app)


# ==============================================================================
# ENDPOINT PARA /nosotros: LISTADO DE ESPECIALIDADES -> ESPECIALISTAS -> SERVICIOS
# ==============================================================================
@app.route('/api/especialidades', methods=['GET'])
def obtener_nosotros_arbol():
    try:
        query_esp = text("SELECT id, epd_nombre as nombre FROM idm_tc.idm_especialidad_tc")
        especialidades_raw = db.session.execute(query_esp).mappings().all()

        resultado = []
        for esp in especialidades_raw:
            # 1. Obtener especialistas activos de esta especialidad
            query_espc = text("""
                with a as (
                    select s.id as id_ser, epd_nombre, esp.id as epd_id 
                    from idm_tc.idm_servicio_tc s 
                    join idm_tc.idm_especialidad_tc esp on s.id_epd = esp.id
                ),
                b as (
                    select concat(esp.esp_name, ' ', esp.esp_ap_pat, ' ', coalesce(esp.esp_ap_mat, '')) as nombre, 
                           s.id_ser, esp.id as id_esp, esp.esp_activo as act
                    from idm_tc.idm_especialista_tc esp
                    join idm_tr.idm_ser_esp_tr s on s.id_esp = esp.id
                )
                select distinct id_esp as id, nombre, epd_nombre as titulo
                from a join b on b.id_ser = a.id_ser
                where epd_id = :esp_id and act = true
            """)
            especialistas_raw = db.session.execute(query_espc, {"esp_id": esp['id']}).mappings().all()

            lista_especialistas = []
            for espc in especialistas_raw:
                esp_id = espc['id']

                # 2. Obtener Servicios del Especialista
                query_serv = text("""
                    SELECT distinct ser.id, ser.ser_name AS nombre
                    FROM idm_tc.idm_servicio_tc ser
                    JOIN idm_tc.idm_tarifa_tc tar ON tar.id_ser = ser.id
                    JOIN idm_tr.idm_ser_esp_tr iset ON iset.id_ser = ser.id
                    WHERE iset.id_esp = :esp_id
                """)
                servicios_raw = db.session.execute(query_serv, {"esp_id": esp_id}).mappings().all()

                # 3. Obtener CV del Especialista
                query_cv = text("""
                    SELECT id as id_cv, slog as presentacion, ced_prof 
                    FROM idm_tc.idm_esp_cv_tc 
                    WHERE id_esp = :esp_id 
                    LIMIT 1
                """)
                cv_raw = db.session.execute(query_cv, {"esp_id": esp_id}).mappings().first()

                cv_data = {
                    "presentacion": cv_raw['presentacion'] if cv_raw else None,
                    "ced_prof": cv_raw['ced_prof'] if cv_raw else None,
                    "areas": [],
                    "estudios": [],
                    "cursos": []
                }

                if cv_raw:
                    id_cv = cv_raw['id_cv']

                    # 3a. Áreas de Enfoque
                    q_areas = text("SELECT nam_area as nombre FROM idm_tc.idm_cv_area_enf_tc WHERE id_cv = :id_cv")
                    cv_data["areas"] = [row['nombre'] for row in db.session.execute(q_areas, {"id_cv": id_cv}).mappings().all()]

                    # 3b. Estudios Académicos
                    q_estudios = text("""
                        SELECT title as grado, name_title as materia, ced_prof, dt_graduate as fecha 
                        FROM idm_tc.idm_cv_acad_tc
                        WHERE id_cv = :id_cv
                    """)
                    cv_data["estudios"] = [dict(r) for r in db.session.execute(q_estudios, {"id_cv": id_cv}).mappings().all()]

                    # 3c. Cursos / Certificaciones
                    q_cursos = text("""
                        SELECT type_doc as tipo, name_doc as nombre, doc_id, dt_emision as fecha 
                        FROM idm_tc.idm_cv_cont_tc 
                        WHERE id_cv = :id_cv
                    """)
                    cv_data["cursos"] = [dict(r) for r in db.session.execute(q_cursos, {"id_cv": id_cv}).mappings().all()]

                lista_especialistas.append({
                    "id": esp_id,
                    "nombre": espc['nombre'],
                    "titulo": espc['titulo'],
                    "cv": cv_data,
                    "servicios": [dict(s) for s in servicios_raw]
                })

            resultado.append({
                "id": esp['id'],
                "nombre": esp['nombre'],
                "especialistas": lista_especialistas
            })

        return jsonify(resultado), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==============================================================================
# ENDPOINT PARA /ubicaciones: ESTADOS -> CIUDADES -> SUCURSALES -> CONSULTORIOS
# ==============================================================================
@app.route('/api/ubicaciones', methods=['GET'])
def obtener_ubicaciones_arbol():

    db.session.expire_all()

    try:
        # Agrupación jerárquica (Estado -> Ciudad -> Sucursales)
        query_sucursales = text("""
            SELECT 
                s.id AS suc_id, 
                s.suc_name AS suc_nombre,                 s.suc_dir AS suc_direccion,
                c.id AS ciudad_id,
                c.c_name AS ciudad_nombre,
                e.idiso AS estado_id,
                e.e_name AS estado_nombre
            FROM idm_tc.idm_sucursal_tc s
            JOIN idm_tc.idm_ciudad_tc c ON s.id_cit = c.id
            JOIN idm_tc.idm_estado_tc e ON c.id_est = e.idiso
        """)
        filas = db.session.execute(query_sucursales).mappings().all()

        estados_dict = {}

        for f in filas:
            est_id = f['estado_id']
            ciu_id = f['ciudad_id']
            suc_id = f['suc_id']

            if est_id not in estados_dict:
                estados_dict[est_id] = {
                    "id": est_id,
                    "nombre": f['estado_nombre'],
                    "ciudades_dict": {}
                }

            if ciu_id not in estados_dict[est_id]["ciudades_dict"]:
                estados_dict[est_id]["ciudades_dict"][ciu_id] = {
                    "id": ciu_id,
                    "nombre": f['ciudad_nombre'],
                    "sucursales": []
                }

            # Consultar consultorios por sucursal
            query_consultorios = text("""
                SELECT id, con_num AS numero, con_type AS tipo, con_onl as online
                FROM idm_tc.idm_consultorio_tc
                WHERE id_suc = :suc_id
            """)
            consultorios_raw = db.session.execute(query_consultorios, {"suc_id": suc_id}).mappings().all()

            lista_consultorios = []
            for c in consultorios_raw:
                lista_consultorios.append({
                    "id": c['id'],
                    "numero": c['numero'],
                    "tipo": c['tipo'],
                    "servicios": ["Atención En Línea", "Valoración Clínica"] if c['online'] else ["Atención Presencial", "Valoración Clínica"] 
                })

            estados_dict[est_id]["ciudades_dict"][ciu_id]["sucursales"].append({
                "id": suc_id,
                "nombre": f['suc_nombre'],
                "direccion": f['suc_direccion'],
                "telefono": "322-288-7047",
                "consultorios": lista_consultorios
            })

        # Formatear la estructura JSON jerárquica
        resultado = []
        for est in estados_dict.values():
            ciudades_list = []
            for ciu in est["ciudades_dict"].values():
                ciudades_list.append(ciu)
            resultado.append({
                "id": est["id"],
                "nombre": est["nombre"],
                "ciudades": ciudades_list
            })

        return jsonify(resultado), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==============================================================================
# 2. FILTROS CRUZADOS EN TIEMPO REAL (PÁGINA /citas)
# ==============================================================================
@app.route('/api/citas/filtros', methods=['GET'])
def obtener_filtros_cruzados():

    db.session.expire_all()
    db.session.rollback()

    query_base1 = """
        WITH es_ser AS (
            SELECT iset.id_esp, iset.id_ser
            FROM idm_tc.idm_especialista_tc iet
            INNER JOIN idm_tr.idm_ser_esp_tr iset ON iset.id_esp = iet.id
        ),
        ser_epd AS (
            SELECT iet.id AS id_epd, iet.epd_nombre AS especialidad, ist.id AS id_ser, ist.ser_name AS servicio, ist.ser_onl AS ser_onl
            FROM idm_tc.idm_especialidad_tc iet
            INNER JOIN idm_tc.idm_servicio_tc ist ON ist.id_epd = iet.id
        ),
        es_suc AS (
            SELECT iet.id AS id_esp, CONCAT(iet.esp_name, ' ', iet.esp_ap_pat) AS especialista, 
                   iet.esp_activo, iest.id_suc, ist.suc_name AS sucursal, ist.suc_dir AS direccion
            FROM idm_tc.idm_especialista_tc iet
            INNER JOIN idm_tr.idm_esp_suc_tr iest ON iest.id_esp = iet.id 
            INNER JOIN idm_tc.idm_sucursal_tc ist ON ist.id = iest.id_suc
        ),
        es_len AS (
            SELECT el.id_esp, len.id AS id_len, len.len_nombre 
            FROM idm_tc.idm_lengua_tc len
            INNER JOIN idm_tr.idm_esp_len_tr el ON len.id = el.id_len
        )
        SELECT """

    query_base2 = """ FROM es_ser
        INNER JOIN ser_epd ON es_ser.id_ser = ser_epd.id_ser
        INNER JOIN es_suc ON es_suc.id_esp = es_ser.id_esp 
        INNER JOIN es_len ON es_ser.id_esp = es_len.id_esp
        WHERE es_suc.esp_activo = TRUE """

    # Parámetros recibidos de la URL
    esp_id = request.args.get('especialidad_id')
    suc_id = request.args.get('ubicacion_id')
    serv_id = request.args.get('servicio_id')
    espc_id = request.args.get('especialista_id')

    try:
        # Helper para agregar condiciones dinámicas sin repetir código
        def construir_where_dinamico(excluir_campo=None):
            condiciones = ""
            params = {}
            
            if esp_id and excluir_campo != 'especialidad':
                condiciones += " AND ser_epd.id_epd = :esp_id"
                params['esp_id'] = esp_id
                
            if suc_id and excluir_campo != 'ubicacion':
                condiciones += " AND es_suc.id_suc = :suc_id"
                if suc_id == 'IDMNT00000':
                    condiciones += " AND ser_epd.ser_onl=true"
                params['suc_id'] = suc_id
                
            if serv_id and excluir_campo != 'servicio':
                condiciones += " AND ser_epd.id_ser = :serv_id"
                params['serv_id'] = serv_id
                
            if espc_id and excluir_campo != 'especialista':
                condiciones += " AND es_ser.id_esp = :espc_id"
                params['espc_id'] = espc_id
                
            return condiciones, params

        # 1. Especialidades (se filtran según ubicación, servicio y especialista elegidos)
        cond_esp, params_esp = construir_where_dinamico(excluir_campo='especialidad')
        query_esp = query_base1 + " DISTINCT ser_epd.id_epd AS id, ser_epd.especialidad AS nombre " + query_base2 + cond_esp
        especialidades = db.session.execute(text(query_esp), params_esp).mappings().all()

        # 2. Ubicaciones (se filtran según especialidad, servicio y especialista elegidos)
        cond_suc, params_suc = construir_where_dinamico(excluir_campo='ubicacion')
        query_suc = query_base1 + " DISTINCT es_suc.id_suc AS id, es_suc.sucursal AS nombre, es_suc.direccion " + query_base2 + cond_suc
        ubicaciones = db.session.execute(text(query_suc), params_suc).mappings().all()

        # 3. Servicios (se filtran según especialidad, ubicación y especialista elegidos)
        cond_serv, params_serv = construir_where_dinamico(excluir_campo='servicio')
        query_serv = query_base1 + " DISTINCT ser_epd.id_ser AS id, ser_epd.servicio AS nombre " + query_base2 + cond_serv
        servicios = db.session.execute(text(query_serv), params_serv).mappings().all()

        # 4. Especialistas (se filtran según especialidad, ubicación y servicio elegidos)
        cond_espc, params_espc = construir_where_dinamico(excluir_campo='especialista')
        query_espc = query_base1 + " DISTINCT es_ser.id_esp AS id, es_suc.especialista AS nombre, ser_epd.especialidad AS titulo " + query_base2 + cond_espc
        especialistas = db.session.execute(text(query_espc), params_espc).mappings().all()

        # 5. Idiomas disponibles
        cond_lang, params_lang = construir_where_dinamico()
        query_lang = query_base1 + " DISTINCT es_len.id_len AS id, es_len.len_nombre AS nombre " + query_base2 + cond_lang
        idiomas = db.session.execute(text(query_lang), params_lang).mappings().all()

        return jsonify({
            "especialidades": [dict(r) for r in especialidades],
            "ubicaciones": [dict(r) for r in ubicaciones],
            "servicios": [dict(r) for r in servicios],
            "especialistas": [dict(r) for r in especialistas],
            "idiomas": [dict(r) for r in idiomas] if idiomas else [
                {"id": "es", "nombre": "Español"},
                {"id": "en", "nombre": "Inglés"}
            ]
        }), 200

    except Exception as e:
        print("Error en /api/citas/filtros:", str(e))
        return jsonify({"error": str(e)}), 500

# ==============================================================================
# 3. CONSULTA DE DISPONIBILIDAD (FECHAS Y HORAS)
# ==============================================================================
@app.route('/api/disponibilidad/proximas', methods=['GET'])
def obtener_proximas_citas():
    suc_id = request.args.get('ubicacion_id')
    serv_id = request.args.get('servicio_id')
    idioma_id = request.args.get('idioma_id', 'es')

    if not suc_id or not serv_id:
        return jsonify([]), 200

    try:
        inicio_busqueda = datetime.now() + timedelta(hours=24)
        citas_sugeridas = []

        # -------------------------------------------------------------------------
        # PASO A: CONFIGURACIÓN INICIAL DE CONSULTORIOS (Se ejecuta una sola vez)
        # -------------------------------------------------------------------------
        # A.1 Traer todos los consultorios de la sucursal
        query_consultorio = text("""
            SELECT id, CASE WHEN con_inf = TRUE THEN '1' ELSE '0' END 
            FROM idm_tc.idm_consultorio_tc 
            WHERE id_suc = :id_suc
        """)
        consultorios = db.session.execute(query_consultorio, {"id_suc": suc_id}).fetchall()

        # A.2 Averiguar si el servicio actual es infantil
        query_servicios = text("""
            SELECT CASE WHEN ser_inf = TRUE THEN '1' ELSE '0' END AS inf 
            FROM idm_tc.idm_servicio_tc
            WHERE id = :id_ser
        """)
        servicio_i = db.session.execute(query_servicios, {"id_ser": serv_id}).fetchone()
        ser_inf = str(servicio_i[0]) if servicio_i else "0"

        # A.3 Filtrar los IDs de consultorios válidos para este servicio y contar el total
        con_id = []
        if ser_inf == "1":
            # Si el servicio es infantil, solo contamos y usamos consultorios infantiles
            for con in consultorios:
                if str(con[1]) == "1":  # con_inf es True
                    con_id.append(str(con[0]))
            tot_con = len(con_id)
        else:
            # Si no es infantil, se usan todos los consultorios asignados a la sucursal
            con_id = [str(con[0]) for con in consultorios]
            tot_con = len(con_id)

        # Si la sucursal no tiene consultorios configurados para este tipo de servicio
        if not con_id or tot_con == 0:
            return jsonify([]), 200

        # -------------------------------------------------------------------------
        # PASO B: BÚSQUEDA DE ESPECIALISTAS Y SLOTS DISPONIBLES
        # -------------------------------------------------------------------------
        query_especialistas = text("""
            SELECT DISTINCT iet.id AS esp_id,case 
	            when ser.id_epd = 'IDMPSICO01' THEN CONCAT('Psic. ', iet.esp_name, ' ', iet.esp_ap_pat)
	            when ser.id_epd = 'IDMPSIQI01' then CONCAT('Psiq. ', iet.esp_name, ' ', iet.esp_ap_pat)
	            when ser.id_epd = 'IDMNUTRI01' then CONCAT('LN. ', iet.esp_name, ' ', iet.esp_ap_pat)
	        else CONCAT('PROF. ', iet.esp_name, ' ', iet.esp_ap_pat)
            end
            AS esp_nombre
                FROM idm_tc.idm_especialista_tc iet
                INNER JOIN idm_tr.idm_ser_esp_tr iset ON iset.id_esp = iet.id
                INNER JOIN idm_tr.idm_esp_suc_tr iest ON iest.id_esp = iet.id
                INNER JOIN idm_tr.idm_esp_len_tr ielt ON ielt.id_esp = iet.id
                INNER JOIN idm_tc.idm_servicio_tc ser ON ser.id = iset.id_ser
                WHERE iet.esp_activo = TRUE 
                AND iset.id_ser = :serv_id
                AND iest.id_suc = :suc_id
                AND ielt.id_len = :idioma_id
        """)

        espc_list = db.session.execute(query_especialistas, {
            "serv_id": serv_id, "suc_id": suc_id, "idioma_id": idioma_id
        }).mappings().all()

        if not espc_list:
            return jsonify([]), 200

        # Nuevas consultas preparadas con soporte para cit_fin (Citas largas)
        query_lleno = text("""
            SELECT cit_ini, MAX(cit_fin), COUNT(con_id) FROM idm_tr.idm_citas_tr
            WHERE CAST(cit_ini AS DATE) = :fecha
              AND cit_est != 'Cancelada'
              AND con_id IN :con_id
            GROUP BY cit_ini
            HAVING COUNT(con_id) >= :tot_con
        """).bindparams(bindparam("con_id", expanding=True))

        query_ocupadas = text("""
            SELECT cit_ini, cit_fin FROM idm_tr.idm_citas_tr 
            WHERE esp_id = :esp_id 
              AND CAST(cit_ini AS DATE) = :fecha 
              AND cit_est != 'Cancelada'
        """)

        # Iterar los próximos 14 días buscando slots
        for i in range(0, 14):
            fecha_eval = (inicio_busqueda + timedelta(days=i)).date()
            fecha_str = fecha_eval.strftime('%Y-%m-%d')
            dia_sem = fecha_eval.weekday()

            # -------------------------------------------------------------------------
            # PASO C: DETERMINAR HORAS CON CONSULTORIOS LLENOS PARA ESTE DÍA SPECÍFICO
            # -------------------------------------------------------------------------
            horas_consultorios_llenos = set()
            
            consul_existentes = db.session.execute(query_lleno, {
                "con_id": con_id, "fecha": fecha_str, "tot_con": tot_con
            }).fetchall()

            for cita in consul_existentes:
                try:
                    ini_dt = cita[0] if isinstance(cita[0], datetime) else datetime.strptime(str(cita[0]), "%Y-%m-%d %H:%M:%S")
                    fin_dt = cita[1] if isinstance(cita[1], datetime) else datetime.strptime(str(cita[1]), "%Y-%m-%d %H:%M:%S")
                    
                    temp_hora = ini_dt.replace(minute=0, second=0, microsecond=0)
                    limite_hora = fin_dt.replace(minute=0, second=0, microsecond=0)
                    
                    if temp_hora == limite_hora:
                        horas_consultorios_llenos.add(temp_hora.strftime("%H:%M"))
                    
                    while temp_hora < limite_hora:
                        horas_consultorios_llenos.add(temp_hora.strftime("%H:%M"))
                        temp_hora += timedelta(hours=1)
                except Exception as ex:
                    print(f"Error procesando consultorio lleno: {ex}", flush=True)

            # -------------------------------------------------------------------------
            # PASO D: EVALUAR CADA ESPECIALISTA
            # -------------------------------------------------------------------------
            for esp in espc_list:
                esp_id = esp['esp_id']

                # Verificar excepciones del especialista
                query_exc = text("""
                    SELECT 1 FROM idm_tr.idm_excepciones_tr
                    WHERE esp_id = :esp_id 
                      AND :fecha BETWEEN DATE(exc_ini) AND DATE(exc_fin)
                """)
                es_excepcion = db.session.execute(query_exc, {"esp_id": esp_id, "fecha": fecha_eval}).first()
                if es_excepcion:
                    continue

                # Consultar horario de trabajo
                query_tanda = text("""
                    SELECT hora_ini, hora_fin 
                    FROM idm_tr.idm_horarios_tc 
                    WHERE esp_id = :esp_id AND id_suc = :suc_id AND sem_day = :dia
                """)
                tanda = db.session.execute(query_tanda, {"esp_id": esp_id, "suc_id": suc_id, "dia": dia_sem}).mappings().first()

                if not tanda:
                    continue

                # Extraer y desglosar citas ocupadas del especialista actual (Minuto 0)
                citas_ocupadas_raw = db.session.execute(query_ocupadas, {"esp_id": esp_id, "fecha": fecha_eval}).fetchall()
                horas_especialista_ocupado = set()

                for cita in citas_ocupadas_raw:
                    try:
                        ini_dt = cita[0] if isinstance(cita[0], datetime) else datetime.strptime(str(cita[0]), "%Y-%m-%d %H:%M:%S")
                        fin_dt = cita[1] if isinstance(cita[1], datetime) else datetime.strptime(str(cita[1]), "%Y-%m-%d %H:%M:%S")
                        
                        temp_hora = ini_dt.replace(minute=0, second=0, microsecond=0)
                        limite_hora = fin_dt.replace(minute=0, second=0, microsecond=0)
                        
                        if temp_hora == limite_hora:
                            horas_especialista_ocupado.add(temp_hora.strftime("%H:%M"))
                        
                        while temp_hora < limite_hora:
                            horas_especialista_ocupado.add(temp_hora.strftime("%H:%M"))
                            temp_hora += timedelta(hours=1)
                    except Exception as ex:
                        print(f"Error procesando cita de especialista: {ex}", flush=True)

                # Construir slots de 1 hr basados en el horario del especialista
                fmt = "%H:%M:%S"
                h_ini = datetime.strptime(str(tanda['hora_ini']), fmt if len(str(tanda['hora_ini'])) == 8 else "%H:%M")
                h_fin = datetime.strptime(str(tanda['hora_fin']), fmt if len(str(tanda['hora_fin'])) == 8 else "%H:%M")

                curr = h_ini
                while curr <= h_fin:
                    hora_str = curr.strftime("%H:%M")
                    dt_slot = datetime.strptime(f"{fecha_str} {hora_str}", "%Y-%m-%d %H:%M")

                    # EL CAMBIO CLAVE: El slot no debe estar ocupado por el especialista 
                    # NI los consultorios deben estar llenos en esa hora en punto
                    if (dt_slot > inicio_busqueda and 
                        hora_str not in horas_especialista_ocupado and 
                        hora_str not in horas_consultorios_llenos):
                        
                        citas_sugeridas.append({
                            "especialista_id": esp['esp_id'],
                            "especialista_nombre": esp['esp_nombre'],
                            "fecha": fecha_str,
                            "hora": hora_str
                        })
                        
                        # Si ya juntamos las 6 citas más próximas del pool general, terminamos inmediatamente
                        if len(citas_sugeridas) >= 6:
                            return jsonify(citas_sugeridas), 200

                    curr += timedelta(hours=1)

        return jsonify(citas_sugeridas), 200

    except Exception as e:
        print("Error en /api/disponibilidad/proximas:", str(e), flush=True)
        return jsonify({"error": str(e)}), 500



# ==============================================================================
# ENDPOINT FECHAS (Limpiado y corregido sin hardcoding)
# ==============================================================================
@app.route('/api/disponibilidad/fechas', methods=['GET'])
def obtener_fechas_disponibles():
    especialista_id = request.args.get('especialista_id')
    ubicacion_id = request.args.get('ubicacion_id')

    if not especialista_id or not ubicacion_id:
        return jsonify([]), 200

    try:
        hoy = date.today()
        dias_disponibles = []

        query_tanda = text("""
            SELECT DISTINCT sem_day AS day
            FROM idm_tr.idm_horarios_tc
            WHERE esp_id = :espc_id AND id_suc = :suc_id
        """)
        dias_laborables_raw = db.session.execute(query_tanda, {"espc_id": especialista_id, "suc_id": ubicacion_id}).fetchall()
        dias_permitidos = [r[0] for r in dias_laborables_raw]

        exc_query = text(""" 
            SELECT DATE(exc_ini) AS exc_ini, DATE(exc_fin) AS exc_fin
             FROM idm_tr.idm_excepciones_tr as ex
             INNER JOIN idm_tr.idm_horarios_tc as iht on  ex.esp_id = iht.esp_id 
              WHERE ex.esp_id = :espc_id AND DATE(exc_fin) >= CURRENT_DATE 
               AND (date_part('isodow', exc_ini)::INTEGER - 1) = sem_day
               AND exc_ini::time <= hora_ini 
               AND exc_fin::time >= hora_fin
        """)
        dias_excepcion = db.session.execute(exc_query, {"espc_id": especialista_id}).fetchall()

        for i in range(1, 31):
            fecha_eval = hoy + timedelta(days=i)
            num_dia_semana = fecha_eval.weekday() # 0 = Lunes

            if num_dia_semana in dias_permitidos:
                es_excepcion = False
                for exc in dias_excepcion:
                    if exc[0] <= fecha_eval <= exc[1]:
                        es_excepcion = True
                        break
                if not es_excepcion:
                    dias_disponibles.append(fecha_eval.strftime('%Y-%m-%d'))

        return jsonify(dias_disponibles), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ==============================================================================
# ENDPOINT HORAS (Corregido y optimizado)
# ==============================================================================
@app.route('/api/disponibilidad/horas', methods=['GET'])
def obtener_horas_disponibles():
    especialista_id = request.args.get('especialista_id')
    ubicacion_id = request.args.get('ubicacion_id')
    fecha_str = request.args.get('fecha')
    servicio_id = request.args.get('servicio_id')

    if not especialista_id or not fecha_str:
        print(especialista_id)
        return jsonify([]), 200

    try:
        # 1. Obtener horario estándar configurado en Tanda para el día seleccionado
        fecha_obj = datetime.strptime(fecha_str, '%Y-%m-%d').date()
        #mapa_dias = {0: "lunes", 1: "martes", 2: "miercoles", 3: "jueves", 4: "viernes", 5: "sabado", 6: "domingo"}
        mapa_dias = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
        dia_nombre = mapa_dias[fecha_obj.weekday()]
        print("id del servicio:",servicio_id)

        query_tanda = text("""
            SELECT hora_ini, hora_fin
            FROM idm_tr.idm_horarios_tc
            WHERE esp_id = :espc_id AND sem_day = :dia AND id_suc = :suc
        """)
        tanda = db.session.execute(query_tanda, {"espc_id": especialista_id, "dia": dia_nombre, "suc":ubicacion_id}).mappings().first()

        # Horario base por defecto si no hay tanda específica
        hora_inicio = tanda['hora_ini'] if tanda else ""
        hora_fin = tanda['hora_fin'] if tanda else ""

        # 2. Consultar citas ya reservadas en RDS para evitar solapamientos
        #2.1 extraer todos los consultorios y la etiqueta de si es infantil o no
        query_consultorio= """select id, case when con_inf = true then '1' else '0' end from idm_tc.idm_consultorio_tc where id_suc=:id_suc"""

        #2.2 averiguar si el servicio que se da es infantil
        query_servicios=text("""select case when ser_inf = true then '1' else '0' end as inf from idm_tc.idm_servicio_tc
            where id = :id_ser""")

        #2.3 total de consultorios :si el servicio no es infantil se trae la consulta como está, si no se agrega and con_inf is True
        query_totalCons= """select count(id) from idm_tc.idm_consultorio_tc where id_suc=:id_suc"""

        #2.4 trae la suma de consultorios por hora.
        query_lleno = text("""
            SELECT cit_ini, MAX(cit_fin), count(con_id) as count_con FROM idm_tr.idm_citas_tr
            WHERE cast(cit_ini as date) =:fecha
              AND cit_est != 'Cancelada'
              and con_id in :con_id
            group by  cit_ini
            having count(id) >= :tot_con
        """).bindparams(bindparam("con_id", expanding=True))

        query_ocupadas = text("""
            SELECT cit_ini, cit_fin FROM idm_tr.idm_citas_tr
            WHERE esp_id = :espc_id
              AND cast(cit_ini as date) = :fecha
              AND cit_est != 'Cancelada'
        """)

        query_excepciones = text("""
            select exc_ini, exc_fin
              FROM idm_tr.idm_excepciones_tr as ex
               WHERE esp_id = :espc_id 
                AND DATE(exc_ini) = :fecha
                AND DATE(exc_fin) = :fecha
        """)

        #paso 2.1
        consultorios = db.session.execute(text(query_consultorio), {"id_suc":ubicacion_id}).fetchall()
        #ser_inf = db.session.execute(query_lleno, {"fecha": fecha_str, "con_id":con_id}).fetchall()
        #paso 2.2
        servicio_i = db.session.execute(query_servicios, {"id_ser":servicio_id}).fetchall()
        ser_inf=[str(ser[0]) for ser in servicio_i]
        print("ser_inf es:",ser_inf)
        #paso 2.3
        con_id = []
        if str(ser_inf[0]) == "1":
            query_totalCons += " AND con_inf is true"
            for con in consultorios:
                if str(con[1]) == "1":
                    con_id.append(str(con[0]))

                else:
                    pass

        else:
            con_id=[str(con[0]) for con in consultorios]
        print("verificando posicion del error")
        total_consul = db.session.execute(text(query_totalCons), {"id_suc":ubicacion_id}).fetchone()
        tot_con=int(total_consul[0]) if total_consul else 0
        print("query total consul exitoso")

                # --- PROCESAMIENTO DE RANGOS DE HORAS OCUPADAS ---
        horas_bloqueadas_set = set()

        # Función auxiliar para desglosar citas largas en bloques de 1 hora
        def registrar_horas_en_rango(citas_lista):
            for cita in citas_lista:
                # cita[0] es cit_ini, cita[1] es cit_fin
                # Extraemos solo la parte del tiempo (HH:MM)
                try:
                    ini_time = cita[0].time() if hasattr(cita[0], 'time') else datetime.strptime(str(cita[0])[11:16], "%H:%M").time()
                    fin_time = cita[1].time() if hasattr(cita[1], 'time') else datetime.strptime(str(cita[1])[11:16], "%H:%M").time()

                    ini_time = ini_time.replace(minute=0)
                    fin_time = fin_time.replace(minute=0)
                    # Iteramos hora por hora desde el inicio hasta el fin de la cita
                    temp_hora = datetime.combine(datetime.today(), ini_time)
                    limite_hora = datetime.combine(datetime.today(), fin_time)
                    print("Hora de inicio",temp_hora)
                    print("hora de fin",limite_hora)
                    
                    while temp_hora < limite_hora:
                        horas_bloqueadas_set.add(temp_hora.strftime("%H:%M"))
                        temp_hora += timedelta(hours=1)
                except Exception as ex:
                    print(f"Error procesando rango de cita: {ex}",flush=True)

        # Procesar consultorios llenos (Paso 2.4)
        print("con_id:", con_id, "fecha:", fecha_str, "tot_con:", tot_con, flush=True)
        print("para query lleno este es con_id:",con_id)
        consul_existentes = db.session.execute(query_lleno, {"con_id": con_id, "fecha": fecha_str, "tot_con": tot_con}).fetchall()
        print("consul existentes:", consul_existentes, flush=True)
        registrar_horas_en_rango(consul_existentes)
        print("primer paso:",horas_bloqueadas_set)
        # Procesar citas del especialista actual
        citas_existentes = []
        try:
            citas_existentes = db.session.execute(query_ocupadas, {"espc_id": especialista_id, "fecha": fecha_str}).fetchall()
        except Exception as e:
            print(f"Error consultando citas: {e}",flush=True)
            
        print("citas existentes:", citas_existentes, flush=True)
        registrar_horas_en_rango(citas_existentes)

        excepciones = db.session.execute(query_excepciones, {"espc_id": especialista_id, "fecha": fecha_str}).fetchall()
        registrar_horas_en_rango(excepciones)

        print("segundo paso:",horas_bloqueadas_set)
        # Convertimos el set a una lista para mantener compatibilidad con tu código final
        horas_ocupadas = list(horas_bloqueadas_set)
        print("Todas las horas bloqueadas (incluyendo citas largas):", horas_ocupadas)

        #consul_existentes = db.session.execute(query_lleno, {"con_id":con_id, "fecha": fecha_str, "tot_con":tot_con}).fetchall()
        #horas_ocupadas1 = [str(c[0])[11:16] for c in consul_existentes]
        #print(horas_ocupadas1)

        #citas_con = db.session.execute(query_lleno, {"fecha": fecha_str, "con_id":con_id}).fetchall()
        #print(citas_con)
        #citas_existentes = []
        #try:
        #    citas_existentes = db.session.execute(query_ocupadas, {"espc_id": especialista_id, "fecha": fecha_str}).fetchall()

        #except Exception as e:
        #    horas_ocupadas2 = []


        #horas_ocupadas2 = [str(c[0])[11:16] for c in citas_existentes] if citas_existentes else []
        #horas_ocupadas = []

        #for hora in horas_ocupadas1:
        #    horas_ocupadas.append(hora)

        #for hora in horas_ocupadas2:
        #    horas_ocupadas.append(hora)

        print("horas ocupadas:",horas_ocupadas)

        # 3. Generar slots de 1 hora
        fmt = "%H:%M:%S"
        h_ini = datetime.strptime(str(hora_inicio), fmt if len(str(hora_inicio)) == 8 else "%H:%M")
        h_fin = datetime.strptime(str(hora_fin), fmt if len(str(hora_fin)) == 8 else "%H:%M")

        slots_libres = []
        slots_libres1 = []
        curr = h_ini
        while curr <= h_fin:
            hora_formatted = curr.strftime("%H:%M")
            if hora_formatted not in horas_ocupadas:
                slots_libres.append(hora_formatted)
            curr += timedelta(hours=1)

        return jsonify(slots_libres), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

#==============================================================================
# 3.1 REGISTRO DE LOS DATOS EN FORMULARIO
#==============================================================================
@app.route('/api/consentimiento', methods=['GET'])
def obtener_consentimiento():
    especialidad_id = request.args.get('especialidad_id')
    servicio_id = request.args.get('servicio_id')

    print(f"-> servicio_id recibido: {servicio_id}")
    print(f"-> especialidad_id recibido: {especialidad_id}")

    if not servicio_id:
        return jsonify({'error': 'Falta el parámetro servicio_id'}), 400
    
    try:
        # Consulta filtrando por el id del servicio en idm_tc.idm_servicio_tc
        query = text("""
            SELECT 
                id, 
                ser_name, 
                ser_inf 
            FROM idm_tc.idm_servicio_tc 
            WHERE id = :servicio_id
        """)
        
        result = db.session.execute(query, {'servicio_id': servicio_id}).fetchone()

        if not result:
            return jsonify({'error': 'Servicio no encontrado'}), 404

        # Evaluamos ser_inf (asumiendo que en MySQL es 1/0 o True/False)
        es_servicio_infantil = bool(result.ser_inf)
        print(especialidad_id)
        # Texto base del consentimiento (puedes parametrizarlo desde BD si gustas)
        if especialidad_id == "IDMPSICO01":
            texto_consentimiento="""Manifiesta la veracidad de los datos personales aportados para la confección de su historia
clínica, y que ha recibido información suficiente acerca del tratamiento que realizará en la
institución, al que presta voluntariamente su consentimiento, de acuerdo a las condiciones
que se transcriben a continuación:
I. Se realizará un tratamiento terapéutico, el cual se orientará a la atención de los motivos de
consulta expuestos.
II. El abordaje terapéutico será el adecuado a cada situación clínica y se inscribe dentro del
marco de la psicoterapia basada en la evidencia.
III. Por la presente dejo constancia que he sido informado de las características técnicas del
enfoque a aplicar y me han sido respondidas todas las dudas pertinentes.
IV. El tratamiento se llevará a cabo con una determinada periodicidad indicada por el
profesional tratante. Las consultas tienen una duración promedio de 50 minutos, pudiendo
haber variaciones según lo que acontezca en su transcurso.
V. El plazo del tratamiento será el que el profesional juzgue necesario de acuerdo al motivo de
consulta expuesto en la primera sesión, mismo que puede ser modificado (extendido o
acortado) mediante un nuevo acuerdo, en función del grado de avance en el cumplimiento
de los objetivos establecidos.
VI. El profesional como miembro de la institución tiene el deber legal de denunciar si conoce la
existencia de un delito, como es el abuso sexual contra las infancias, y el estado tiene la
obligación de proteger la integridad del menor, tal como está comprometido en la LEY DE
PROTECCIÓN DE LOS DERECHOS DE LAS NIÑAS, NIÑOS Y LOS ADOLESCENTES
DEL ESTADO DE NAYARIT.
VII. El tratamiento podrá ser interrumpido en forma unilateral por el consultante en el momento
en que lo considere oportuno, informando de esta decisión al profesional tratante, quien
evaluará si esta interrupción puede ser perjudicial para el mismo o para terceros.
Reservándose el derecho de notificar a quien considere responsable.
VIII. Se garantiza la confidencialidad, respecto a la información recibida por el consultante, cuyo
límite sólo podrá ser vulnerado con causa justa de acuerdo a lo establecido en el código
ético del ejercicio de la profesión.
IX. El consultante se responsabiliza a seguir las indicaciones terapéuticas que el profesional
tratante le imparta: inter consulta con profesionales médicos y no médicos y eventual
derivación institucional.
X. El profesional no está autorizado para brindar documentos de uso legal en ninguna de sus
formas, debido a que no cuenta con la formación señalada para hacerlo.
En modalidad en línea el consultante se compromete a facilitar un espacio con las
siguientes especificaciones: privado, exento de ruido, red de conexión wifi, iluminación y
ventilación adecuada.
XII. En modalidad a domicilio el consultante se compromete a facilitar un espacio con las
siguientes especificaciones: privado, exento de ruido, iluminación y ventilación adecuada.
XIII. La institución se reserva el derecho de brindar la atención, de acuerdo a los derechos y
obligaciones establecidos en el código ético del ejercicio de la profesión y al cumplimiento
de sus políticas internas."""

        elif especialidad_id == "IDMPSIQI01":
            texto_consentimiento = """Manifiesta la veracidad de los datos personales aportados para la confección de su historia
clínica, y que ha recibido información suficiente acerca del tratamiento que realizará en la
institución, al que presta voluntariamente su consentimiento, de acuerdo a las condiciones
que se transcriben a continuación:
I. Se realizará un tratamiento psiquiátrico, el cual se orientará a la atención de los motivos de
consulta expuestos.
II. El abordaje psiquiátrico será el adecuado a cada situación clínica y se inscribe dentro del
marco de la psiquiatría basada en la evidencia.
III. Por la presente dejo constancia que he sido informado de las características del tratamiento
y me han sido respondidas todas las dudas pertinentes.
IV. El tratamiento se llevará a cabo con una determinada periodicidad indicada por el
profesional tratante. Las consultas tienen una duración promedio de 50 minutos, pudiendo
haber variaciones según lo que acontezca en su transcurso.
V. El plazo del tratamiento será el que el profesional juzgue necesario de acuerdo al motivo de
consulta expuesto en la primera sesión, mismo que puede ser modificado (extendido o
acortado) mediante un nuevo acuerdo, en función del grado de avance en el cumplimiento
del tratamiento establecido.
VI. El tratamiento podrá ser interrumpido en forma unilateral por el paciente en el momento en
que lo considere oportuno, informando de esta decisión al profesional tratante, quien
evaluará si esta interrupción puede ser perjudicial para el mismo o para terceros.
Reservándose el derecho de notificar a quien considere responsable.
VII. Se garantiza la confidencialidad, respecto a la información recibida por el paciente, cuyo
límite sólo podrá ser vulnerado con causa justa de acuerdo a lo establecido en el código
ético del ejercicio de la profesión.
VIII. El paciente se responsabiliza a seguir las indicaciones que el profesional tratante le imparta:
inter consulta con profesionales médicos y no médicos y eventual derivación institucional.
IX. El profesional no está autorizado para brindar documentos de uso legal en ninguna de sus
formas, debido a que no cuenta con la formación señalada para hacerlo.
X. En modalidad en línea el consultante se compromete a facilitar un espacio con las
siguientes especificaciones: privado, exento de ruido, red de conexión wifi, iluminación y
ventilación adecuada.
XI. La institución se reserva el derecho de brindar la atención, de acuerdo a los derechos y
obligaciones establecidos en el código ético del ejercicio de la profesión y al cumplimiento
de sus políticas internas. """

        elif especialidad_id == "IDMNUTRI01":
            texto_consentimiento = """ Manifiesta la veracidad de los datos personales y médicos aportados para la confección de
su historia clínica, y que ha recibido información suficiente acerca del tratamiento que
realizará en la institución, al que presta voluntariamente su consentimiento, de acuerdo a las
condiciones que se transcriben a continuación:
Entiéndase por:
I. Primera consulta: Equivale a la primera sesión de evaluación, o una sesión después de 4
meses inactivo, incluye su plan con evaluación y valoración.
II. Control: Se refiere a las consultas posteriores a la primera evaluación, en estas se renueva
su plan, se da seguimiento a sus resultados, se llevan a cabo sesiones educativas referente
a temas relacionados a sus objetivos.
III. Aplicación de seguimiento: Es una red social entre consultante y nutrióloga donde se
notifica el proceso del tratamiento nutricional
a) Mantenimiento de la aplicación: Nutrimind es un software para nutriólogos que
realiza mantenimiento constante para mejorar la aplicación, por lo que en algunas
ocasiones se encontrará fuera de servicio por causas externas al nutriólogo.
b) Toda información de salud compartida por el paciente es confidencial (expediente
clínico, fotografías, estudios clínicos, etc.)
c) La cuenta se dará de baja en caso de que el paciente deje de asistir a consulta o
una vez dado de alta
IV. Medidas antropométricas:
Durante la valoración y tratamiento es necesario que el paciente retire zapatos, chamarras,
dando oportunidad a tomar medidas más exactas.
Se tendrá en cuenta que el trabajo realizado durante el proceso de tratamiento nutricional
puede tener consecuencias negativas o positivas dependiendo del compromiso que usted
aplique. A continuación se expresan algunas consecuencias que se pueden producir
durante el proceso de mejorar hábitos alimenticios y que el paciente debe de ser
consciente, tales como:
a) Aumento en la frecuencia urinaria y fecal, estreñimiento ó diarrea
b) Poca tolerancia a alimentos con alto contenido en grasas
c) Cambios hormonales
d) Cambio de tallas en prendas de vestir
e) El uso de anticonceptivos y medicamentos puede influir en el avance en el
tratamiento nutricional
f) El estrés causado por causas externas puede provocar poco o nulo avance en el
tratamiento nutricional

IV. El tratamiento se llevará a cabo con una determinada periodicidad indicada por el
profesional tratante. Las consultas tienen una duración promedio de 50 minutos, pudiendo
haber variaciones según lo que acontezca en su transcurso.
acortado) mediante un nuevo acuerdo, en función del grado de avance en el cumplimiento
de los objetivos establecidos.
VI. El tratamiento podrá ser interrumpido en forma unilateral por el consultante en el momento
en que lo considere oportuno, informando de esta decisión al profesional tratante, quien
evaluará si esta interrupción puede ser perjudicial para el mismo o para terceros.
Reservándose el derecho de notificar a quien considere responsable.
VII. Se garantiza la confidencialidad, respecto a la información recibida por el paciente, cuyo
límite sólo podrá ser vulnerado con causa justa de acuerdo a lo establecido en el código
ético del ejercicio de la profesión.
VIII. El paciente se responsabiliza a seguir las indicaciones que el profesional tratante le imparta:
inter consulta con profesionales médicos y no médicos y eventual derivación institucional.
IX. El profesional no está autorizado para brindar documentos de uso legal en ninguna de sus
formas, debido a que no cuenta con la formación señalada para hacerlo.
X. En modalidad en línea el paciente se compromete a facilitar un espacio con las siguientes
especificaciones: privado, exento de ruido, red de conexión wifi, iluminación y ventilación
adecuada. Así mismo se compromete a enviar en tiempo y forma la información requerida,
de lo contrario será dado de baja sin previo aviso.
XI. La institución se reserva el derecho de brindar la atención, de acuerdo a los derechos y
obligaciones establecidos en el código ético del ejercicio de la profesión y al cumplimiento
de sus políticas internas.
Leído que fuere el consentimiento reconozco que he tenido la oportunidad de hacer
preguntas sobre mi valoración nutricional, expediente clínico y tratamiento. Comprendo que
la consulta nutricional no presenta un sustituto para medicamentos o tratamientos médicos.
También comprendo que se me recomienda trabajar junto con mi médico de atención
primaria para tratar cualquier condición que pueda tener. """
        else:
            pass
        
        if es_servicio_infantil:
            texto_consentimiento += (
                " Declaro adicionalmente que soy el/la tutor(a) legal del menor "
                "ingresado en este formulario y autorizo su atención especializada."
            )

        return jsonify({
            'servicio_id': result.id,
            'servicio_nombre': result.ser_name,
            'es_para_menores': es_servicio_infantil,
            'texto': texto_consentimiento
        }), 200

    except Exception as e:
        print(f"Error al consultar servicio: {e}")
        return jsonify({'error': 'Error interno del servidor'}), 500


# ==============================================================================
# 4. REGISTRO FINAL DE CITA (TRANSACCIÓN ATÓMICA AL FINAL DEL FLUJO MULTI-PASO)
# ==============================================================================
@app.route('/api/citas/confirmar-reserva', methods=['POST'])
def confirmar_reserva_completa():
    """
    Recibe los datos consolidados del flujo completo:
    /citas + /registro (datos personales + consentimiento + aviso privacidad) + /facturacion
    """
    data = request.json
    
    try:
        # Iniciar transacción relacional en RDS
        with db.session.begin_nested():
            # 1. Crear o actualizar el registro del Paciente
            paciente_data = data.get('paciente', {})
            query_paciente = text("""
                INSERT INTO idm_tc.idm_paciente_tc (pac_name, pac_ap_pat, pac_ap_mat, pac_edad, id_elec, id_pas)
                VALUES (:nombre, :ap_pat, :ap_mat, :edad, :email, :tel)
                RETURNING id;
            """)
            pac_id = db.session.execute(query_paciente, {
                "nombre": paciente_data.get('nombre'),
                "ap_pat": paciente_data.get('apellidoPaterno'),
                "ap_mat": paciente_data.get('apellidoMaterno', ''),
                "edad": paciente_data.get('edad', 0),
                "email": paciente_data.get('email'),
                "tel": paciente_data.get('telefono')
            }).scalar()

            # 2. Insertar Registro de Cita
            cita_info = data.get('cita', {})
            query_cita = text("""
                INSERT INTO idm_tr.idm_citas_tr (id_pac, esp_id, id_suc, id_ser, cit_f, cit_hi, cit_est)
                VALUES (:pac_id, :esp_id, :suc_id, :ser_id, :fecha, :hora, 'Confirmada')
                RETURNING id;
            """)
            cita_id = db.session.execute(query_cita, {
                "pac_id": pac_id,
                "esp_id": cita_info.get('especialistaId'),
                "suc_id": cita_info.get('ubicacionId'),
                "ser_id": cita_info.get('servicioId'),
                "fecha": cita_info.get('fecha'),
                "hora": cita_info.get('hora')
            }).scalar()

            # 3. Guardar consentimiento y aviso de privacidad aceptado
            query_legal = text("""
                INSERT INTO Pagos (id_cit, dt_monto, dt_confirmado, dt_coordinador)
                VALUES (:cita_id, :monto, FALSE, 'Sistema-Web')
            """)
            db.session.execute(query_legal, {
                "cita_id": cita_id,
                "monto": cita_info.get('monto', 0.0)
            })

        db.session.commit()
        return jsonify({
            "status": "success",
            "message": "Cita registrada con éxito",
            "cita_id": cita_id
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


# =============================================================================# 5. OBTENER INSTRUCCIONES DE PAGO PARA PANTALLA FINAL
# ==============================================================================

CUENTA_FISCAL = {
    "banco": "BANAMEX",
    "titular": "Valeria Berenice Flores Torres",
    "clabe": "002 375 70221452664 4",
    "numeroCuenta": "70221452664"
}

CUENTA_GENERAL = {
    "banco": "SANTANDER",
    "titular": "Epifanio Flores García",
    "clabe": "014 375 56595777989 9",
    "numeroCuenta": "56595777989"
}

@app.route('/api/instrucciones-pago', methods=['GET'])
def obtener_instrucciones_pago():
    try:
        servicio_id = request.args.get('servicio_id') or request.args.get('cita_id')
        ubicacion_id = request.args.get('ubicacion_id')
        requiere_factura_str = request.args.get('requiere_factura','false')
        requiere_factura = requiere_factura_str.lower() in ['true', '1']

        pacName = {
        "pac_name": request.args.get('nombres', ''),
        "pac_ap_pat": request.args.get('apellidoPaterno', ''),
        "pac_ap_mat": request.args.get('apellidoMaterno', '')
        }

        # Seleccionar la cuenta adecuada según requerimiento fiscal
        cuenta_seleccionada = CUENTA_FISCAL if requiere_factura_str == "true" else CUENTA_GENERAL

        monto = "600.00"

        # Si tenemos servicio_id, consultamos el monto real del servicio
        if servicio_id and ubicacion_id:
            query_servicio = text("""
                select itt.tar_mon as ser_costo from idm_tc.idm_tarifa_tc itt 
                    where id_ser = :servicio_id
                    and id_suc = :ubicacion_id
                    and sesiones = 1
            """)
            res_servicio = db.session.execute(query_servicio, {"servicio_id": servicio_id,"ubicacion_id":ubicacion_id}).mappings().first()
            if res_servicio and res_servicio.get('ser_costo'):
                monto = str(res_servicio['ser_costo'])

        # Generar concepto usando timestamp actual o referencia temporal
        timestamp_concepto = datetime.now().strftime('%m%d%H%M')
        ref_id = pacName["pac_name"] + "-" + pacName["pac_ap_pat"] + "-" + pacName["pac_ap_mat"]
        concepto_generado = f"CITA-{ref_id}-{timestamp_concepto}"

        return jsonify({
            **cuenta_seleccionada,
            "concepto": concepto_generado,
            "monto": monto,
            "reqfact":requiere_factura_str,
            "refact":requiere_factura
        }), 200

    except Exception as e:
        return jsonify({"error": f"Error al generar instrucciones de pago: {str(e)}"}), 500


@app.route('/api/citas/confirmar', methods=['POST'])
def confirmar_y_guardar_cita():
    try:
        payload = request.get_json() or {}
        
        cita = payload.get('cita', {})
        paciente = payload.get('paciente', {})
        firma = payload.get('firma', {})
        facturacion = payload.get('facturacion', {})

        id_suc = cita.get('ubicacion_id', '1')
        id_ser = cita.get('servicio_id', '1')

        # ---------------------------------------------------------------------
        # 1. GENERAR ID DE PACIENTE E INSERTAR
        # ---------------------------------------------------------------------
        query_busqueda_paciente1 = text("""SELECT MAX(CAST(SUBSTRING(id, 4, 7) AS INT)) AS max_num
                FROM idm_tc.idm_paciente_tc""")
        
        query_busqueda_ciudad = text("""SELECT SUBSTRING(id_cit, 3, 2) AS city 
                FROM idm_tc.idm_sucursal_tc
                WHERE id = :id_suc""")
        
        query_busqueda_servicio = text("""SELECT CASE WHEN ser_inf is true then '1' else '0' end AS ser_inf
                FROM idm_tc.idm_servicio_tc
                WHERE id = :id_ser
                ORDER BY 1 asc
                limit 1""")

        ##Pendiente: corregir query, debe separar ciudad y paciente, crear if para el menor
        idpac = db.session.execute(query_busqueda_paciente1).mappings().first()
        idcit = db.session.execute(query_busqueda_ciudad, {"id_suc": id_suc}).mappings().first()
        serinf = db.session.execute(query_busqueda_servicio, {"id_ser": id_ser}).mappings().first()
        
        # Resguardo por si la consulta devuelve None
        cur_city = idcit["city"] if idcit else "01"
        max_num = idpac["max_num"] if idpac else 0
        ser_inf = serinf["ser_inf"] if serinf else "0"

        new_id_num = str(max_num + 1)
        zeros_pac = 7 - len(new_id_num)
        id_pac = "I" + cur_city + ("0" * zeros_pac) + new_id_num

        query_in_pac = text("""
            INSERT INTO idm_tc.idm_paciente_tc
            (id, pac_name, pac_ap_pat, pac_ap_mat, pac_edad)
            VALUES (:id_pac, :pac_name, :pac_ap_pat, :pac_ap_mat, :pac_edad)
        """)

        ################### Menor #############################################
        if str(ser_inf) == "1":
            query_menr = text(""" SELECT COALESCE(MAX(CAST(SUBSTRING(id, 4, 7) AS INT)), 0) AS id_num 
                FROM idm_tc.idm_menor_tc """)
            idmen = db.session.execute(query_menr).mappings().first()
            max_men = int(idmen["id_num"]) if idmen else int(0)

            new_id_men = str(max_men + 1)
            zeros_men = 7 - len(new_id_men)
            id_men = "M" + cur_city + ("0" * zeros_men) + new_id_men

            query_in_men = text(""" INSERT INTO idm_tc.idm_menor_tc
                                (id, men_name, men_edad, id_tutor)
                                VALUES(:id_men, :men_name, :men_age, :id_pac) """)
        else:
            pass

        # ---------------------------------------------------------------------
        # 2. GENERAR ID DE TELÉFONO E INSERTAR
        # ---------------------------------------------------------------------
        query_busqueda_tel = text("""
            WITH tel AS (
                SELECT SUBSTRING(id, 1, 2) AS id_city, CAST(SUBSTRING(id, 4, 8) AS INT) AS id_num 
                FROM idm_tc.idm_telefono_tc
            )
            SELECT tel.id_city AS city, COALESCE(MAX(id_num), 0) AS max_num 
            FROM tel
            WHERE tel.id_city = :cur_city
            GROUP BY tel.id_city
        """)

        idtel = db.session.execute(query_busqueda_tel, {"cur_city": cur_city}).mappings().first()
        max_num_tel = idtel["max_num"] if idtel else 0
        new_id_tel = str(max_num_tel + 1)
        zeros_tel = 8 - len(new_id_tel)
        id_tel = cur_city + ("0" * zeros_tel) + new_id_tel

        query_in_tel = text("""
            INSERT INTO idm_tc.idm_telefono_tc
            (id, cla_pai, num_tel, id_pac)
            VALUES (:id_tel, '52', :num_tel, :id_pac)
        """)

        
        # ---------------------------------------------------------------------
        # 3. GENERAR ID DE EMAIL E INSERTAR
        # ---------------------------------------------------------------------
        query_busqueda_ema = text("""
            WITH ema AS (
                SELECT SUBSTRING(id, 1, 2) AS id_city, CAST(SUBSTRING(id, 4, 8) AS INT) AS id_num 
                FROM idm_tc.idm_email_tc
            )
            SELECT ema.id_city AS city, COALESCE(MAX(id_num), 0) AS max_num 
            FROM ema
            WHERE ema.id_city = :cur_city
            GROUP BY ema.id_city
        """)



        idema = db.session.execute(query_busqueda_ema, {"cur_city": cur_city}).mappings().first()
        max_num_ema = idema["max_num"] if idema else 0
        new_id_ema = str(max_num_ema + 1)
        zeros_ema = 8 - len(new_id_ema)
        id_ema = cur_city + ("0" * zeros_ema) + new_id_ema

        query_in_ema = text("""
            INSERT INTO idm_tc.idm_email_tc
            (id, dir_ema, id_pac)
            VALUES (:id_ema, :dir_ema, :id_pac)
        """)

        # EJECUTAR INSERCIONES DE PACIENTE, TELÉFONO Y EMAIL
        params_paciente = {
            "id_pac": id_pac,
            "pac_name": paciente.get('nombres', ''),
            "pac_ap_pat": paciente.get('apellidoPaterno', ''),
            "pac_ap_mat": paciente.get('apellidoMaterno', ''),
            "pac_edad": int(paciente.get('edad', 0)) if str(paciente.get('edad', '')).isdigit() else 0,
            "num_tel": paciente.get('celular', ''),
            "id_tel": id_tel,
            "id_ema": id_ema,
            "dir_ema": paciente.get('email', '')
        }

        db.session.execute(query_in_pac, params_paciente)
        db.session.execute(query_in_tel, params_paciente)
        db.session.execute(query_in_ema, params_paciente)
        if str(ser_inf) == "1":
            params_menor = {
            "id_men":id_men,
            "men_name":paciente.get('nombreMenor',''),
            "men_age":paciente.get('edadMenor',''),
            "id_pac":id_pac
            }
            db.session.execute(query_in_men, params_menor)
        else:
            pass
        # ---------------------------------------------------------------------
        # 4. CALCULAR ID DE CITA Y REGISTRAR EN TABLAS TR
        # ---------------------------------------------------------------------
        query_max_cita = text("""SELECT COALESCE(MAX(id), 0) AS max_id FROM idm_tr.idm_citas_tr""")
        step1 = db.session.execute(query_max_cita).mappings().first()
        id_cita = (step1["max_id"] if step1 else 0) + 1

        # Tiempos de inicio y fin
        fecha_str = cita.get('fecha')
        hora_str = cita.get('hora')
        cit_ini = datetime.strptime(f"{fecha_str} {hora_str}", "%Y-%m-%d %H:%M")
        cit_fin = cit_ini + timedelta(hours=1)

        #asignar consultorio ###########################################################################

        query_cons = text(""" 
            with concit as(

	            select con_id from idm_tr.idm_citas_tr
		            where cit_ini between :cit_ini and (:cit_fin - interval '1 minute')
		        	and esp_id = :espc_id
			
            ),
            serinf as (

            	select ser_inf from idm_tc.idm_servicio_tc
            		where id = :ser_id
            		order by 1 asc
        		limit 1
            )
            select id from idm_tc.idm_consultorio_tc
            	where id not in (select con_id from concit)
                	and id_suc = :id_suc
                	AND (
                  (SELECT ser_inf FROM serinf) IS NOT TRUE  -- Si NO es true (es false o null), no se aplica el filtro
                  OR 
                  (con_inf = TRUE)                          -- Si SÍ es true, se obliga a que con_inf sea true
                   )
            	order by 1 asc
            	limit 1
        
                """)

        params_con= {
            "cit_ini":cit_ini,
            "cit_fin":cit_fin,
            "espc_id":cita.get('especialista_id', '1'),
            "id_suc":id_suc,
            "ser_id":cita.get('servicio_id','1')

        }
        consult=db.session.execute(query_cons, params_con).mappings().first()
        #con_id = "INT0000101"
        con_id=str(consult["id"] if consult else "INT0000101")
        
        ########################## No olvides agreagar lo del cambio de clave de paciente ##################################

        # Cita Principal
        query_cita = text("""
            INSERT INTO idm_tr.idm_citas_tr 
            (id, esp_id, pac_id, con_id, cit_est, cit_ini, cit_fin, cit_age, ser_id)
            VALUES (:id_cita, :esp_id, :pac_id, :con_id, 'Confirmada', :cit_ini, :cit_fin, NOW(),:ser_id)
        """)

        params_cita = {
            "id_cita": id_cita,
            "esp_id": cita.get('especialista_id', '1'),
            "pac_id": id_pac,
            "con_id": con_id,
            "cit_ini": cit_ini,
            "cit_fin": cit_fin,
            "ser_id": cita.get('servicio_id','1')

        }
        db.session.execute(query_cita, params_cita)

        # Firma y Auditoría
        query_audit = text("""
            INSERT INTO idm_tr.idm_firma_consentimiento
            (id_cita, id_firma_cliente, fecha_registro_utc, imagen_base64, ip_origen)
            VALUES (:id_cita, :id_firma, :fec_reg, :imb64, :ip_orig)
        """)
        params_audit = {
            "id_cita": id_cita,
            "id_firma": firma.get('id_firma_cliente', '1'),
            "fec_reg": firma.get('fecha_registro_utc', datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')),
            "imb64": firma.get('imagen_base64', ''),
            "ip_orig": firma.get('ip_origen', request.remote_addr or '127.0.0.1')
        }
        db.session.execute(query_audit, params_audit)
        
        #Agregar datos de facturacion en caso de requerirse.

        req_fact = facturacion.get('requiere_factura',)

        if req_fact:
            datos_fiscales = facturacion.get('datos_fiscales') or {}

            query_facturacion = text("""
                 INSERT INTO idm_tr.idm_facturacion_tr
                (txt_rfc, txt_raz_soc, txt_cp, txt_uso_cfdi, txt_reg_fis, txt_ema_fac, num_id_cita)
                    VALUES(:txt_rfc, :txt_razon_social, :txt_codigo_postal, :txt_uso_cfdi, :txt_regimen_fiscal,:txt_ema_fact, :num_id_cita)""")


            paramsFacturacion = {

                "txt_rfc":datos_fiscales.get('rfc',''),
                "txt_razon_social":datos_fiscales.get('razon_social',''),
                "txt_codigo_postal":datos_fiscales.get('codigo_postal',''),
                "txt_uso_cfdi":datos_fiscales.get('uso_cfdi',''),
                "txt_regimen_fiscal":datos_fiscales.get('regimen_fiscal',''),
                "txt_ema_fact":datos_fiscales.get('email_facturacion',''),
                "num_id_cita":id_cita

            }
            db.session.execute(query_facturacion,paramsFacturacion)

        # Confirmar la transacción completa
        db.session.commit()

#        return jsonify({
#            "exito": True,
#            "mensaje": "Cita registrada correctamente en el sistema.",
#            "cita_id": id_cita
#        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al registrar la cita en BD: {str(e)}"}), 500


    try:
        print("El mensaje empieza a enviarse")
        query_mensaje = text("""select ist.suc_name, ict.con_num
        ,concat(iet.esp_name, ' ', iet.esp_ap_pat  ) as name_esp
        ,ist2.ser_name
        ,concat(ipt.pac_name , ' ', ipt.pac_ap_pat ) as name_pac
        ,ipt.pac_edad
        ,itt.num_tel
        ,coalesce(imt.men_name,'NULL') as men_name
        ,imt.men_edad
        ,cast(cit.cit_ini as time) as hora
        ,cast(cit.cit_ini as date) as dia
        ,case when ift.id is null then 'NO' else 'SI' end as ift_id
        ,coalesce(ift.txt_raz_soc,'NULL') as txt_raz_soc
        ,coalesce(ift.txt_cp,'NULL') as txt_cp
        ,coalesce(ift.txt_uso_cfdi,'NULL') as txt_uso_cfdi
        ,coalesce(ift.txt_reg_fis,'NULL') as txt_reg_fis
        ,coalesce(ift.txt_ema_fac,'NULL') as txt_ema_fac
        ,coalesce(ift.num_id_cita,0) as num_id_cita
        ,coalesce(ift.txt_rfc,'NULL') as txt_rfc
        from idm_tr.idm_citas_tr cit
                inner join idm_tc.idm_consultorio_tc ict on ict.id = cit.con_id
                inner join idm_tc.idm_sucursal_tc ist on ist.id = ict.id_suc
                inner join idm_tc.idm_especialista_tc iet on iet.id = cit.esp_id
                inner join idm_tc.idm_servicio_tc ist2 on ist2.id = cit.ser_id
                inner join idm_tc.idm_paciente_tc ipt on ipt.id = cit.pac_id
                left join idm_tc.idm_menor_tc imt on imt.id_tutor = ipt.id
                inner join idm_tc.idm_telefono_tc itt on itt.id_pac = ipt.id
                left join idm_tr.idm_facturacion_tr ift on ift.num_id_cita = cit.id
                where
		    cit.id = :id_cita; """)

        print("ejecutando consulta")
        dat_mens=db.session.execute(query_mensaje, {"id_cita":id_cita}).mappings().first()
        
        print("Consulta exitosa")
        suc_name = dat_mens["suc_name"]
        con_num = dat_mens["con_num"]
        name_esp = dat_mens["name_esp"]
        ser_name = dat_mens["ser_name"]
        name_pac = dat_mens["name_pac"]
        pac_edad = dat_mens["pac_edad"]
        num_tel = dat_mens["num_tel"]
        men_name = dat_mens["men_name"]
        men_edad = dat_mens["men_edad"]
        hora = dat_mens["hora"]
        dia = dat_mens["dia"]
        ift_id = dat_mens["ift_id"]
        txt_raz_soc = dat_mens["txt_raz_soc"]
        txt_cp = dat_mens["txt_cp"]
        txt_uso_cfdi = dat_mens["txt_uso_cfdi"]
        txt_reg_fis = dat_mens["txt_reg_fis"]
        txt_ema_fac = dat_mens["txt_ema_fac"]
        num_id_cita = dat_mens["num_id_cita"]
        txt_rfc = dat_mens["txt_rfc"]


        objeto_fecha = datetime.strptime(str(dia), "%Y-%m-%d")
        dia = objeto_fecha.strftime("%d/%b/%Y").lower()

        print("consulta completa")

        texto_notificacion=""

        link = ""

        if suc_name == "San Vicente":
            link = "https://maps.app.goo.gl/vxMy1TjSqLPszMha7"
        elif suc_name == "Vallarta Villas":
            link = "https://maps.app.goo.gl/se7uTKB743p8T12W6"
        else:
            pass

        if men_name != "NULL":
            texto_notificacion =f"""🚨 *Nueva Cita Registrada*

               Se ha confirmado una nueva cita en el sistema:

                🏢 *Sucursal:* {suc_name}
                🏪 *Consultorio:* {con_num}
                👩‍💼 *Especialista:*¨{name_esp}
                🏥 *Servicio:* {ser_name}
                🧘‍♀️ *Paciente:* {name_pac} edad: {pac_edad}
                🗓️ *Fecha:* {dia}
                🕑 *Hora:* {hora}
                📱 *Contacto:* {num_tel}
                🧒 *Menor:* {men_name} edad: {men_edad}

                Este es un aviso automático enviado por el sistema."""

        elif ift_id == "SI":
            texto_notificacion = f"""🚨 *Nueva Cita Registrada*

               Se ha confirmado una nueva cita en el sistema:

                🏢 *Sucursal:* {suc_name}
                🏪 *Consultorio:* {con_num}
                👩‍💼 *Especialista:*¨{name_esp}
                🏥 *Servicio:* {ser_name}
                🧘‍♀️ *Paciente:* {name_pac} edad: {pac_edad}
                🗓️ *Fecha:* {dia}
                🕑 *Hora:* {hora}
                📱 *Contacto:* {num_tel}

                Datos para factura:
                *RFC:* {txt_rfc}
                *Razon Social:* {txt_raz_soc}
                *Código Postal:* {txt_cp}
                *Uso de CFDI:* {txt_uso_cfdi}
                *Regimen Fiscal:*{txt_reg_fis}
                *Email:*{txt_ema_fac}
                *Cita:*{num_id_cita}
                

                Este es un aviso automático enviado por el sistema."""
            
        else:
            texto_notificacion = f"""🚨 *Nueva Cita Registrada*

               Se ha confirmado una nueva cita en el sistema:

                🏢 *Sucursal:* {suc_name}
                🏪 *Consultorio:* {con_num}
                👩‍💼 *Especialista:*¨{name_esp}
                🏥 *Servicio:* {ser_name}
                🧘‍♀️ *Paciente:* {name_pac} edad: {pac_edad}
                🗓️ *Fecha:* {dia}
                🕑 *Hora:* {hora}
                📱 *Contacto:* {num_tel}

                Este es un aviso automático enviado por el sistema."""


        texto_paciente =f"""Gracias {name_pac} por agendar con IDEALMENTE 🧠, para confirmar tu cita envía tu comprobante de pago a través de esta conversación por favor.
               
               Información de la cita:

                🏢 *Sucursal:* {suc_name}
                👩‍💼 *Especialista:*¨{name_esp}
                🏥 *Servicio:* {ser_name}
                🗓️ *Fecha:* {dia}
                🕑 *Hora:* {hora}.

                Te compartimos nuestra ubicación:
                {link}

                Este es un aviso automático enviado por el sistema."""


        # --- 3. Disparar el envío por WhatsApp de forma asíncrona ---
        print("Listo para enviar")
        print("numero:",num_tel)
        enviar_whatsapp_en_segundo_plan(NUMERO_DESTINO, texto_notificacion,num_tel,texto_paciente)

        # --- 4. Responder al cliente de inmediato ---
        print("mensaje entregado correctamente")
        return jsonify({
            "exito": True,
            "message": "Cita guardada y notificación en proceso.",
            "cita_id": id_cita
        }), 201
    
    except Exception as e:
        print("error: No se pudo enviar el mensaje.", {str(e)})
        logger.info("error: No se pudo enviar el mensaje. {str(e)}")
        return jsonify({"error": f"Mensaje no pudo ser enviado: {str(e)}"}), 500


@app.route('/api/citas/confirmacion', methods=['GET'])
def obtener_confirmacion_cita():
    cita_id = request.args.get('cita_id')
    
    if not cita_id:
        return jsonify({'error': 'Falta el parámetro cita_id'}), 400

    query = text("""
        SELECT 
            c.id AS id,                
            -- Paciente Principal
            CONCAT(p.pac_name, ' ', p.pac_ap_pat, ' ', COALESCE(p.pac_ap_mat, '')) AS nombrePaciente,
            p.pac_edad AS pac_edad,
            
            -- Detalle del servicio y especialista
            suc.suc_name AS sucursal,
            suc.suc_dir AS direccionSucursal,
            epd.epd_nombre AS especialidad,
            concat(med.esp_name, ' ',med.esp_ap_pat) AS especialista,
            srv.ser_name AS servicio,
            
            -- Tiempos formateados a string para evitar errores de serialización
            to_char(c.cit_ini, 'YYYY-MM-DD') AS fechaCita,
            to_char(c.cit_ini, 'hh24:mi') AS horaCita,
           to_char(c.cit_age, 'YYYY-MM-DD hh24:mi') AS fechaHoraGeneracion
        FROM idm_tr.idm_citas_tr c
        INNER JOIN idm_tc.idm_paciente_tc p ON c.pac_id = p.id
        LEFT JOIN idm_tc.idm_menor_tc m ON m.id_tutor = p.id
        INNER JOIN idm_tc.idm_consultorio_tc con ON c.con_id = con.id
        INNER JOIN idm_tc.idm_sucursal_tc suc ON con.id_suc = suc.id
        INNER JOIN idm_tc.idm_especialista_tc med ON c.esp_id = med.id
        INNER JOIN idm_tc.idm_servicio_tc srv ON c.ser_id = srv.id
        INNER JOIN idm_tc.idm_especialidad_tc epd ON epd.id = srv.id_epd
        WHERE c.id = :cita_id
    """)

    result = db.session.execute(query, {'cita_id': cita_id}).mappings().first()

    if not result:
        return jsonify({'error': 'Cita no encontrada'}), 404

    # Mapeo y serialización limpia de cualquier objeto temporal sobrante
    data = dict(result)
    for k, v in data.items():
        if isinstance(v, (date, time, datetime, timedelta)):
            data[k] = str(v)

    return jsonify(data)


app.config['MAIL_SERVER'] = 'smtp.gmail.com'           # Servidor SMTP (ejemplo con Gmail)
app.config['MAIL_PORT'] = 587                          # Puerto TLS habitual
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False
app.config['MAIL_USERNAME'] = 'idealmentecitas@gmail.com'    # Tu correo remitente
app.config['MAIL_PASSWORD'] = 'tocrnqwmmgefbjow'   # Contraseña de aplicación
app.config['MAIL_DEFAULT_SENDER'] = ('Idealmente', 'idealmentecitas@gmail.com')

mail = Mail(app)


@app.route('/api/citas/enviar-email-confirmacion', methods=['GET','POST'])
def enviar_email_confirmacion():

    data = {}
    cita_id = None

    if request.method == 'POST':
        data = request.get_json(force=True, silent=True) or {}
        cita_id = data.get('cita_id') or request.args.get('cita_id')
    else:
        cita_id = request.args.get('cita_id')

    if not cita_id and request.form:
        cita_id = request.form.get('cita_id')

    if not cita_id:
        return jsonify({
            'error': 'Falta el parámetro cita_id', 
            'debug_info': {
                'content_type': request.content_type,
                'has_json_data': bool(data),
                'method_recibido': request.method
            }
        }), 400
    print("todo bien hasta aquí",flush=True)
    # 1. Consultar la información de la cita y el correo del paciente/tutor
    query = text("""
        SELECT 
            c.id AS id,                
            -- Paciente Principal
            CONCAT(p.pac_name, ' ', p.pac_ap_pat, ' ', COALESCE(p.pac_ap_mat, '')) AS nombrePaciente,
            p.pac_edad AS pac_edad, dir_ema as emailPaciente,
            
            -- Detalle del servicio y especialista
            suc.suc_name AS sucursal,
            suc.suc_dir AS direccionSucursal,
            epd.epd_nombre AS especialidad,
            concat(med.esp_name, ' ',med.esp_ap_pat) AS especialista,
            srv.ser_name AS servicio,
            
            -- Tiempos formateados a string para evitar errores de serialización
            to_char(c.cit_ini, 'YYYY-MM-DD') AS fechaCita,
            to_char(c.cit_ini, 'hh24:mi') AS horaCita,
           to_char(c.cit_age, 'YYYY-MM-DD hh24:mi') AS fechaHoraGeneracion
        FROM idm_tr.idm_citas_tr c
        INNER JOIN idm_tc.idm_paciente_tc p ON c.pac_id = p.id
        LEFT JOIN idm_tc.idm_menor_tc m ON m.id_tutor = p.id
        INNER JOIN idm_tc.idm_consultorio_tc con ON c.con_id = con.id
        INNER JOIN idm_tc.idm_sucursal_tc suc ON con.id_suc = suc.id
        INNER JOIN idm_tc.idm_especialista_tc med ON c.esp_id = med.id 
        INNER JOIN idm_tc.idm_servicio_tc srv ON c.ser_id = srv.id
        INNER JOIN idm_tc.idm_especialidad_tc epd ON epd.id = srv.id_epd
        INNER JOIN idm_tc.idm_email_tc ema ON ema.id_pac = p.id
        WHERE c.id = :cita_id
    """)

    result = db.session.execute(query, {'cita_id': cita_id}).mappings().first()

    if not result:
        return jsonify({'error': 'No se encontró la cita especificada'}), 404

    cita = dict(result)
    print(cita,flush=True)
    correo_destino = cita.get('emailpaciente')

    if not correo_destino:
        return jsonify({'error': 'El paciente no tiene un correo electrónico registrado'}), 400

    # 2. Plantilla HTML responsiva para el correo electrónico
    cuerpo_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f6f8; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; background: #ffffff; border-radius: 8px; margin: auto; padding: 30px; border: 1px solid #e2e8f0; }}
            .header {{ text-align: center; border-bottom: 2px solid #2563eb; padding-bottom: 15px; margin-bottom: 20px; }}
            .header h2 {{ color: #1e293b; margin: 0; }}
            .detalle-row {{ display: flex; justify-content: space-between; margin-bottom: 12px; font-size: 14px; border-bottom: 1px #f1f5f9 dashed; padding-bottom: 8px; }}
            .label {{ color: #64748b; font-weight: bold; }}
            .value {{ color: #0f172a; font-weight: 600; text-align: right; }}
            .badge {{ background-color: #dcfce7; color: #16a34a; padding: 4px 12px; border-radius: 12px; font-weight: bold; }}
            .footer {{ text-align: center; margin-top: 25px; font-size: 12px; color: #94a3b8; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>Confirmación de Cita Médica</h2>
                <p style="color: #64748b; margin-top: 5px;">Folio: <strong style="color: #2563eb;">#{cita['id']}</strong></p>
            </div>

            <p>Estimado(a) <strong>{cita['nombrepaciente']}</strong>,</p>
            <p>Se ha registrado exitosamente tu cita. A continuación te mostramos el resumen de tu reservación:</p>

            <br>
            <div class="detalle-row">
                <span class="label">Especialidad:</span>
                <span class="value">{cita['especialidad']}</span>
            </div>
            <div class="detalle-row">
                <span class="label">Especialista:</span>
                <span class="value">{cita['especialista']}</span>
            </div>
            <div class="detalle-row">
                <span class="label">Servicio:</span>
                <span class="value">{cita['servicio']}</span>
            </div>
            <div class="detalle-row">
                <span class="label">Fecha:</span>
                <span class="value badge">{cita['fechacita']}</span>
            </div>
            <div class="detalle-row">
                <span class="label">Hora:</span>
                <span class="value badge">{cita['horacita']} hrs</span>
            </div>
            <div class="detalle-row">
                <span class="label">Sucursal:</span>
                <span class="value">{cita['sucursal']}</span>
            </div>
            <div class="detalle-row">
                <span class="label">Dirección:</span>
                <span class="value">{cita['direccionsucursal']}</span>
            </div>

            <br>
            <p style="font-size: 13px; color: #475569;">
                Por favor, preséntate 10 minutos antes de la hora programada. Si deseas cancelar o reagendar, ponte en contacto con nosotros con anticipación vía <a href="https://wa.me/523222887047" target="_blank" class="nav-link2">Whatsapp: 3222887047</a>.
            </p>

            <div class="footer">
                <p>Este es un mensaje automático, por favor no respondas a este correo.</p>
            </div>
        </div>
    </body>
    </html>
    """

    # 3. Construir y enviar el correo
    try:
        msg = Message(
            subject=f"Confirmación de Cita #{cita['id']} - Idealmente",
            recipients=[correo_destino],
            html=cuerpo_html
        )
        mail.send(msg)
        return jsonify({'mensaje': 'Correo enviado correctamente'}), 200

    except Exception as e:
        print(f"Error al enviar correo: {e}",flush=True)
        return jsonify({'error': 'No se pudo enviar el correo de confirmación'}), 500

MAPEO_MOTIVOS = {
    "Renta de espacio": "EXCEPRENTA",
    "Reunión de especialistas": "EXCEPREUNI",
    "Mantenimiento": "EXCEPMANTE",
    "Día Festivo": "EXCEPFESTI"
}

@app.route('/api/bloqueo/catalogos', methods=['GET'])
def obtener_catalogos_bloqueo():
    """Obtiene especialistas, sucursales y consultorios para llenar los desplegables."""
    try:
        q_esp = text("""
            SELECT id, CONCAT(esp_name, ' ', COALESCE(esp_ap_pat, ''), ' ', COALESCE(esp_ap_mat, '')) AS nombre 
            FROM idm_tc.idm_especialista_tc 
            WHERE esp_activo = true
            ORDER BY esp_name ASC
        """)
        especialistas = [dict(row) for row in db.session.execute(q_esp).mappings()]

        q_suc = text("""
            SELECT id, suc_name AS nombre 
            FROM idm_tc.idm_sucursal_tc
            ORDER BY suc_name ASC
        """)
        sucursales = [dict(row) for row in db.session.execute(q_suc).mappings()]

        q_con = text("""
            SELECT id, con_num, id_suc 
            FROM idm_tc.idm_consultorio_tc
            ORDER BY con_num ASC
        """)
        consultorios = [dict(row) for row in db.session.execute(q_con).mappings()]

        return jsonify({
            "especialistas": especialistas,
            "sucursales": sucursales,
            "consultorios": consultorios
        }), 200

    except Exception as e:
        return jsonify({"error": f"Error al cargar catálogos: {str(e)}"}), 500


@app.route('/api/bloqueo/guardar', methods=['POST'])
def guardar_bloqueo():
    """Procesa el registro del bloqueo según el caso seleccionado."""
    data = request.json or {}
    print("data exception:",data)
    
    tipo_bloqueo = data.get('tipo_bloqueo') # '1', '2', '3'
    fecha_ini = data.get('fecha_ini')       # 'YYYY-MM-DD HH:MM:SS'
    fecha_fin = data.get('fecha_fin')       # 'YYYY-MM-DD HH:MM:SS'
    
    if not tipo_bloqueo or not fecha_ini or not fecha_fin:
        return jsonify({"error": "Faltan parámetros obligatorios (tipo, fecha inicio, fecha fin)"}), 400
    
    try:
        query_max_cita = text("""SELECT COALESCE(MAX(id), 0) AS max_id FROM idm_tr.idm_citas_tr""")
        step1 = db.session.execute(query_max_cita).mappings().first()
        id_cita = (step1["max_id"] if step1 else 0) + 1

        query_max_exc = text("""SELECT COALESCE(MAX(id), 0) AS max_id FROM idm_tr.idm_excepciones_tr""")
        stepex = db.session.execute(query_max_exc).mappings().first()
        id_exc = (stepex["max_id"] if stepex else 0) + 1

        # Caso 1: Excepción de Especialista -> Inserta en idm_tr.idm_excepciones_tr
        if tipo_bloqueo == '1':
            esp_id = data.get('esp_id')
            if not esp_id:
                return jsonify({"error": "Debe seleccionar un especialista."}), 400

            q_insert = text("""
                INSERT INTO idm_tr.idm_excepciones_tr (id, esp_id, exc_ini, exc_fin)
                VALUES (:id_exc, :esp_id, :exc_ini, :exc_fin)
            """)
            db.session.execute(q_insert, {
                "id_exc": id_exc,
                "esp_id": esp_id,
                "exc_ini": fecha_ini,
                "exc_fin": fecha_fin
            })

        # Caso 2: Suspensión en Consultorio -> Inserta en idm_tr.idm_citas_tr
        elif tipo_bloqueo == '2':
            con_id = data.get('con_id')
            motivo = data.get('motivo')
            pac_id_code = MAPEO_MOTIVOS.get(motivo)

            if not con_id or not pac_id_code:
                return jsonify({"error": "Debe seleccionar consultorio y un motivo válido."}), 400

            esp_id_registro = 'EXCIM00001'
            tipo_arrendatario = (data.get('tipo_arrendatario') or 'especialista').strip()
            arrendatario_externo = (data.get('arrendatario_externo') or '').strip()

            print("motivo:",motivo)
            print("arrendatario:",tipo_arrendatario)

            if motivo == "Renta de espacio":
                if tipo_arrendatario == 'especialista':
                    esp_id_sel = (data.get('esp_id') or '').strip()
                    if not esp_id_sel:
                        return jsonify({"error": "Debe seleccionar el especialista que renta el espacio."}), 400
                    esp_id_registro = esp_id_sel
                elif tipo_arrendatario == 'externo':
                    if not arrendatario_externo:
                        return jsonify({"error": "Debe indicar el nombre de la persona externa que renta el espacio."}), 400
                    esp_id_registro = 'EXCIM00001'
                else:
                    return jsonify({"error": "Tipo de arrendatario inválido."}), 400

            q_insert = text("""
                INSERT INTO idm_tr.idm_citas_tr 
                (id, esp_id, pac_id, con_id, cit_est, cit_ini, cit_fin, cit_age, ser_id)
                VALUES (:id_cita, :esp_id, :pac_id, :con_id, CAST('Confirmada' as estatus), :cit_ini, :cit_fin, NOW(), NULL)
            """)
            db.session.execute(q_insert, {
                "id_cita": id_cita,
                "esp_id": esp_id_registro,
                "pac_id": pac_id_code,
                "con_id": con_id,
                "cit_ini": fecha_ini,
                "cit_fin": fecha_fin
            })

        # Caso 3: Suspensión General -> Inserta en idm_tr.idm_citas_tr para CADA consultorio
        elif tipo_bloqueo == '3':
            motivo = data.get('motivo')
            pac_id_code = MAPEO_MOTIVOS.get(motivo)

            if not pac_id_code:
                return jsonify({"error": "Debe seleccionar un motivo válido."}), 400

            q_consultorios = text("SELECT id FROM idm_tc.idm_consultorio_tc")
            consultorios = db.session.execute(q_consultorios).fetchall()

            if not consultorios:
                return jsonify({"error": "No se encontraron consultorios registrados."}), 404

            q_insert = text("""
                INSERT INTO idm_tr.idm_citas_tr 
                (id, esp_id, pac_id, con_id, cit_est, cit_ini, cit_fin, cit_age, ser_id)
                VALUES (:id_cita, 'EXCIM00001', :pac_id, :con_id, CAST('Confirmada' as estatus), :cit_ini, :cit_fin, NOW(), NULL)
            """)
            
            for con in consultorios:
                db.session.execute(q_insert, {
                    "id_cita": id_cita,
                    "pac_id": pac_id_code,
                    "con_id": con[0],
                    "cit_ini": fecha_ini,
                    "cit_fin": fecha_fin
                })
                id_cita += 1
        else:
            return jsonify({"error": "Tipo de bloqueo inválido."}), 400

        db.session.commit()
        return jsonify({
            "mensaje": "Bloqueo registrado con éxito.",
            "arrendatario_externo": arrendatario_externo if tipo_bloqueo == '2' and motivo == "Renta de espacio" else None
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al procesar el bloqueo: {str(e)}"}), 500

@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()

# ---------------------------------------------------------
# 3. ENDPOINTS DE ACCESO, CAMBIO DE CONTRASEÑA Y ALTA FUTURA
# ---------------------------------------------------------
@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"error": "Usuario y contraseña requeridos"}), 400

    try:
        row = db.session.execute(
            text("""
                SELECT id, username, display_name, password_hash, role, is_active,
                       must_change_password, failed_attempts, locked_until
                FROM admin_users
                WHERE LOWER(username) = :username
            """),
            {"username": username},
        ).mappings().first()

        if not row:
            return jsonify({"error": "Credenciales incorrectas"}), 401

        now = datetime.now(timezone.utc)

        if not row["is_active"]:
            return jsonify({"error": "Usuario desactivado. Contacta al administrador."}), 403

        if row["locked_until"] and row["locked_until"] > now:
            return jsonify({"error": "Cuenta bloqueada temporalmente por intentos fallidos. Intenta en 15 minutos."}), 429

        if not check_password_hash(row["password_hash"], password):
            new_attempts = (row["failed_attempts"] or 0) + 1
            lock_time = now + timedelta(minutes=15) if new_attempts >= 5 else None
            db.session.execute(
                text("""
                    UPDATE admin_users
                    SET failed_attempts = :attempts, locked_until = :lock_time
                    WHERE id = :id
                """),
                {"attempts": new_attempts, "lock_time": lock_time, "id": row["id"]},
            )
            db.session.commit()
            return jsonify({"error": "Credenciales incorrectas"}), 401

        db.session.execute(
            text("""
                UPDATE admin_users
                SET failed_attempts = 0, locked_until = NULL, last_login_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """),
            {"id": row["id"]},
        )
        log_admin_action(row["id"], "LOGIN", "admin_users", row["id"])
        db.session.commit()

        token_payload = {
            "user_id": row["id"],
            "username": row["username"],
            "role": row["role"],
            "exp": now + timedelta(hours=JWT_EXPIRATION_HOURS),
        }
        token = jwt.encode(token_payload, JWT_SECRET_KEY, algorithm="HS256")

        return jsonify({
            "token": token,
            "user": {
                "id": row["id"],
                "username": row["username"],
                "display_name": row["display_name"],
                "role": row["role"],
                "must_change_password": row["must_change_password"],
            },
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error interno al autenticar: {str(e)}"}), 500
    
@app.route("/api/admin/change-password", methods=["POST"])
@admin_required()
def admin_change_password():
    data = request.get_json(silent=True) or {}
    current_password = data.get("current_password") or ""
    new_password = data.get("new_password") or ""

    if len(new_password) < 8:
        return jsonify({"error": "La nueva contraseña debe tener al menos 8 caracteres."}), 400
    if current_password == new_password:
        return jsonify({"error": "La nueva contraseña debe ser diferente a la actual."}), 400

    user_id = g.admin_user["id"]

    try:
        row = db.session.execute(
            text("SELECT password_hash FROM admin_users WHERE id = :id"),
            {"id": user_id},
        ).mappings().first()

        if not row or not check_password_hash(row["password_hash"], current_password):
            return jsonify({"error": "La contraseña actual es incorrecta."}), 401

        new_hash = generate_password_hash(new_password)
        db.session.execute(
            text("""
                UPDATE admin_users
                SET password_hash = :password_hash,
                    must_change_password = FALSE,
                    password_changed_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """),
            {"password_hash": new_hash, "id": user_id},
        )
        log_admin_action(user_id, "CAMBIO_CONTRASENA", "admin_users", user_id)
        db.session.commit()

        return jsonify({"message": "Contraseña actualizada correctamente."}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al actualizar contraseña: {str(e)}"}), 500

@app.route("/api/admin/users", methods=["POST"])
@admin_required(allowed_roles=["superadmin", "admin"])
def create_admin_user():
    """Permite crear usuarios adicionales (ej. nuevos asistentes) a futuro."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    display_name = (data.get("display_name") or "").strip()
    role = (data.get("role") or "asistente").strip()
    temp_password = data.get("password") or os.getenv("ADMIN_INITIAL_PASSWORD", "Ideal2025*!")

    if not username or not display_name:
        return jsonify({"error": "Usuario y nombre completo son obligatorios."}), 400
    if role not in ("admin", "asistente"):
        return jsonify({"error": "Rol inválido. Solo se permite 'admin' o 'asistente'."}), 400

    try:
        new_id = db.session.execute(
            text("""
                INSERT INTO admin_users (username, display_name, password_hash, role, must_change_password, created_by)
                VALUES (:username, :display_name, :password_hash, :role, TRUE, :created_by)
                RETURNING id;
            """),
            {
                "username": username,
                "display_name": display_name,
                "password_hash": generate_password_hash(temp_password),
                "role": role,
                "created_by": g.admin_user["id"],
            },
        ).scalar()

        log_admin_action(
            g.admin_user["id"],
            "CREAR_USUARIO",
            "admin_users",
            new_id,
            {"username": username, "role": role},
        )
        db.session.commit()

        return jsonify({"id": new_id, "username": username, "role": role}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"No se pudo crear el usuario (posible duplicado): {str(e)}"}), 400



@app.route("/api/admin/dashboard/summary", methods=["GET"])
@admin_required()
def admin_dashboard_summary():
    try:
        # 1. Citas reales de hoy (excluyendo registros de bloqueo con pac_id que inicia en 'EXCEP')
        citas_hoy = db.session.execute(
            text("""
                SELECT COUNT(*)
                FROM idm_tr.idm_citas_tr
                WHERE cit_ini::date = CURRENT_DATE
                  AND (pac_id IS NULL OR pac_id NOT LIKE 'EXCEP%')
            """)
        ).scalar() or 0

        # 2. Bloqueos de hoy (rentas, reuniones o festividades registrados en citas con pac_id LIKE 'EXCEP%')
        bloqueos_hoy = db.session.execute(
            text("""
                SELECT COUNT(*)
                FROM idm_tr.idm_citas_tr
                WHERE cit_ini::date = CURRENT_DATE
                  AND pac_id LIKE 'EXCEP%'
            """)
        ).scalar() or 0

        # 3. Pagos pendientes asociados a citas reales
        pagos_pendientes = db.session.execute(
            text("""
                SELECT COUNT(*)
                FROM idm_tr.idm_pagos_tr p
                INNER JOIN idm_tr.idm_citas_tr c ON p.id_cit = c.id
                WHERE LOWER(CAST(p.pag_estatus AS TEXT)) LIKE '%pend%'
                  AND (c.pac_id IS NULL OR c.pac_id NOT LIKE 'EXCEP%')
            """)
        ).scalar() or 0

        # 4. Especialistas activos
        especialistas_activos = db.session.execute(
            text("""
                SELECT COUNT(*)
                FROM idm_tc.idm_especialista_tc
                WHERE esp_activo IS TRUE
            """)
        ).scalar() or 0

        # 5. Excepciones vigentes hoy para especialistas (idm_tr.idm_excepciones_tr)
        excepciones_especialistas_hoy = db.session.execute(
            text("""
                SELECT COUNT(*)
                FROM idm_tr.idm_excepciones_tr
                WHERE CURRENT_DATE BETWEEN exc_ini::date AND exc_fin::date
            """)
        ).scalar() or 0

        return jsonify({
            "citas_hoy": int(citas_hoy),
            "pagos_pendientes": int(pagos_pendientes),
            "consultorios_ocupados": int(bloqueos_hoy),
            "especialistas_activos": int(especialistas_activos),
            "excepciones_especialistas_hoy": int(excepciones_especialistas_hoy),
        }), 200

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"No se pudo obtener el resumen del dashboard: {str(e)}"}), 500


# ---------------------------------------------------------
# BACKOFFICE: CITAS, PAGOS, REAGENDAMIENTO, BLOQUEOS Y EXCEPCIONES
# ---------------------------------------------------------

@app.route("/api/admin/backoffice/citas", methods=["GET"])
@admin_required()
def backoffice_list_citas():
    fecha = (request.args.get("fecha") or "").strip()
    estatus_cita = (request.args.get("cit_est") or "").strip()

    sql = """
        SELECT
            c.id,
            c.esp_id,
            c.pac_id,
            c.con_id,
            cns.con_num,
            suc.suc_name,
            c.cit_est,
            c.cit_ini,
            c.cit_fin,
            c.cit_age,
            c.ser_id,
            e.esp_name,
            e.esp_ap_pat,
            e.esp_ap_mat,
            p.id AS pago_id,
            p.id_tar,
            p.pag_estatus,
            p.pag_ref,
            p.dt_confirmado
        FROM idm_tr.idm_citas_tr c
        LEFT JOIN idm_tc.idm_especialista_tc e ON c.esp_id = e.id
        LEFT JOIN idm_tr.idm_pagos_tr p ON p.id_cit = c.id
        INNER JOIN idm_tc.idm_consultorio_tc cns ON cns.id = c.con_id
        INNER JOIN idm_tc.idm_sucursal_tc suc ON suc.id = cns.id_suc
        WHERE (c.pac_id IS NULL OR c.pac_id NOT LIKE 'EXCEP%')
        AND c.cit_ini >= CURRENT_DATE
    """
    params = {}

    if fecha:
        sql += " AND c.cit_ini::date = CAST(:fecha AS date)"
        params["fecha"] = fecha

    if estatus_cita:
        sql += " AND CAST(c.cit_est AS TEXT) = :cit_est"
        params["cit_est"] = estatus_cita

    sql += " ORDER BY c.cit_ini DESC LIMIT 200"

    try:
        rows = db.session.execute(text(sql), params).mappings().all()
        items = []
        for r in rows:
            nombre_esp = " ".join(
                part for part in [r["esp_name"], r["esp_ap_pat"], r["esp_ap_mat"]] if part
            ).strip() or r["esp_id"]

            items.append({
                "id": r["id"],
                "esp_id": r["esp_id"],
                "especialista_nombre": nombre_esp,
                "pac_id": r["pac_id"],
                "con_id": r["con_id"],
                "con_num": r["con_num"],
                "suc_name":r["suc_name"],
                "cit_est": str(r["cit_est"]) if r["cit_est"] is not None else "",
                "cit_ini": r["cit_ini"].strftime("%d/%m/%Y %H:%M") if r["cit_ini"] else None,
                "cit_fin": r["cit_fin"].isoformat() if r["cit_fin"] else None,
                "ser_id": r["ser_id"],
                "pago_id": r["pago_id"],
                "pag_estatus": str(r["pag_estatus"]) if r["pag_estatus"] is not None else "Sin registro",
                "pag_ref": r["pag_ref"],
            })
        return jsonify({"citas": items}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al consultar citas: {str(e)}"}), 500


@app.route("/api/admin/backoffice/citas/<int:cita_id>/estado", methods=["PATCH"])
@admin_required()
def backoffice_update_cita_estado(cita_id):
    data = request.get_json(silent=True) or {}
    nuevo_cit_est = (data.get("cit_est") or "").strip()
    nuevo_pag_est = (data.get("pag_estatus") or "").strip()

    if not nuevo_cit_est and not nuevo_pag_est:
        return jsonify({"error": "Debes indicar cit_est o pag_estatus para actualizar."}), 400

    try:
        cita = db.session.execute(
            text("SELECT id, cit_est FROM idm_tr.idm_citas_tr WHERE id = :id"),
            {"id": cita_id},
        ).mappings().first()

        if not cita:
            return jsonify({"error": "Cita no encontrada."}), 404

        if nuevo_cit_est:
            db.session.execute(
                text("""
                    UPDATE idm_tr.idm_citas_tr
                    SET cit_est = :cit_est
                    WHERE id = :id
                """),
                {"cit_est": nuevo_cit_est, "id": cita_id},
            )

        if nuevo_pag_est:
            pago_existente = db.session.execute(
                text("SELECT id FROM idm_tr.idm_pagos_tr WHERE id_cit = :id_cit"),
                {"id_cit": cita_id},
            ).mappings().first()

            if pago_existente:
                db.session.execute(
                    text("""
                        UPDATE idm_tr.idm_pagos_tr
                        SET pag_estatus = CAST(:pag_estatus AS text),
                            dt_actualizado = CURRENT_TIMESTAMP,
                            dt_confirmado = CASE
                                WHEN LOWER(:pag_estatus) LIKE '%confirm%' THEN COALESCE(dt_confirmado, CURRENT_TIMESTAMP)
                                ELSE dt_confirmado
                            END
                        WHERE id_cit = :id_cit
                    """),
                    {"pag_estatus": nuevo_pag_est, "id_cit": cita_id},
                )

        log_admin_action(
            g.admin_user["id"],
            "ACTUALIZAR_CITA_PAGO",
            "cita",
            cita_id,
            {"cit_est": nuevo_cit_est or None, "pag_estatus": nuevo_pag_est or None},
        )
        db.session.commit()
        return jsonify({"message": "Estado actualizado correctamente."}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"No se pudo actualizar el estado: {str(e)}"}), 400


@app.route("/api/admin/backoffice/citas/<int:cita_id>/reagendar", methods=["PATCH"])
@admin_required()
def backoffice_reagendar_cita(cita_id):
    data = request.get_json(silent=True) or {}
    cit_ini = (data.get("cit_ini") or "").strip()
    cit_fin = (data.get("cit_fin") or "").strip()
    con_id = (data.get("con_id") or "").strip()

    if not cit_ini or not cit_fin:
        return jsonify({"error": "Debes proporcionar cit_ini y cit_fin."}), 400

    try:
        actual = db.session.execute(
            text("SELECT id, cit_ini, cit_fin, con_id FROM idm_tr.idm_citas_tr WHERE id = :id"),
            {"id": cita_id},
        ).mappings().first()

        if not actual:
            return jsonify({"error": "Cita no encontrada."}), 404

        nuevo_con_id = con_id if con_id else actual["con_id"]

        db.session.execute(
            text("""
                UPDATE idm_tr.idm_citas_tr
                SET cit_ini = CAST(:cit_ini AS timestamp),
                    cit_fin = CAST(:cit_fin AS timestamp),
                    con_id = :con_id
                WHERE id = :id
            """),
            {
                "cit_ini": cit_ini,
                "cit_fin": cit_fin,
                "con_id": nuevo_con_id,
                "id": cita_id,
            },
        )

        log_admin_action(
            g.admin_user["id"],
            "REAGENDAR_CITA",
            "cita",
            cita_id,
            {
                "anterior": {"cit_ini": str(actual["cit_ini"]), "cit_fin": str(actual["cit_fin"]), "con_id": actual["con_id"]},
                "nuevo": {"cit_ini": cit_ini, "cit_fin": cit_fin, "con_id": nuevo_con_id},
            },
        )
        db.session.commit()
        return jsonify({"message": "Cita reagendada correctamente."}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"No se pudo reagendar la cita: {str(e)}"}), 400


@app.route("/api/admin/backoffice/citas/catalogos", methods=["GET"])
@admin_required()
def backoffice_citas_catalogos():
    try:
        sucursales = db.session.execute(
            text("""
                SELECT suc.id, suc.suc_name, suc.id_cit, est.id2, est.e_name AS estado_nombre
                FROM idm_tc.idm_sucursal_tc suc
                LEFT JOIN idm_tc.idm_ciudad_tc ciu ON suc.id_cit = ciu.id
                LEFT JOIN idm_tc.idm_estado_tc est ON ciu.id_est = est.idiso
                ORDER BY suc.suc_name
            """)
        ).mappings().all()

        especialidades = db.session.execute(
            text("""
                SELECT id, epd_nombre
                FROM idm_tc.idm_especialidad_tc
                WHERE epd_activo IS TRUE
                ORDER BY epd_nombre
            """)
        ).mappings().all()

        especialistas = db.session.execute(
            text("""
                SELECT id, esp_name, esp_ap_pat, esp_ap_mat, esp_disp_onl, esp_disp_offl, esp_mult
                FROM idm_tc.idm_especialista_tc
                WHERE esp_activo IS TRUE
                ORDER BY esp_name, esp_ap_pat
            """)
        ).mappings().all()

        servicios = db.session.execute(
            text("""
                SELECT id, ser_name, ser_dur, ser_onl, id_epd, ser_inf
                FROM idm_tc.idm_servicio_tc
                ORDER BY ser_name
            """)
        ).mappings().all()

        lenguas = db.session.execute(
            text("SELECT id, len_nombre FROM idm_tc.idm_lengua_tc ORDER BY len_nombre")
        ).mappings().all()

        consultorios = db.session.execute(
            text("""
                SELECT id, con_num, id_suc, con_type, con_inf, con_psic, con_psiq, con_nutr, con_otro, con_onl
                FROM idm_tc.idm_consultorio_tc
                ORDER BY id_suc, con_num
            """)
        ).mappings().all()

        tarifas = db.session.execute(
            text("""
                SELECT id, tar_name, tar_mon, tar_type, id_ser, id_suc
                FROM idm_tc.idm_tarifa_tc
                ORDER BY tar_name
            """)
        ).mappings().all()

        return jsonify({
            "sucursales": [dict(r) for r in sucursales],
            "especialidades": [dict(r) for r in especialidades],
            "especialistas": [
                {
                    **dict(r),
                    "nombre_completo": " ".join(
                        p for p in [r["esp_name"], r["esp_ap_pat"], r["esp_ap_mat"]] if p
                    ).strip(),
                }
                for r in especialistas
            ],
            "servicios": [
                {
                    **dict(r),
                    "ser_dur": str(r["ser_dur"]) if r["ser_dur"] is not None else None,
                    "ser_inf": bool(r["ser_inf"]),
                }
                for r in servicios
            ],
            "lenguas": [dict(r) for r in lenguas],
            "consultorios": [dict(r) for r in consultorios],
            "tarifas": [
                {
                    **dict(r),
                    "tar_mon": float(r["tar_mon"]) if r["tar_mon"] is not None else 0.0,
                }
                for r in tarifas
            ],
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al cargar catálogos de citas: {str(e)}"}), 500


@app.route("/api/admin/backoffice/citas/verificar-horario", methods=["POST"])
@admin_required()
def backoffice_verificar_horario():
    data = request.get_json(silent=True) or {}
    esp_id = (data.get("esp_id") or "").strip()
    suc_id = (data.get("id_suc") or "").strip()
    con_id = (data.get("con_id") or "").strip()
    cit_ini_str = (data.get("cit_ini") or "").strip()
    cit_fin_str = (data.get("cit_fin") or "").strip()

    if not esp_id or not cit_ini_str or not cit_fin_str:
        return jsonify({"error": "esp_id, cit_ini y cit_fin son requeridos."}), 400

    try:
        dt_ini = datetime.fromisoformat(cit_ini_str.replace("T", " "))
        dt_fin = datetime.fromisoformat(cit_fin_str.replace("T", " "))
        if dt_fin <= dt_ini:
            return jsonify({"error": "La hora de fin debe ser posterior a la de inicio."}), 400

        # En Python weekday(): lunes=0 ... domingo=6
        sem_day = dt_ini.weekday()
        t_ini = dt_ini.time()
        t_fin = dt_fin.time()

        horarios = db.session.execute(
            text("""
                SELECT hora_ini, hora_fin, id_suc
                FROM idm_tr.idm_horarios_tc
                WHERE esp_id = :esp_id
                  AND sem_day = :sem_day
                  AND (:id_suc = '' OR id_suc = :id_suc)
            """),
            {"esp_id": esp_id, "sem_day": sem_day, "id_suc": suc_id},
        ).mappings().all()

        dentro_horario = any(
            h["hora_ini"] is not None
            and h["hora_fin"] is not None
            and h["hora_ini"] <= t_ini
            and t_fin <= h["hora_fin"]
            for h in horarios
        )

        # Revisar posible overbooking en especialista o consultorio
        empalmes = db.session.execute(
            text("""
                SELECT COUNT(*)
                FROM idm_tr.idm_citas_tr
                WHERE cit_ini < CAST(:cit_fin AS timestamp)
                  AND cit_fin > CAST(:cit_ini AS timestamp)
                  AND (
                        esp_id = :esp_id
                        OR (:con_id <> '' AND con_id = :con_id)
                      )
            """),
            {
                "cit_ini": dt_ini.strftime("%Y-%m-%d %H:%M:%S"),
                "cit_fin": dt_fin.strftime("%Y-%m-%d %H:%M:%S"),
                "esp_id": esp_id,
                "con_id": con_id,
            },
        ).scalar() or 0

        return jsonify({
            "fuera_de_horario": not dentro_horario,
            "hay_empalme": int(empalmes) > 0,
            "empalmes_count": int(empalmes),
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al verificar horario: {str(e)}"}), 400


@app.route("/api/admin/backoffice/citas", methods=["POST"])
@admin_required()
def backoffice_create_cita():
    data = request.get_json(silent=True) or {}

    suc_id = (data.get("suc_id") or "").strip()
    esp_id = (data.get("esp_id") or "").strip()
    ser_id = (data.get("ser_id") or "").strip()
    len_id = (data.get("len_id") or "").strip()
    con_id = (data.get("con_id") or "").strip()
    tar_id = (data.get("tar_id") or "").strip()

    pac_name = (data.get("pac_name") or "").strip()
    pac_ap_pat = (data.get("pac_ap_pat") or "").strip()
    pac_ap_mat = (data.get("pac_ap_mat") or "").strip()
    pac_edad = data.get("pac_edad")

    whatsapp = (data.get("whatsapp") or "").strip()
    cla_pai = int(data.get("cla_pai") or 52)
    email = (data.get("email") or "").strip()

    cit_ini = (data.get("cit_ini") or "").strip()
    cit_fin = (data.get("cit_fin") or "").strip()
    cit_est = (data.get("cit_est") or "Confirmada").strip()
    pag_estatus = (data.get("pag_estatus") or "Pendiente").strip()
    confirmar_fuera_horario = bool(data.get("confirmar_fuera_horario", False))

    men_name = (data.get("men_name") or "").strip()
    men_edad = data.get("men_edad")

    if not all([suc_id, esp_id, ser_id, con_id, tar_id, pac_name, pac_ap_pat, whatsapp, email, cit_ini, cit_fin]):
        return jsonify({"error": "Faltan campos obligatorios para crear la cita."}), 400

    try:
        # Verificar servicio y si es infantil
        servicio = db.session.execute(
            text("SELECT id, ser_name, ser_inf FROM idm_tc.idm_servicio_tc WHERE id = :ser_id"),
            {"ser_id": ser_id},
        ).mappings().first()
        if not servicio:
            return jsonify({"error": "El servicio seleccionado no existe."}), 400

        es_infantil = bool(servicio["ser_inf"])
        if es_infantil and (not men_name or men_edad in (None, "")):
            return jsonify({"error": "Para servicios infantiles/adolescentes se requiere nombre y edad del menor."}), 400

        # Validar tarifa correspondiente al servicio y sucursal
        tarifa = db.session.execute(
            text("""
                SELECT id, tar_mon
                FROM idm_tc.idm_tarifa_tc
                WHERE id = :tar_id AND id_ser = :ser_id AND id_suc = :suc_id
            """),
            {"tar_id": tar_id, "ser_id": ser_id, "suc_id": suc_id},
        ).mappings().first()
        if not tarifa:
            return jsonify({"error": "La tarifa seleccionada no corresponde al servicio y sucursal elegidos."}), 400

        # Validación de horario disponible
        dt_ini = datetime.fromisoformat(cit_ini.replace("T", " "))
        dt_fin = datetime.fromisoformat(cit_fin.replace("T", " "))
        horarios = db.session.execute(
            text("""
                SELECT hora_ini, hora_fin
                FROM idm_tr.idm_horarios_tc
                WHERE esp_id = :esp_id AND sem_day = :sem_day AND id_suc = :suc_id
            """),
            {"esp_id": esp_id, "sem_day": dt_ini.weekday(), "suc_id": suc_id},
        ).mappings().all()

        fuera_de_horario = not any(
            h["hora_ini"] is not None
            and h["hora_fin"] is not None
            and h["hora_ini"] <= dt_ini.time()
            and dt_fin.time() <= h["hora_fin"]
            for h in horarios
        )
        if fuera_de_horario and not confirmar_fuera_horario:
            return jsonify({
                "error": "El horario está fuera de la disponibilidad del especialista. Confirma para continuar.",
                "requiere_confirmacion": True,
            }), 409

        # 1. Generar IDs consecutivos con id2 del estado de la sucursal
        id2 = _obtener_id2_estado_sucursal(suc_id)
        pac_id = _generar_consecutivo_prefijo("idm_tc.idm_paciente_tc", f"I{id2}", 10)
        tel_id = _generar_consecutivo_prefijo("idm_tc.idm_telefono_tc", id2, 10)
        ema_id = _generar_consecutivo_prefijo("idm_tc.idm_email_tc", id2, 10)

        # 2. Insertar paciente
        db.session.execute(
            text("""
                INSERT INTO idm_tc.idm_paciente_tc (id, pac_name, pac_ap_pat, pac_ap_mat, pac_edad)
                VALUES (:id, :pac_name, :pac_ap_pat, :pac_ap_mat, :pac_edad)
            """),
            {
                "id": pac_id,
                "pac_name": pac_name,
                "pac_ap_pat": pac_ap_pat,
                "pac_ap_mat": pac_ap_mat or None,
                "pac_edad": int(pac_edad) if pac_edad not in (None, "") else None,
            },
        )

        # 3. Insertar menor si aplica
        men_id = None
        if es_infantil:
            men_id = _generar_consecutivo_prefijo("idm_tc.idm_menor_tc", f"M{id2}", 10)
            db.session.execute(
                text("""
                    INSERT INTO idm_tc.idm_menor_tc (id, men_name, men_edad, id_tutor)
                    VALUES (:id, :men_name, :men_edad, :id_tutor)
                """),
                {
                    "id": men_id,
                    "men_name": men_name,
                    "men_edad": int(men_edad),
                    "id_tutor": pac_id,
                },
            )

        # 4. Insertar teléfono y correo vinculados a id_pac (id_esp vacío/NULL)
        db.session.execute(
            text("""
                INSERT INTO idm_tc.idm_telefono_tc (id, cla_pai, num_tel, id_pac, id_esp)
                VALUES (:id, :cla_pai, :num_tel, :id_pac, NULL)
            """),
            {
                "id": tel_id,
                "cla_pai": cla_pai,
                "num_tel": whatsapp[:10],
                "id_pac": pac_id,
            },
        )

        db.session.execute(
            text("""
                INSERT INTO idm_tc.idm_email_tc (id, dir_ema, id_pac, id_esp)
                VALUES (:id, :dir_ema, :id_pac, NULL)
            """),
            {
                "id": ema_id,
                "dir_ema": email,
                "id_pac": pac_id,
            },
        )

        query_max_cita = text("""SELECT COALESCE(MAX(id), 0) AS max_id FROM idm_tr.idm_citas_tr""")
        step1 = db.session.execute(query_max_cita).mappings().first()
        id_cita = (step1["max_id"] if step1 else 0) + 1
        
        # 5. Insertar cita
        cita_id = db.session.execute(
            text("""
                INSERT INTO idm_tr.idm_citas_tr
                    (id, esp_id, pac_id, con_id, cit_est, cit_ini, cit_fin, cit_age, ser_id)
                VALUES
                    (:id_cita,:esp_id, :pac_id, :con_id, CAST(:cit_est AS estatus),
                     CAST(:cit_ini AS timestamp), CAST(:cit_fin AS timestamp),
                     CURRENT_TIMESTAMP, :ser_id)
                RETURNING id
            """),
            {
                "esp_id": esp_id,
                "pac_id": pac_id,
                "con_id": con_id,
                "cit_est": cit_est,
                "cit_ini": dt_ini.strftime("%Y-%m-%d %H:%M:%S"),
                "cit_fin": dt_fin.strftime("%Y-%m-%d %H:%M:%S"),
                "ser_id": ser_id,
                "id_cita": id_cita
            },
        ).scalar()

        # 6. Insertar pago asociado a la cita y a la tarifa seleccionada

        query_max_pago = text("""SELECT COALESCE(MAX(id), 0) AS max_id FROM idm_tr.idm_pagos_tr""")
        step1 = db.session.execute(query_max_pago).mappings().first()
        id_pago = (step1["max_id"] if step1 else 0) + 1
        
        referencia = f"CITA-{pac_name.replace(' ', '-')}-{pac_ap_pat.replace(' ', '-')}-{cita_id}"[:100]
        pago_id = db.session.execute(
            text("""
                INSERT INTO idm_tr.idm_pagos_tr
                    (id,id_cit, id_tar, dt_creado, dt_confirmado, dt_actualizado, pag_estatus, pag_ref)
                VALUES
                    (:id_pago,:id_cit, :id_tar, CURRENT_TIMESTAMP,
                     CASE WHEN LOWER(:pag_estatus) LIKE '%confirm%' THEN CURRENT_TIMESTAMP ELSE NULL END,
                     CURRENT_TIMESTAMP, CAST(:pag_estatus AS estatus_p), :pag_ref)
                RETURNING id
            """),
            {
                "id_cit": cita_id,
                "id_tar": tar_id,
                "pag_estatus": pag_estatus,
                "pag_ref": referencia,
                "id_pago": id_pago
            },
        ).scalar()

        log_admin_action(
            g.admin_user["id"],
            "CREAR_CITA_BACKOFFICE",
            "cita",
            cita_id,
            {
                "pac_id": pac_id,
                "men_id": men_id,
                "pago_id": pago_id,
                "tar_id": tar_id,
                "len_id": len_id,
                "fuera_de_horario": fuera_de_horario,
            },
        )
        db.session.commit()

        return jsonify({
            "message": "Cita y pago registrados correctamente.",
            "cita_id": cita_id,
            "pago_id": pago_id,
            "pac_id": pac_id,
            "men_id": men_id,
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"No se pudo registrar la cita: {str(e)}"}), 400

@app.route("/api/admin/backoffice/bloqueos", methods=["GET"])
@admin_required()
def backoffice_bloqueos():
    try:
        rows = db.session.execute(
            text("""
                SELECT
                    c.id,
                    c.esp_id,
                    c.pac_id,
                    c.con_id,
                    suc.suc_name,
                    cs.con_num,
                    c.cit_est,
                    c.cit_ini,
                    c.cit_fin,
                    c.ser_id
                 FROM idm_tr.idm_citas_tr c
                 INNER JOIN idm_tc.idm_consultorio_tc cs ON cs.id = c.con_id
                 INNER JOIN idm_tc.idm_sucursal_tc suc ON suc.id = cs.id_suc
                WHERE c.pac_id LIKE 'EXCEP%'
                ORDER BY c.cit_ini DESC
                LIMIT 200
            """)
        ).mappings().all()

        bloqueos = [
            {
                "id": r["id"],
                "pac_id": r["pac_id"],
                "esp_id": r["esp_id"],
                "con_id": r["con_id"],
                "suc_name": r["suc_name"],
                "con_num": r["con_num"],
                "cit_est": str(r["cit_est"]) if r["cit_est"] is not None else "",
                "cit_ini": r["cit_ini"].isoformat() if r["cit_ini"] else None,
                "cit_fin": r["cit_fin"].isoformat() if r["cit_fin"] else None,
                "ser_id": r["ser_id"],
            }
            for r in rows
        ]
        return jsonify({"bloqueos": bloqueos}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al obtener bloqueos: {str(e)}"}), 500

import re
from datetime import datetime
from flask import jsonify, request
from sqlalchemy import text


def _siguiente_consecutivo_prefijo(tabla_sql: str, prefijo: str, ancho_num: int) -> str:
    """
    Busca el mayor consecutivo existente para un prefijo dado y devuelve
    prefijo + str(max_num + 1).zfill(ancho_num).
    """
    patron = f"{prefijo}%"
    rows = db.session.execute(
        text(f"""
            SELECT id
            FROM {tabla_sql}
            WHERE CAST(id AS TEXT) LIKE :patron
        """),
        {"patron": patron},
    ).scalars().all()

    max_num = 0
    regex = re.compile(rf"^{re.escape(prefijo)}(\d+)$")
    for raw_id in rows:
        if raw_id is None:
            continue
        m = regex.match(str(raw_id).strip())
        if m:
            val = int(m.group(1))
            if val > max_num:
                max_num = val

    return f"{prefijo}{str(max_num + 1).zfill(ancho_num)}"


def _siguiente_id_entero(tabla_sql: str) -> int:
    """Obtiene el siguiente entero consecutivo para tablas con ID numérico."""
    rows = db.session.execute(
        text(f"SELECT id FROM {tabla_sql}")
    ).scalars().all()

    max_num = 0
    for raw_id in rows:
        if raw_id is None:
            continue
        s = str(raw_id).strip()
        if s.isdigit():
            val = int(s)
            if val > max_num:
                max_num = val
        else:
            m = re.search(r"(\d+)$", s)
            if m:
                val = int(m.group(1))
                if val > max_num:
                    max_num = val

    return max_num + 1


def _obtener_codigo_estado(estado_input: str) -> str:
    """
    Resuelve el código de 2 letras del estado consultando idm_tc.idm_estado_tc.
    Acepta el código directo (ej. 'NT', 'JC') o el id/nombre del estado en la tabla.
    """
    limpio = (estado_input or "").strip().upper()
    if not limpio:
        return ""

    # Intenta buscar en idm_tc.idm_estado_tc
    try:
        cols_rows = db.session.execute(
            text("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'idm_tc' AND table_name = 'idm_estado_tc'
            """)
        ).scalars().all()
        cols_set = {c.lower() for c in cols_rows}

        candidatos_codigo = [c for c in ["edo_abre", "edo_cod", "codigo", "clave", "abreviatura", "sigla", "id"] if c in cols_set]
        candidatos_nombre = [c for c in ["edo_nombre", "nombre", "estado", "descripcion"] if c in cols_set]

        if candidatos_codigo or candidatos_nombre:
            condiciones = []
            for c in candidatos_codigo + candidatos_nombre:
                condiciones.append(f"UPPER(CAST({c} AS TEXT)) = :val")
            where_sql = " OR ".join(condiciones)
            select_col = candidatos_codigo[0] if candidatos_codigo else candidatos_nombre[0]

            row_val = db.session.execute(
                text(f"""
                    SELECT UPPER(TRIM(CAST({select_col} AS TEXT)))
                    FROM idm_tc.idm_estado_tc
                    WHERE {where_sql}
                    LIMIT 1
                """),
                {"val": limpio},
            ).scalar()

            if row_val:
                letras = re.sub(r"[^A-Z]", "", row_val)
                if len(letras) >= 2:
                    return letras[:2]
    except Exception:
        pass

    letras_directas = re.sub(r"[^A-Z]", "", limpio)
    return letras_directas[:2] if len(letras_directas) == 2 else ""


@app.route("/api/admin/backoffice/especialistas/catalogos", methods=["GET"])
@admin_required()
def backoffice_especialistas_catalogos():
    try:
        estados_rows = [
            dict(r)
            for r in db.session.execute(
                text("""
                    SELECT
                        idiso,
                        e_name,
                        id2 AS codigo
                    FROM idm_tc.idm_estado_tc
                    ORDER BY e_name ASC
                """)
            ).mappings().all()
        ]

        sucursales_rows = [
            dict(r)
            for r in db.session.execute(
                text("""
                    SELECT
                        id,
                        suc_name
                    FROM idm_tc.idm_sucursal_tc
                    ORDER BY suc_name ASC
                """)
            ).mappings().all()
        ]

        lenguas_rows = [
            dict(r)
            for r in db.session.execute(
                text("""
                    SELECT
                        id,
                        len_nombre
                    FROM idm_tc.idm_lengua_tc
                    ORDER BY len_nombre ASC
                """)
            ).mappings().all()
        ]

        especialidades_rows = [
            dict(r)
            for r in db.session.execute(
                text("""
                    SELECT
                        id,
                        epd_nombre,
                        COALESCE(epd_activo, true) AS epd_activo
                    FROM idm_tc.idm_especialidad_tc
                    WHERE COALESCE(epd_activo, true) = true
                    ORDER BY epd_nombre ASC
                """)
            ).mappings().all()
        ]

        servicios_rows = [
            dict(r)
            for r in db.session.execute(
                text("""
                    SELECT
                        s.id,
                        s.ser_name,
                        TO_CHAR(s.ser_dur, 'HH24:MI') AS ser_dur,
                        s.ser_ses,
                        COALESCE(s.ser_onl, false) AS ser_onl,
                        s.id_epd,
                        COALESCE(e.epd_nombre, '') AS epd_nombre,
                        COALESCE(s.ser_inf, false) AS ser_inf
                    FROM idm_tc.idm_servicio_tc s
                    LEFT JOIN idm_tc.idm_especialidad_tc e
                        ON CAST(e.id AS TEXT) = CAST(s.id_epd AS TEXT)
                    ORDER BY e.epd_nombre ASC NULLS LAST, s.ser_name ASC
                """)
            ).mappings().all()
        ]

        return jsonify({
            "estados": estados_rows,
            "sucursales": sucursales_rows,
            "lenguas": lenguas_rows,
            "especialidades": especialidades_rows,
            "servicios": servicios_rows,
        }), 200
    except Exception as e:
        return jsonify({"error": f"Error al cargar catálogos: {str(e)}"}), 500

@app.route("/api/admin/backoffice/especialistas", methods=["GET"])
@admin_required()
def backoffice_list_especialistas():
    try:
        rows = db.session.execute(
            text("""
                SELECT
                    e.id,
                    e.esp_name,
                    COALESCE(e.esp_ap_pat, '') AS esp_ap_pat,
                    COALESCE(e.esp_ap_mat, '') AS esp_ap_mat,
                    TRIM(CONCAT_WS(' ', e.esp_name, e.esp_ap_pat, e.esp_ap_mat)) AS nombre_completo,
                    COALESCE(e.esp_activo, true) AS esp_activo,
                    cv.id AS cv_id,
                    COALESCE(cv.ced_prof, '') AS ced_prof,
                    COALESCE(cv.slog, '') AS slog,
                    tel.id AS telefono_id,
                    COALESCE(tel.cla_pai, 52) AS cla_pai,
                    COALESCE(tel.num_tel, '') AS num_tel,
                    ema.id AS email_id,
                    COALESCE(ema.dir_ema, '') AS dir_ema
                FROM idm_tc.idm_especialista_tc e
                LEFT JOIN LATERAL (
                    SELECT id, ced_prof, slog
                    FROM idm_tc.idm_esp_cv_tc
                    WHERE id_esp = e.id
                    ORDER BY id ASC
                    LIMIT 1
                ) cv ON TRUE
                LEFT JOIN LATERAL (
                    SELECT id, cla_pai, num_tel
                    FROM idm_tc.idm_telefono_tc
                    WHERE id_esp = e.id
                    ORDER BY id ASC
                    LIMIT 1
                ) tel ON TRUE
                LEFT JOIN LATERAL (
                    SELECT id, dir_ema
                    FROM idm_tc.idm_email_tc
                    WHERE id_esp = e.id
                    ORDER BY id ASC
                    LIMIT 1
                ) ema ON TRUE
                ORDER BY e.id ASC
            """)
        ).mappings().all()

        especialistas = []
        for r in rows:
            item = dict(r)
            cv_id = item["cv_id"]

            acad = [
                dict(x)
                for x in db.session.execute(
                    text("""
                        SELECT id, id_cv, title, name_title, ced_prof, dt_graduate
                        FROM idm_tc.idm_cv_acad_tc
                        WHERE id_cv = :esp_id
                        ORDER BY id ASC
                    """),
                    {"esp_id": cv_id},
                ).mappings().all()
            ]
            for a in acad:
                if a.get("dt_graduate") and hasattr(a["dt_graduate"], "isoformat"):
                    a["dt_graduate"] = a["dt_graduate"].isoformat()

            cont = [
                dict(x)
                for x in db.session.execute(
                    text("""
                        SELECT id, id_cv, type_doc, name_doc, doc_id, dt_emision
                        FROM idm_tc.idm_cv_cont_tc
                        WHERE id_cv = :esp_id
                        ORDER BY id ASC
                    """),
                    {"esp_id": cv_id},
                ).mappings().all()
            ]
            for c in cont:
                if c.get("dt_emision") and hasattr(c["dt_emision"], "isoformat"):
                    c["dt_emision"] = c["dt_emision"].isoformat()

            areas = [
                dict(x)
                for x in db.session.execute(
                    text("""
                        SELECT id, id_cv, nam_area
                        FROM idm_tc.idm_cv_area_enf_tc
                        WHERE id_cv = :esp_id
                        ORDER BY id ASC
                    """),
                    {"esp_id": cv_id},
                ).mappings().all()
            ]

            item["formacion_academica"] = acad
            item["formacion_continua"] = cont
            item["areas_enfoque"] = areas
            especialistas.append(item)

        return jsonify({"especialistas": especialistas}), 200
    except Exception as e:
        return jsonify({"error": f"Error al consultar especialistas: {str(e)}"}), 500

@app.route("/api/admin/backoffice/servicios", methods=["GET", "POST"])
@admin_required()
def backoffice_servicios():
    if request.method == "GET":
        rows = [
            dict(r)
            for r in db.session.execute(
                text("""
                    SELECT
                        s.id,
                        s.ser_name,
                        TO_CHAR(s.ser_dur, 'HH24:MI') AS ser_dur,
                        s.ser_ses,
                        COALESCE(s.ser_onl, false) AS ser_onl,
                        s.id_epd,
                        COALESCE(e.epd_nombre, '') AS epd_nombre,
                        COALESCE(s.ser_inf, false) AS ser_inf
                    FROM idm_tc.idm_servicio_tc s
                    LEFT JOIN idm_tc.idm_especialidades_tc e ON e.id = s.id_epd
                    ORDER BY e.epd_nombre ASC NULLS LAST, s.ser_name ASC
                """)
            ).mappings().all()
        ]
        return jsonify({"servicios": rows}), 200

    # POST: crear nuevo servicio en idm_tc.idm_servicio_tc
    data = request.get_json(silent=True) or {}
    ser_name = (data.get("ser_name") or "").strip()
    ser_dur_sql = _normalizar_hora_time(data.get("ser_dur") or "00:50")
    id_epd = (data.get("id_epd") or "").strip()
    ser_onl = bool(data.get("ser_onl", False))
    ser_inf = bool(data.get("ser_inf", False))

    try:
        ser_ses = int(data.get("ser_ses", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "El número de sesiones debe ser un entero válido."}), 400

    if not ser_name:
        return jsonify({"error": "El nombre del servicio es obligatorio."}), 400
    if not id_epd:
        return jsonify({"error": "Debes seleccionar la especialidad a la que pertenece el servicio."}), 400
    if ser_ses < 0:
        return jsonify({"error": "El número de sesiones no puede ser negativo."}), 400

    try:
        epd_row = db.session.execute(
            text("SELECT id, epd_nombre FROM idm_tc.idm_especialidades_tc WHERE id = :id"),
            {"id": id_epd},
        ).mappings().first()
        if not epd_row:
            return jsonify({"error": "La especialidad seleccionada no existe."}), 404

        nuevo_id = _siguiente_id_servicio(id_epd, ser_name)

        db.session.execute(
            text("""
                INSERT INTO idm_tc.idm_servicio_tc
                    (id, ser_name, ser_dur, ser_ses, ser_onl, id_epd, ser_inf)
                VALUES
                    (:id, :ser_name, CAST(:ser_dur AS time), :ser_ses, :ser_onl, :id_epd, :ser_inf)
            """),
            {
                "id": nuevo_id,
                "ser_name": ser_name[:50],
                "ser_dur": ser_dur_sql,
                "ser_ses": ser_ses,
                "ser_onl": ser_onl,
                "id_epd": id_epd,
                "ser_inf": ser_inf,
            },
        )
        db.session.commit()

        return jsonify({
            "message": f"Servicio '{ser_name}' creado correctamente.",
            "servicio": {
                "id": nuevo_id,
                "ser_name": ser_name[:50],
                "ser_dur": ser_dur_sql[:5],
                "ser_ses": ser_ses,
                "ser_onl": ser_onl,
                "id_epd": id_epd,
                "epd_nombre": epd_row["epd_nombre"],
                "ser_inf": ser_inf,
            },
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al crear servicio: {str(e)}"}), 500


@app.route("/api/admin/backoffice/especialistas", methods=["POST"])
@admin_required(allowed_roles=["admin","superadmin"])
def backoffice_create_especialista():
    data, foto_file, parse_err = _extraer_datos_y_foto()
    if parse_err:
        return jsonify({"error": parse_err}), 400
    
    print("Data Received:")
    print(data)

    esp_name = (data.get("esp_name") or "").strip()
    esp_ap_pat = (data.get("esp_ap_pat") or "").strip()
    esp_ap_mat = (data.get("esp_ap_mat") or "").strip()
    esp_ap_mat = (data.get("esp_ap_mat") or "").strip()
    esp_activo = bool(data.get("esp_activo", True))

    estado_input = (data.get("estado_id") or data.get("estado_codigo") or "").strip()
    cod_estado = _obtener_codigo_estado(estado_input)

    ced_prof = (data.get("ced_prof") or "").strip()
    slog = (data.get("slog") or "").strip()

    cla_pai = int(data.get("cla_pai") or 52)
    num_tel = re.sub(r"\D", "", str(data.get("num_tel") or "").strip())
    dir_ema = (data.get("dir_ema") or "").strip()

    acad_items = data.get("formacion_academica") or []
    cont_items = data.get("formacion_continua") or []
    areas_items = data.get("areas_enfoque") or []
    ser_items = data.get("servicios_ids") or []
    suc_items = data.get("sucursales_ids") or []
    len_items = data.get("lenguas_ids") or []

    if not esp_name or not esp_ap_pat:
        return jsonify({"error": "Nombre y apellido paterno son obligatorios."}), 400
    if not cod_estado or len(cod_estado) != 2:
        return jsonify({"error": "Debes seleccionar un estado válido para componer el ID."}), 400
    if not ced_prof:
        return jsonify({"error": "La cédula profesional es obligatoria para dar de alta el CV del especialista."}), 400

    # Validar extensión/tipo de la foto antes de insertar en BD
    if foto_file and foto_file.filename:
        nombre_orig = foto_file.filename.lower()
        mime_orig = (foto_file.mimetype or "").lower()
        if not (nombre_orig.endswith((".jpg", ".jpeg")) or mime_orig in ("image/jpeg", "image/jpg")):
            return jsonify({"error": "La foto del especialista debe estar en formato JPG (.jpg)."}), 400

    try:
        prefijo_esp = f"IDM{cod_estado}"
        prefijo_cv = f"ICV{cod_estado}"

        esp_id = _siguiente_consecutivo_prefijo("idm_tc.idm_especialista_tc", prefijo_esp, 5)
        cv_id = _siguiente_consecutivo_prefijo("idm_tc.idm_esp_cv_tc", prefijo_cv, 5)

        # 1. Insertar especialista
        db.session.execute(
            text("""
                INSERT INTO idm_tc.idm_especialista_tc
                    (id, esp_name, esp_ap_pat, esp_ap_mat, esp_activo,esp_dt_ini,esp_dt_fin)
                VALUES
                    (:id, :esp_name, :esp_ap_pat, :esp_ap_mat, :esp_activo,CURRENT_DATE, '2099-12-12 23:59:59')
            """),
            {
                "id": esp_id,
                "esp_name": esp_name,
                "esp_ap_pat": esp_ap_pat,
                "esp_ap_mat": esp_ap_mat,
                "esp_activo": esp_activo,
            },
        )

        # 2. Insertar CV base obligatorio (ced_prof)
        db.session.execute(
            text("""
                INSERT INTO idm_tc.idm_esp_cv_tc
                    (id, id_esp, slog, ced_prof)
                VALUES
                    (:id, :id_esp, :slog, :ced_prof)
            """),
            {
                "id": cv_id,
                "id_esp": esp_id,
                "slog": slog,
                "ced_prof": ced_prof,
            },
        )

        # 3. Insertar teléfono (id_pac vacío/NULL, id_esp = esp_id)
        if num_tel:
            tel_id = _siguiente_consecutivo_prefijo("idm_tc.idm_telefono_tc", "GT", 8)
            db.session.execute(
                text("""
                    INSERT INTO idm_tc.idm_telefono_tc
                        (id, cla_pai, num_tel, id_pac, id_esp)
                    VALUES
                        (:id, :cla_pai, :num_tel, NULL, :id_esp)
                """),
                {
                    "id": tel_id,
                    "cla_pai": cla_pai,
                    "num_tel": num_tel[:10],
                    "id_esp": esp_id,
                },
            )

        # 4. Insertar correo (id_pac vacío/NULL, id_esp = esp_id)
        if dir_ema:
            ema_id = _siguiente_consecutivo_prefijo("idm_tc.idm_email_tc", "GE", 8)
            db.session.execute(
                text("""
                    INSERT INTO idm_tc.idm_email_tc
                        (id, dir_ema, id_pac, id_esp)
                    VALUES
                        (:id, :dir_ema, NULL, :id_esp)
                """),
                {
                    "id": ema_id,
                    "dir_ema": dir_ema,
                    "id_esp": esp_id,
                },
            )

        # 5. Formación académica (prefijo ACA + 7 dígitos)
        for item in acad_items:
            title = (item.get("title") or "").strip()
            name_title = (item.get("name_title") or "").strip()
            ced_acad = (item.get("ced_prof") or "").strip()
            dt_grad = (item.get("dt_graduate") or "").strip() or None
            if title or name_title:
                aca_id = _siguiente_consecutivo_prefijo("idm_tc.idm_cv_acad_tc", "ACA", 7)
                db.session.execute(
                    text("""
                        INSERT INTO idm_tc.idm_cv_acad_tc
                            (id, id_cv, title, name_title, ced_prof, dt_graduate)
                        VALUES
                            (:id, :id_esp, :title, :name_title, :ced_prof, :dt_graduate)
                    """),
                    {
                        "id": aca_id,
                        "id_esp": cv_id,
                        "title": title,
                        "name_title": name_title,
                        "ced_prof": ced_acad,
                        "dt_graduate": dt_grad,
                    },
                )
        #Agrega registro de servicios-especialista para crear servicios.
        for item in ser_items:
            ser_id = item

            db.session.execute(
                text("""
                    WITH lastid as (
                    select max(id) as lid from idm_tr.idm_ser_esp_tr
                    )
                    INSERT INTO idm_tr.idm_ser_esp_tr
                        (id,id_esp, id_ser)
                    VALUES
                        ((select lastid.lid+1 from lastid), :id_esp, :ser_id)
                    """),
                {
                    "id_esp": esp_id,
                    "ser_id": ser_id
                    },
                )

        for item in suc_items:
            suc_id = item

            db.session.execute(
                text("""
                    WITH lastid as (
                    select max(id) as lid from idm_tr.idm_esp_suc_tr
                    )
                    INSERT INTO idm_tr.idm_esp_suc_tr
                        (id,id_esp, id_suc)
                    VALUES
                        ((select lastid.lid+1 from lastid), :id_esp, :suc_id)
                    """),
                {
                    "id_esp": esp_id,
                    "suc_id": suc_id
                    },
                )

        for item in len_items:
            len_id = item

            db.session.execute(
                text("""
                    WITH lastid as (
                    select max(id) as lid from idm_tr.idm_esp_len_tr
                    )
                    INSERT INTO idm_tr.idm_esp_len_tr
                        (id,id_esp, id_len)
                    VALUES
                        ((select lastid.lid+1 from lastid), :id_esp, :len_id)
                    """),
                {
                    "id_esp": esp_id,
                    "len_id": len_id
                    },
                )

        # 6. Formación continua (prefijo ECO + 7 dígitos)
        for item in cont_items:
            type_doc = (item.get("type_doc") or "").strip()
            name_doc = (item.get("name_doc") or "").strip()
            doc_id = (item.get("doc_id") or "").strip()
            dt_emi = (item.get("dt_emision") or "").strip() or None
            if type_doc or name_doc:
                eco_id = _siguiente_consecutivo_prefijo("idm_tc.idm_cv_cont_tc", "ECO", 7)
                db.session.execute(
                    text("""
                        INSERT INTO idm_tc.idm_cv_cont_tc
                            (id, id_cv, type_doc, name_doc, doc_id, dt_emision)
                        VALUES
                            (:id, :id_esp, :type_doc, :name_doc, :doc_id, :dt_emision)
                    """),
                    {
                        "id": eco_id,
                        "id_esp": cv_id,
                        "type_doc": type_doc,
                        "name_doc": name_doc,
                        "doc_id": doc_id,
                        "dt_emision": dt_emi,
                    },
                )

        # 7. Área de enfoque (ID numérico entero consecutivo)
        for item in areas_items:
            nam_area = (item.get("nam_area") or "").strip()
            if nam_area:
                area_id = _siguiente_id_entero("idm_tc.idm_cv_area_enf_tc")
                db.session.execute(
                    text("""
                        INSERT INTO idm_tc.idm_cv_area_enf_tc
                            (id, id_cv, nam_area)
                        VALUES
                            (:id, :id_esp, :nam_area)
                    """),
                    {
                        "id": area_id,
                        "id_esp": cv_id,
                        "nam_area": nam_area,
                    },
                )
        
        

        # 8. Guardar foto en .../src/assets/especialistas/<esp_id>.jpg
        foto_guardada = None
        if foto_file and foto_file.filename:
            carpeta_fotos = _resolver_carpeta_fotos_especialistas()
            print("carpeta_fotos")
            destino_foto = carpeta_fotos / f"{esp_id}.jpg"
            foto_file.save(str(destino_foto))
            foto_guardada = str(destino_foto)

        db.session.commit()

        try:
            log_admin_action(
                action="CREATE_ESPECIALISTA",
                entity="idm_especialista_tc",
                entity_id=str(esp_id),
                details={
                    "cv_id": cv_id,
                    "ced_prof": ced_prof,
                    "estado": cod_estado,
                    "foto": f"{esp_id}.jpg" if foto_guardada else None,
                },
            )
        except Exception:
            pass

        return jsonify({
            "message": f"Especialista {esp_id} registrado correctamente.",
            "id": esp_id,
            "cv_id": cv_id,
            "foto": f"{esp_id}.jpg" if foto_guardada else None,
        }), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al crear especialista: {str(e)}"}), 500


@app.route("/api/admin/backoffice/especialistas/<esp_id>", methods=["PUT", "PATCH"])
@admin_required()
def backoffice_update_especialista(esp_id):
    data = request.get_json(silent=True) or {}

    esp_name = (data.get("esp_name") or "").strip()
    esp_ap_pat = (data.get("esp_ap_pat") or "").strip()
    esp_ap_mat = (data.get("esp_ap_mat") or "").strip()
    esp_activo = bool(data.get("esp_activo", True))

    estado_input = (data.get("estado_id") or data.get("estado_codigo") or "").strip()
    cod_estado = _obtener_codigo_estado(estado_input) if estado_input else ""

    ced_prof = (data.get("ced_prof") or "").strip()
    slog = (data.get("slog") or "").strip()

    cla_pai = int(data.get("cla_pai") or 52)
    num_tel = re.sub(r"\D", "", str(data.get("num_tel") or "").strip())
    dir_ema = (data.get("dir_ema") or "").strip()

    acad_items = data.get("formacion_academica") or []
    cont_items = data.get("formacion_continua") or []
    areas_items = data.get("areas_enfoque") or []

    if not esp_name or not esp_ap_pat:
        return jsonify({"error": "Nombre y apellido paterno son obligatorios."}), 400
    if not ced_prof:
        return jsonify({"error": "La cédula profesional no puede quedar vacía."}), 400

    try:
        existe = db.session.execute(
            text("SELECT id FROM idm_tc.idm_especialista_tc WHERE id = :id"),
            {"id": esp_id},
        ).scalar()
        if not existe:
            db.session.rollback()
            return jsonify({"error": "Especialista no encontrado."}), 404

        id_final_esp = esp_id
        # Si cambió el estado y el nuevo prefijo IDM<XX> no coincide con el ID actual, recomponer ID
        if cod_estado and len(cod_estado) == 2:
            nuevo_prefijo_esp = f"IDM{cod_estado}"
            if not str(esp_id).startswith(nuevo_prefijo_esp):
                id_final_esp = _siguiente_consecutivo_prefijo("idm_tc.idm_especialista_tc", nuevo_prefijo_esp, 5)

        if id_final_esp != esp_id:
            # Si cambia la PK en idm_especialista_tc, insertamos con nuevo ID, migramos hijas y desactivamos/removemos anterior
            db.session.execute(
                text("""
                    INSERT INTO idm_tc.idm_especialista_tc
                        (id, esp_name, esp_ap_pat, esp_ap_mat, esp_activo)
                    VALUES
                        (:nuevo_id, :esp_name, :esp_ap_pat, :esp_ap_mat, :esp_activo)
                """),
                {
                    "nuevo_id": id_final_esp,
                    "esp_name": esp_name,
                    "esp_ap_pat": esp_ap_pat,
                    "esp_ap_mat": esp_ap_mat,
                    "esp_activo": esp_activo,
                },
            )
            db.session.execute(
                text("UPDATE idm_tc.idm_esp_cv_tc SET id_esp = :nuevo_id WHERE id_esp = :viejo_id"),
                {"nuevo_id": id_final_esp, "viejo_id": esp_id},
            )
            db.session.execute(
                text("UPDATE idm_tc.idm_cv_acad_tc SET id_esp = :nuevo_id WHERE id_esp = :viejo_id"),
                {"nuevo_id": id_final_esp, "viejo_id": esp_id},
            )
            db.session.execute(
                text("UPDATE idm_tc.idm_cv_cont_tc SET id_esp = :nuevo_id WHERE id_esp = :viejo_id"),
                {"nuevo_id": id_final_esp, "viejo_id": esp_id},
            )
            db.session.execute(
                text("UPDATE idm_tc.idm_cv_area_enf_tc SET id_esp = :nuevo_id WHERE id_esp = :viejo_id"),
                {"nuevo_id": id_final_esp, "viejo_id": esp_id},
            )
            db.session.execute(
                text("UPDATE idm_tc.idm_telefono_tc SET id_esp = :nuevo_id WHERE id_esp = :viejo_id"),
                {"nuevo_id": id_final_esp, "viejo_id": esp_id},
            )
            db.session.execute(
                text("UPDATE idm_tc.idm_email_tc SET id_esp = :nuevo_id WHERE id_esp = :viejo_id"),
                {"nuevo_id": id_final_esp, "viejo_id": esp_id},
            )
            try:
                db.session.execute(
                    text("DELETE FROM idm_tc.idm_especialista_tc WHERE id = :viejo_id"),
                    {"viejo_id": esp_id},
                )
            except Exception:
                db.session.execute(
                    text("UPDATE idm_tc.idm_especialista_tc SET esp_activo = false WHERE id = :viejo_id"),
                    {"viejo_id": esp_id},
                )
        else:
            db.session.execute(
                text("""
                    UPDATE idm_tc.idm_especialista_tc
                    SET
                        esp_name = :esp_name,
                        esp_ap_pat = :esp_ap_pat,
                        esp_ap_mat = :esp_ap_mat,
                        esp_activo = :esp_activo
                    WHERE id = :id
                """),
                {
                    "id": id_final_esp,
                    "esp_name": esp_name,
                    "esp_ap_pat": esp_ap_pat,
                    "esp_ap_mat": esp_ap_mat,
                    "esp_activo": esp_activo,
                },
            )

        # Actualizar o crear CV base
        cv_existente = db.session.execute(
            text("SELECT id FROM idm_tc.idm_esp_cv_tc WHERE id_esp = :id_esp LIMIT 1"),
            {"id_esp": id_final_esp},
        ).scalar()

        if cv_existente:
            db.session.execute(
                text("""
                    UPDATE idm_tc.idm_esp_cv_tc
                    SET ced_prof = :ced_prof, slog = :slog
                    WHERE id = :cv_id
                """),
                {"cv_id": cv_existente, "ced_prof": ced_prof, "slog": slog},
            )
        else:
            prefijo_cv = f"ICV{cod_estado if len(cod_estado) == 2 else 'MX'}"
            nuevo_cv_id = _siguiente_consecutivo_prefijo("idm_tc.idm_esp_cv_tc", prefijo_cv, 5)
            db.session.execute(
                text("""
                    INSERT INTO idm_tc.idm_esp_cv_tc (id, id_esp, slog, ced_prof)
                    VALUES (:id, :id_esp, :slog, :ced_prof)
                """),
                {"id": nuevo_cv_id, "id_esp": id_final_esp, "slog": slog, "ced_prof": ced_prof},
            )

        # Actualizar o insertar teléfono
        tel_existente = db.session.execute(
            text("SELECT id FROM idm_tc.idm_telefono_tc WHERE id_esp = :id_esp LIMIT 1"),
            {"id_esp": id_final_esp},
        ).scalar()
        if num_tel:
            if tel_existente:
                db.session.execute(
                    text("""
                        UPDATE idm_tc.idm_telefono_tc
                        SET cla_pai = :cla_pai, num_tel = :num_tel, id_pac = NULL
                        WHERE id = :tel_id
                    """),
                    {"tel_id": tel_existente, "cla_pai": cla_pai, "num_tel": num_tel[:10]},
                )
            else:
                tel_id = _siguiente_consecutivo_prefijo("idm_tc.idm_telefono_tc", "GT", 8)
                db.session.execute(
                    text("""
                        INSERT INTO idm_tc.idm_telefono_tc (id, cla_pai, num_tel, id_pac, id_esp)
                        VALUES (:id, :cla_pai, :num_tel, NULL, :id_esp)
                    """),
                    {"id": tel_id, "cla_pai": cla_pai, "num_tel": num_tel[:10], "id_esp": id_final_esp},
                )

        # Actualizar o insertar correo
        ema_existente = db.session.execute(
            text("SELECT id FROM idm_tc.idm_email_tc WHERE id_esp = :id_esp LIMIT 1"),
            {"id_esp": id_final_esp},
        ).scalar()
        if dir_ema:
            if ema_existente:
                db.session.execute(
                    text("""
                        UPDATE idm_tc.idm_email_tc
                        SET dir_ema = :dir_ema, id_pac = NULL
                        WHERE id = :ema_id
                    """),
                    {"ema_id": ema_existente, "dir_ema": dir_ema},
                )
            else:
                ema_id = _siguiente_consecutivo_prefijo("idm_tc.idm_email_tc", "GE", 8)
                db.session.execute(
                    text("""
                        INSERT INTO idm_tc.idm_email_tc (id, dir_ema, id_pac, id_esp)
                        VALUES (:id, :dir_ema, NULL, :id_esp)
                    """),
                    {"id": ema_id, "dir_ema": dir_ema, "id_esp": id_final_esp},
                )

        # Sincronizar formación académica (actualiza existentes o inserta nuevos con ACA + 7 dígitos)
        for item in acad_items:
            item_id = (item.get("id") or "").strip()
            title = (item.get("title") or "").strip()
            name_title = (item.get("name_title") or "").strip()
            ced_acad = (item.get("ced_prof") or "").strip()
            dt_grad = (item.get("dt_graduate") or "").strip() or None
            if not (title or name_title or ced_acad or dt_grad):
                continue
            if item_id:
                db.session.execute(
                    text("""
                        UPDATE idm_tc.idm_cv_acad_tc
                        SET title = :title, name_title = :name_title, ced_prof = :ced_prof, dt_graduate = :dt_graduate
                        WHERE id = :id AND id_cv = :id_esp
                    """),
                    {
                        "id": item_id,
                        "id_esp": nuevo_cv_id,
                        "title": title,
                        "name_title": name_title,
                        "ced_prof": ced_acad,
                        "dt_graduate": dt_grad,
                    },
                )
            else:
                aca_id = _siguiente_consecutivo_prefijo("idm_tc.idm_cv_acad_tc", "ACA", 7)
                db.session.execute(
                    text("""
                        INSERT INTO idm_tc.idm_cv_acad_tc
                            (id, id_cv, title, name_title, ced_prof, dt_graduate)
                        VALUES
                            (:id, :id_esp, :title, :name_title, :ced_prof, :dt_graduate)
                    """),
                    {
                        "id": aca_id,
                        "id_esp": nuevo_cv_id,
                        "title": title,
                        "name_title": name_title,
                        "ced_prof": ced_acad,
                        "dt_graduate": dt_grad,
                    },
                )

        # Sincronizar formación continua (actualiza existentes o inserta nuevos con ECO + 7 dígitos)
        for item in cont_items:
            item_id = (item.get("id") or "").strip()
            type_doc = (item.get("type_doc") or "").strip()
            name_doc = (item.get("name_doc") or "").strip()
            doc_id = (item.get("doc_id") or "").strip()
            dt_emi = (item.get("dt_emision") or "").strip() or None
            if not (type_doc or name_doc or doc_id or dt_emi):
                continue
            if item_id:
                db.session.execute(
                    text("""
                        UPDATE idm_tc.idm_cv_cont_tc
                        SET type_doc = :type_doc, name_doc = :name_doc, doc_id = :doc_id, dt_emision = :dt_emision
                        WHERE id = :id AND id_cv = :id_esp
                    """),
                    {
                        "id": item_id,
                        "id_esp": nuevo_cv_id,
                        "type_doc": type_doc,
                        "name_doc": name_doc,
                        "doc_id": doc_id,
                        "dt_emision": dt_emi,
                    },
                )
            else:
                eco_id = _siguiente_consecutivo_prefijo("idm_tc.idm_cv_cont_tc", "ECO", 7)
                db.session.execute(
                    text("""
                        INSERT INTO idm_tc.idm_cv_cont_tc
                            (id, id_cv, type_doc, name_doc, doc_id, dt_emision)
                        VALUES
                            (:id, :id_esp, :type_doc, :name_doc, :doc_id, :dt_emision)
                    """),
                    {
                        "id": eco_id,
                        "id_esp": nuevo_cv_id,
                        "type_doc": type_doc,
                        "name_doc": name_doc,
                        "doc_id": doc_id,
                        "dt_emision": dt_emi,
                    },
                )

        # Sincronizar áreas de enfoque (ID entero consecutivo)
        for item in areas_items:
            item_id = item.get("id")
            nam_area = (item.get("nam_area") or "").strip()
            if not nam_area:
                continue
            if item_id is not None and str(item_id).strip() != "":
                db.session.execute(
                    text("""
                        UPDATE idm_tc.idm_cv_area_enf_tc
                        SET nam_area = :nam_area
                        WHERE id = :id AND id_cv = :id_esp
                    """),
                    {"id": int(str(item_id)), "id_esp": nuevo_cv_id, "nam_area": nam_area},
                )
            else:
                area_id = _siguiente_id_entero("idm_tc.idm_cv_area_enf_tc")
                db.session.execute(
                    text("""
                        INSERT INTO idm_tc.idm_cv_area_enf_tc (id, id_cv, nam_area)
                        VALUES (:id, :id_esp, :nam_area)
                    """),
                    {"id": area_id, "id_esp": nuevo_cv_id, "nam_area": nam_area},
                )

        db.session.commit()

        try:
            log_admin_action(
                action="UPDATE_ESPECIALISTA",
                entity="idm_especialista_tc",
                entity_id=str(id_final_esp),
                details={"ced_prof": ced_prof, "esp_activo": esp_activo},
            )
        except Exception:
            pass

        return jsonify({
            "message": f"Especialista {id_final_esp} actualizado correctamente.",
            "id": id_final_esp,
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al actualizar especialista: {str(e)}"}), 500


@app.route("/api/admin/backoffice/especialistas/<esp_id>/estado", methods=["PATCH"])
@admin_required(allowed_roles=["admin","superadmin"])
def backoffice_toggle_especialista_estado(esp_id):
    data = request.get_json(silent=True) or {}
    if "esp_activo" not in data:
        return jsonify({"error": "Falta el campo esp_activo."}), 400

    nuevo_estado = bool(data.get("esp_activo"))

    try:
        res = db.session.execute(
            text("""
                UPDATE idm_tc.idm_especialista_tc
                SET esp_activo = :esp_activo
                WHERE id = :id
            """),
            {"id": esp_id, "esp_activo": nuevo_estado},
        )
        if res.rowcount == 0:
            db.session.rollback()
            return jsonify({"error": "Especialista no encontrado."}), 404

        db.session.commit()

        try:
            log_admin_action(
                action="TOGGLE_ESPECIALISTA_ESTADO",
                entity="idm_especialista_tc",
                entity_id=str(esp_id),
                details={"esp_activo": nuevo_estado},
            )
        except Exception:
            pass

        return jsonify({
            "message": f"Especialista {'activado' if nuevo_estado else 'desactivado'} correctamente."
        }), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al cambiar estado del especialista: {str(e)}"}), 500

@app.route("/api/admin/backoffice/especialistas/<esp_id>/horarios", methods=["GET", "POST"])
@admin_required()
def backoffice_horarios_especialista(esp_id):
    if request.method == "GET":
        sem_day_param = (request.args.get("sem_day") or "").strip()
        sql = """
            SELECT
                h.id,
                h.esp_id,
                h.sem_day,
                TO_CHAR(h.hora_ini, 'HH24:MI') AS hora_ini,
                TO_CHAR(h.hora_fin, 'HH24:MI') AS hora_fin,
                h.id_suc,
                COALESCE(s.suc_name, h.id_suc) AS suc_name
            FROM idm_tr.idm_horarios_tc h
            LEFT JOIN idm_tc.idm_sucursal_tc s ON CAST(s.id AS TEXT) = CAST(h.id_suc AS TEXT)
            WHERE h.esp_id = :esp_id
        """
        params = {"esp_id": esp_id}

        if sem_day_param != "":
            try:
                sem_day_filtro = int(sem_day_param)
            except (TypeError, ValueError):
                return jsonify({"error": "El día de la semana (sem_day) debe ser un número entre 0 y 6."}), 400
            if sem_day_filtro < 0 or sem_day_filtro > 6:
                return jsonify({"error": "El día de la semana debe estar entre 0 (Lunes) y 6 (Domingo)."}), 400
            sql += " AND h.sem_day = :sem_day"
            params["sem_day"] = sem_day_filtro

        sql += " ORDER BY h.sem_day ASC, h.hora_ini ASC, h.id ASC"

        try:
            rows = [dict(r) for r in db.session.execute(text(sql), params).mappings().all()]
            return jsonify({"horarios": rows}), 200
        except Exception as e:
            return jsonify({"error": f"Error al consultar horarios: {str(e)}"}), 500

    # --- MÉTODO POST ---
    data = request.get_json(silent=True) or {}
    sem_day_raw = data.get("sem_day")
    hora_ini = (data.get("hora_ini") or "").strip()
    hora_fin = (data.get("hora_fin") or "").strip()
    id_suc = (data.get("id_suc") or "").strip()

    try:
        sem_day = int(sem_day_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "El día de la semana (sem_day) debe ser un número entre 0 y 6."}), 400

    if sem_day < 0 or sem_day > 6:
        return jsonify({"error": "El día de la semana debe estar entre 0 (Lunes) y 6 (Domingo)."}), 400
    if not id_suc:
        return jsonify({"error": "Debes seleccionar la sucursal."}), 400
    if not hora_ini or not hora_fin:
        return jsonify({"error": "Debes indicar hora de inicio y hora de fin."}), 400

    if len(hora_ini) == 5:
        hora_ini_sql = f"{hora_ini}:00"
    else:
        hora_ini_sql = hora_ini

    if len(hora_fin) == 5:
        hora_fin_sql = f"{hora_fin}:00"
    else:
        hora_fin_sql = hora_fin

    if hora_fin_sql <= hora_ini_sql:
        return jsonify({"error": "La hora de finalización debe ser posterior a la hora de inicio."}), 400

    try:
        esp_existe = db.session.execute(
            text("SELECT id FROM idm_tc.idm_especialista_tc WHERE id = :esp_id"),
            {"esp_id": esp_id},
        ).scalar()
        if not esp_existe:
            return jsonify({"error": "Especialista no encontrado."}), 404

        next_id = db.session.execute(
            text("SELECT COALESCE(MAX(id), 0) + 1 FROM idm_tr.idm_horarios_tc")
        ).scalar()

        db.session.execute(
            text("""
                INSERT INTO idm_tr.idm_horarios_tc
                    (id, esp_id, sem_day, hora_ini, hora_fin, id_suc)
                VALUES
                    (:id, :esp_id, :sem_day, CAST(:hora_ini AS time), CAST(:hora_fin AS time), :id_suc)
            """),
            {
                "id": int(next_id),
                "esp_id": esp_id,
                "sem_day": sem_day,
                "hora_ini": hora_ini_sql,
                "hora_fin": hora_fin_sql,
                "id_suc": id_suc,
            },
        )
        db.session.commit()

        try:
            log_admin_action(
                action="CREATE_HORARIO_ESPECIALISTA",
                entity="idm_horario_tc",
                entity_id=str(next_id),
                details={
                    "esp_id": esp_id,
                    "sem_day": sem_day,
                    "id_suc": id_suc,
                    "hora_ini": hora_ini_sql,
                    "hora_fin": hora_fin_sql,
                },
            )
        except Exception:
            pass

        return jsonify({"message": "Horario agregado correctamente.", "id": int(next_id)}), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al guardar horario: {str(e)}"}), 500


@app.route("/api/admin/backoffice/especialistas/<esp_id>/horarios/<int:horario_id>", methods=["PUT", "PATCH"])
@admin_required()
def backoffice_update_horario_especialista(esp_id, horario_id):
    data = request.get_json(silent=True) or {}
    sem_day_raw = data.get("sem_day")
    hora_ini = (data.get("hora_ini") or "").strip()
    hora_fin = (data.get("hora_fin") or "").strip()
    id_suc = (data.get("id_suc") or "").strip()

    try:
        sem_day = int(sem_day_raw)
    except (TypeError, ValueError):
        return jsonify({"error": "El día de la semana (sem_day) debe ser un número entre 0 y 6."}), 400

    if sem_day < 0 or sem_day > 6:
        return jsonify({"error": "El día de la semana debe estar entre 0 (Lunes) y 6 (Domingo)."}), 400
    if not id_suc:
        return jsonify({"error": "Debes seleccionar la sucursal."}), 400
    if not hora_ini or not hora_fin:
        return jsonify({"error": "Debes indicar hora de inicio y hora de fin."}), 400

    hora_ini_sql = f"{hora_ini}:00" if len(hora_ini) == 5 else hora_ini
    hora_fin_sql = f"{hora_fin}:00" if len(hora_fin) == 5 else hora_fin

    if hora_fin_sql <= hora_ini_sql:
        return jsonify({"error": "La hora de finalización debe ser posterior a la hora de inicio."}), 400

    try:
        res = db.session.execute(
            text("""
                UPDATE idm_tr.idm_horarios_tc
                SET
                    sem_day = :sem_day,
                    hora_ini = CAST(:hora_ini AS time),
                    hora_fin = CAST(:hora_fin AS time),
                    id_suc = :id_suc
                WHERE id = :horario_id AND esp_id = :esp_id
            """),
            {
                "horario_id": horario_id,
                "esp_id": esp_id,
                "sem_day": sem_day,
                "hora_ini": hora_ini_sql,
                "hora_fin": hora_fin_sql,
                "id_suc": id_suc,
            },
        )
        if res.rowcount == 0:
            db.session.rollback()
            return jsonify({"error": "Horario no encontrado para este especialista."}), 404

        db.session.commit()

        try:
            log_admin_action(
                action="UPDATE_HORARIO_ESPECIALISTA",
                entity="idm_horario_tc",
                entity_id=str(horario_id),
                details={"esp_id": esp_id, "sem_day": sem_day, "id_suc": id_suc, "hora_ini": hora_ini_sql, "hora_fin": hora_fin_sql},
            )
        except Exception:
            pass

        return jsonify({"message": "Horario actualizado correctamente."}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al actualizar horario: {str(e)}"}), 500


@app.route("/api/admin/backoffice/especialistas/<esp_id>/horarios/<int:horario_id>", methods=["DELETE"])
@admin_required()
def backoffice_delete_horario_especialista(esp_id, horario_id):
    sem_day_param = request.args.get("sem_day", "").strip()
    try:
        sql = """
            DELETE FROM idm_tr.idm_horarios_tc
            WHERE id = :horario_id AND esp_id = :esp_id
        """
        params = {"horario_id": horario_id, "esp_id": esp_id}
        if sem_day_param != "":
            sql += " AND sem_day = :sem_day"
            params["sem_day"] = int(sem_day_param)

        res = db.session.execute(text(sql), params)
        if res.rowcount == 0:
            db.session.rollback()
            return jsonify({"error": "No se encontró el horario a eliminar."}), 404

        db.session.commit()

        try:
            log_admin_action(
                action="DELETE_HORARIO_ESPECIALISTA",
                entity="idm_horario_tc",
                entity_id=str(horario_id),
                details={"esp_id": esp_id},
            )
        except Exception:
            pass

        return jsonify({"message": "Horario eliminado correctamente."}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al eliminar horario: {str(e)}"}), 500




if __name__ == '__main__':
    with app.app_context():
        init_admin_schema()
    app.run(debug=True, port=5000)
