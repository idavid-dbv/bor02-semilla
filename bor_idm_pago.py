
@app.route('/api/citas/instrucciones-pago', methods=['GET'])
def obtener_instrucciones_pago():
    cita_id = request.args.get('cita_id')

    try:
        # Consulta de datos de la cita y la cuenta asignada
        #query = text("""
        #    SELECT 
        #        c.fecha_cita,
        #        c.hora_cita,
        #        cb.banco,
        #        cb.titular,
        #        cb.clabe,
        #        cb.numero_cuenta,
        #        s.precio
        #    FROM citas c
        #    JOIN cuentas_bancarias cb ON c.cuenta_asignada_id = cb.id
        #    JOIN servicios s ON c.servicio_id = s.id
        #    WHERE c.id = :cita_id
        #""")
        query = text("""
            SELECT 
                2026-08-22,
                12:00,
                santander,
                Mar Mejía,
                XXX-XXXXX-XXXXX-XXXXXX,
                XXXX-XXX-XXXX,
                600
            
            """)
        
        
        resultado = db.session.execute(query, {"cita_id": cita_id}).mappings().first()

        if not resultado:
            return jsonify({"error": "Cita no encontrada"}), 404

        # Formatear fecha (AAMMDD) y hora (HHMM)
        # Ejemplo: 2026-08-25 -> 260825 | 09:30 -> 0930
        fecha_dt = datetime.strptime(str(resultado['fecha_cita']), '%Y-%m-%d')
        hora_str = str(resultado['hora_cita']).replace(':', '')[:4]
        
        fecha_formateada = fecha_dt.strftime('%y%m%d')
        
        # Generar concepto único: Ej. CITA102-260825-0930
        concepto_generado = f"CITA{cita_id}-{fecha_formateada}-{hora_str}"

        return jsonify({
            "banco": resultado['banco'],
            "titular": resultado['titular'],
            "clabe": resultado['clabe'],
            "numeroCuenta": resultado['numero_cuenta'],
            "concepto": concepto_generado,
            "monto": float(resultado['precio'])
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500