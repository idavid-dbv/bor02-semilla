import os
from datetime import datetime, date, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from sqlalchemy import text, and_, or_, bindparam
from datetime import date, time, datetime, timedelta
from flask_mail import Mail, Message

load_dotenv()

S3_BUCKET = os.getenv('S3_BUCKET_NAME')
AWS_KEY = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION = os.getenv('AWS_REGION')

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# ==============================================================================
# 1. SALUD Y COMPROBACIÓN
# ==============================================================================
@app.route('/api/healthcheck', methods=['GET'])
def healthcheck():
    try:
        db.session.execute(text('SELECT 1'))
        return jsonify({"status": "ok", "database": "Conectado a RDS correctamente"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



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
            # Especialistas por especialidad
            query_espc = text("""
                with a as (
            select s.id as id_ser, epd_nombre,esp.id as epd_id 
		    from idm_tc.idm_servicio_tc s 
		    join idm_tc.idm_especialidad_tc esp on s.id_epd=esp.id
            ),
            b as(
            select concat(esp.esp_name, ' ', esp.esp_ap_pat) as nombre, s.id_ser id_ser, esp.id as id_esp, esp.esp_activo as act
            from idm_tc.idm_especialista_tc esp
            join idm_tr.idm_ser_esp_tr s
            	on s.id_esp = esp.id
            )
            select distinct id_esp as id, nombre as nombre, epd_nombre as titulo
            from a join b on b.id_ser = a.id_ser
            where 1=1
            and epd_id = :esp_id
            and act = true
            """)
            especialistas_raw = db.session.execute(query_espc, {"esp_id": esp['id']}).mappings().all()

            lista_especialistas = []
            for espc in especialistas_raw:
                # Servicios que ofrece el especialista/especialidad
                query_serv = text("""
                    with servicio as(
                    SELECT ser.id as id, ser_name AS nombre, tar.tar_mon AS costo
                    FROM idm_tc.idm_servicio_tc ser
                    join idm_tc.idm_tarifa_tc tar
                     on tar.id_ser = ser.id
                    )
                    select servicio.id as id
	                , servicio.nombre as nombre
	                , servicio.costo as costo
	                from servicio
	                join idm_tr.idm_ser_esp_tr iset 
	                 on iset.id_ser = servicio.id
	                 where iset.id_esp = :esp_id 
                """)
                servicios_raw = db.session.execute(query_serv, {"esp_id": espc['id']}).mappings().all()
                lista_especialistas.append({
                    "id": espc['id'],
                    "nombre": espc['nombre'],
                    "titulo": espc['titulo'],
                    "servicios": [dict(s) for s in servicios_raw]
                })
            #print(esp['id'],esp['nombre'])
            resultado.append({
                "id": esp['id'],
                "nombre": esp['nombre'],
                "especialistas": lista_especialistas
            })

        return jsonify(resultado), 200
    except Exception as e:
        print(especialistas_raw,lista_especialistas,espc['id'])
        return jsonify({"error": str(e)}), 500


# ==============================================================================
# ENDPOINT PARA /ubicaciones: ESTADOS -> CIUDADES -> SUCURSALES -> CONSULTORIOS
# ==============================================================================
@app.route('/api/ubicaciones', methods=['GET'])
def obtener_ubicaciones_arbol():
    try:
        # Agrupación jerárquica (Estado -> Ciudad -> Sucursales)
        query_sucursales = text("""
            SELECT 
                s.id AS suc_id, 
                s.suc_name AS suc_nombre, 
                s.suc_dir AS suc_direccion,
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
                SELECT id, con_num AS numero, con_type AS tipo
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
                    "servicios": ["Atención Presencial", "Valoración Clínica"]
                })

            estados_dict[est_id]["ciudades_dict"][ciu_id]["sucursales"].append({
                "id": suc_id,
                "nombre": f['suc_nombre'],
                "direccion": f['suc_direccion'],
                "telefono": "462-000-0000",
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

    query_base1 = """
        WITH es_ser AS (
            SELECT iset.id_esp, iset.id_ser, itt.tar_mon AS costo 
            FROM idm_tc.idm_especialista_tc iet
            INNER JOIN idm_tr.idm_ser_esp_tr iset ON iset.id_esp = iet.id
            INNER JOIN idm_tc.idm_tarifa_tc itt ON iset.id_ser = itt.id_ser
        ),
        ser_epd AS (
            SELECT iet.id AS id_epd, iet.epd_nombre AS especialidad, ist.id AS id_ser, ist.ser_name AS servicio 
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
        query_serv = query_base1 + " DISTINCT ser_epd.id_ser AS id, ser_epd.servicio AS nombre, es_ser.costo " + query_base2 + cond_serv
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
@app.route('/api/disponibilidad/fechas', methods=['GET'])
def obtener_fechas_disponibles():
    especialista_id = request.args.get('especialista_id')
    ubicacion_id = request.args.get('ubicacion_id')

    if not especialista_id or not ubicacion_id:
        return jsonify([]), 200

    try:
        # Generar rango de fechas (próximos 30 días)
        hoy = date.today()
        dias_disponibles = []
        
        # Consultar días de agenda estándar del especialista en la tabla Tanda
        query_tanda = text("""
            SELECT DISTINCT sem_day 
            FROM idm_tr.idm_horarios_tc
            WHERE esp_id = :espc_id
        """)
        dias_laborables_raw = db.session.execute(query_tanda, {"espc_id": especialista_id}).fetchall()
        dias_semana_permitidos = [r[0].lower() for r in dias_laborables_raw] if dias_laborables_raw else ["lunes", "martes", "miercoles", "jueves", "viernes"]

        mapa_dias = {0: "lunes", 1: "martes", 2: "miercoles", 3: "jueves", 4: "viernes", 5: "sabado", 6: "domingo"}

        for i in range(1, 31):
            fecha_eval = hoy + timedelta(days=i)
            nombre_dia = mapa_dias[fecha_eval.weekday()]
            
            if nombre_dia in dias_semana_permitidos:
                dias_disponibles.append(fecha_eval.strftime('%Y-%m-%d'))

        return jsonify(dias_disponibles), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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

        query_tanda = text("""
            SELECT hora_ini, hora_fin 
            FROM idm_tr.idm_horarios_tc 
            WHERE esp_id = :espc_id AND sem_day = :dia
        """)
        tanda = db.session.execute(query_tanda, {"espc_id": especialista_id, "dia": dia_nombre}).mappings().first()

        # Horario base por defecto si no hay tanda específica
        hora_inicio = tanda['hora_ini'] if tanda else ""
        hora_fin = tanda['hora_fin'] if tanda else ""

        # 2. Consultar citas ya reservadas en RDS para evitar solapamientos

        #2.1 extraer todos los consultorios y la etiqueta de si es infantil o no
        query_consultorio= text("""select id, case when con_inf = true then '1' else '0' end from idm_tc.idm_consultorio_tc where id_suc=:id_suc""")
        
        #2.2 averiguar si el servicio que se da es infantil
        query_servicios=text("""select case when ser_inf = true then '1' else '0' end as inf from idm_tc.idm_servicio_tc
	        where id = :id_ser""")
       
        #2.3 total de consultorios :si el servicio no es infantil se trae la consulta como está, si no se agrega and con_inf is True
        query_totalCons= text("""select sum(id) from idm_tc.idm_consultorio_tc where id_suc=:id_suc""")
        
        #2.4 trae la suma de consultorios por hora.
        query_lleno = text("""
            SELECT cit_ini, sum(con_id) FROM idm_tr.idm_citas_tr 
            WHERE cast(cit_ini as date) =:fecha 
              AND cit_est != 'Cancelada'
              and con_id in :con_id
            group by = cit_ini
            having 2 = :tot_con
        """).bindparams(bindparam("con_id", expanding=True))

        #2.5 si el consultorio
        query_ocuinf = text("""
            SELECT cit_ini con_id FROM idm_tr.idm_citas_tr 
            WHERE cast(cit_ini as date) =:fecha 
              and con_id in :con_id 
              AND cit_est != 'Cancelada'
        """)
        
        query_ocupadas = text("""
            SELECT cit_ini FROM idm_tr.idm_citas_tr 
            WHERE esp_id = :espc_id
              AND cast(cit_ini as date) = :fecha 
              AND cit_est != 'Cancelada'
        """)


        consultorios = db.session.execute(query_consultorio, {"id_suc":ubicacion_id}).fetchall()
        con_id=[str(con[0]) for con in consultorios]
        #ser_inf = db.session.execute(query_lleno, {"fecha": fecha_str, "con_id":con_id}).fetchall()




        #citas_con = db.session.execute(query_lleno, {"fecha": fecha_str, "con_id":con_id}).fetchall()
        #print(citas_con)
        citas_existentes = db.session.execute(query_ocupadas, {"espc_id": especialista_id, "fecha": fecha_str}).fetchall()
        horas_ocupadas = [str(c[0])[11:16] for c in citas_existentes]
        print(horas_ocupadas)

        # 3. Generar slots de 1 hora
        fmt = "%H:%M:%S"
        h_ini = datetime.strptime(str(hora_inicio), fmt if len(str(hora_inicio)) == 8 else "%H:%M")
        h_fin = datetime.strptime(str(hora_fin), fmt if len(str(hora_fin)) == 8 else "%H:%M")

        slots_libres = []
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
                VALUES (:pac_id, :esp_id, :suc_id, :ser_id, :fecha, :hora, 'Pendiente_Pago')
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


# ==============================================================================
# 5. OBTENER INSTRUCCIONES DE PAGO PARA PANTALLA FINAL
# ==============================================================================

CUENTA_FISCAL = {
    "banco": "Santander",
    "titular": "Idealmente Salud Mental S.A. de C.V.",
    "clabe": "014225920012345678",      # CLABE para Facturación
    "numeroCuenta": "92001234567"
}

CUENTA_GENERAL = {
    "banco": "BBVA México",
    "titular": "Idealmente Servicios",
    "clabe": "012180009876543210",      # CLABE sin Factura
    "numeroCuenta": "0987654321"
}





CUENTA_FISCAL = {
    "banco": "BBVA",
    "titular": "Idealmente Servicios Médicos S.A. de C.V.",
    "clabe": "012180001234567890",
    "numeroCuenta": "0123456789"
}

CUENTA_GENERAL = {
    "banco": "Banorte",
    "titular": "Idealmente Salud",
    "clabe": "072180009876543210",
    "numeroCuenta": "9876543210"
}

@app.route('/api/instrucciones-pago', methods=['GET'])
def obtener_instrucciones_pago():
    try:
        servicio_id = request.args.get('servicio_id') or request.args.get('cita_id')
        requiere_factura_str = request.args.get('requiere_factura', 'false')
        requiere_factura = requiere_factura_str.lower() in ['true', '1']

        # Seleccionar la cuenta adecuada según requerimiento fiscal
        cuenta_seleccionada = CUENTA_FISCAL if requiere_factura else CUENTA_GENERAL

        monto = "600.00"

        # Si tenemos servicio_id, consultamos el monto real del servicio
        if servicio_id and servicio_id.isdigit():
            query_servicio = text("""
                SELECT ser_costo FROM idm_cat.idm_servicios_cat WHERE id = :servicio_id
            """)
            res_servicio = db.session.execute(query_servicio, {"servicio_id": int(servicio_id)}).mappings().first()
            if res_servicio and res_servicio.get('ser_costo'):
                monto = str(res_servicio['ser_costo'])

        # Generar concepto usando timestamp actual o referencia temporal
        timestamp_concepto = datetime.now().strftime('%m%d%H%M')
        ref_id = servicio_id if servicio_id else '001'
        concepto_generado = f"CITA-{ref_id}-{timestamp_concepto}"

        return jsonify({
            **cuenta_seleccionada,
            "concepto": concepto_generado,
            "monto": monto
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

        # ---------------------------------------------------------------------
        # 1. GENERAR ID DE PACIENTE E INSERTAR
        # ---------------------------------------------------------------------
        query_busqueda_paciente = text("""
            WITH pac AS (
                SELECT SUBSTRING(id, 2, 2) AS id_city, CAST(SUBSTRING(id, 4, 7) AS INT) AS id_num 
                FROM idm_tc.idm_paciente_tc
            ),
            city AS (
                SELECT SUBSTRING(id_cit, 3, 2) AS id_city 
                FROM idm_tc.idm_sucursal_tc
                WHERE id = :id_suc
            )
            SELECT pac.id_city AS city, COALESCE(MAX(id_num), 0) AS max_num 
            FROM pac
            INNER JOIN city ON pac.id_city = city.id_city
            GROUP BY pac.id_city
        """)

        idpac = db.session.execute(query_busqueda_paciente, {"id_suc": id_suc}).mappings().first()
        
        # Resguardo por si la consulta devuelve None
        cur_city = idpac["city"] if idpac else "01"
        max_num = idpac["max_num"] if idpac else 0

        new_id_num = str(max_num + 1)
        zeros_pac = 7 - len(new_id_num)
        id_pac = "I" + cur_city + ("0" * zeros_pac) + new_id_num

        query_in_pac = text("""
            INSERT INTO idm_tc.idm_paciente_tc
            (id, pac_name, pac_ap_pat, pac_ap_mat, pac_edad)
            VALUES (:id_pac, :pac_name, :pac_ap_pat, :pac_ap_mat, :pac_edad)
        """)

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

        # Cita Principal
        query_cita = text("""
            INSERT INTO idm_tr.idm_citas_tr 
            (id, esp_id, pac_id, con_id, cit_est, cit_ini, cit_fin, cit_age)
            VALUES (:id_cita, :esp_id, :pac_id, :con_id, 'Confirmada', :cit_ini, :cit_fin, NOW())
        """)
        params_cita = {
            "id_cita": id_cita,
            "esp_id": cita.get('especialista_id', '1'),
            "pac_id": id_pac,
            "con_id": id_suc,
            "cit_ini": cit_ini,
            "cit_fin": cit_fin
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

        # Confirmar la transacción completa
        db.session.commit()

        return jsonify({
            "exito": True,
            "mensaje": "Cita registrada correctamente en el sistema.",
            "cita_id": id_cita
        }), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Error al registrar la cita en BD: {str(e)}"}), 500

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
        INNER JOIN idm_tc.idm_sucursal_tc suc ON c.con_id = suc.id
        INNER JOIN idm_tc.idm_especialista_tc med ON c.esp_id = med.id
        INNER JOIN idm_tr.idm_ser_esp_tr es ON es.id_esp = med.id 
        INNER JOIN idm_tc.idm_servicio_tc srv ON es.id_ser = srv.id
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
app.config['MAIL_USERNAME'] = 'irondavidm@gmail.com'    # Tu correo remitente
app.config['MAIL_PASSWORD'] = 'Almaireri'   # Contraseña de aplicación
app.config['MAIL_DEFAULT_SENDER'] = ('Idealmente', 'irondavidm@gmail.com')

mail = Mail(app)
@app.route('/api/citas/enviar-email-confirmacion', methods=['POST'])
def enviar_email_confirmacion():
    data = request.get_json() or {}
    cita_id = data.get('cita_id')

    if not cita_id:
        return jsonify({'error': 'Falta el parámetro cita_id'}), 400

    # 1. Consultar la información de la cita y el correo del paciente/tutor
    query = text("""
        SELECT 
            c.id AS id,                
            -- Paciente Principal
            CONCAT(p.pac_name, ' ', p.pac_ap_pat, ' ', COALESCE(p.pac_ap_mat, '')) AS nombrePaciente,
            p.pac_edad AS pac_edad, dir_ema
            
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
        INNER JOIN idm_tc.idm_sucursal_tc suc ON c.con_id = suc.id
        INNER JOIN idm_tc.idm_especialista_tc med ON c.esp_id = med.id
        INNER JOIN idm_tr.idm_ser_esp_tr es ON es.id_esp = med.id 
        INNER JOIN idm_tc.idm_servicio_tc srv ON es.id_ser = srv.id
        INNER JOIN idm_tc.idm_especialidad_tc epd ON epd.id = srv.id_epd
        INNER JOIN idm_tc.idm_email_tc ema ON ema.id_pac = p.id
        WHERE c.id = :cita_id
    """)

    result = db.session.execute(query, {'cita_id': cita_id}).mappings().first()

    if not result:
        return jsonify({'error': 'No se encontró la cita especificada'}), 404

    cita = dict(result)
    correo_destino = cita.get('emailPaciente')

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

            <p>Estimado(a) <strong>{cita['nombrePaciente']}</strong>,</p>
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
                <span class="value badge">{cita['fechaCita']}</span>
            </div>
            <div class="detalle-row">
                <span class="label">Hora:</span>
                <span class="value badge">{cita['horaCita']} hrs</span>
            </div>
            <div class="detalle-row">
                <span class="label">Sucursal:</span>
                <span class="value">{cita['sucursal']}</span>
            </div>
            <div class="detalle-row">
                <span class="label">Dirección:</span>
                <span class="value">{cita['direccionSucursal']}</span>
            </div>

            <br>
            <p style="font-size: 13px; color: #475569;">
                Por favor, preséntate 10 minutos antes de la hora programada. Si deseas cancelar o reagendar, ponte en contacto con nosotros con anticipación.
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
        print(f"Error al enviar correo: {e}")
        return jsonify({'error': 'No se pudo enviar el correo de confirmación'}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)