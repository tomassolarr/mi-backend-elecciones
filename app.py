from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from flask_jwt_extended import (
    JWTManager, create_access_token, 
    jwt_required, get_jwt_identity
)
from flask import send_from_directory
from typing import Dict, List, Tuple, Optional
from rapidfuzz import fuzz, process
import pandas as pd
import json
import requests
import xml.etree.ElementTree as ET
import functools
import time
import os

app = Flask(__name__, static_folder=None)
app.config['JSON_AS_ASCII'] = False
app.config["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY", "clave_produccion_segura")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = 3600
jwt = JWTManager(app)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

if os.environ.get("FLASK_ENV") == "development":
    CORS(app, origins="*")
else:
    CORS(app, 
         resources={r"/api/*": {"origins": "*"}}, 
         supports_credentials=False)

USERS = {
    "jorge": "1234",
    "gato": "pomelo",
    "admin": "admin123",
    "usuario": "password"
}

CSV_URL = "https://www.emol.com/especiales/2025/nacional/elecciones/data/dip.csv"
API_JSON_URL = "https://static.emol.cl/emol50/especiales/js/2025/elecciones/dbres.json"
XML_BASE_URL = "https://www.emol.com/nacional/especiales/2025/presidenciales/dip_{}.xml"
FOTO_BASE_URL = "https://static.emol.cl/emol50/especiales/img/2025/elecciones/dip/{}.jpg"

COLUMNS = {
    'distrito': 1,
    'pacto_letra': 2,
    'partido': 3,
    'cupo': 4,
    'id': 5,
    'nombre': 6,
    'sexo': 9   
}

_cache = {
    'csv_data': None,
    'api_data': None,
    'config': None,
    'last_refresh': {}
}

@app.route("/api/login", methods=["POST"])
def login():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No se recibieron datos"}), 400
        
        username = data.get("user")
        password = data.get("password")
        
        if not username or not password:
            return jsonify({"error": "Usuario y contraseña requeridos"}), 400
        
        if username not in USERS or USERS[username] != password:
            return jsonify({"error": "Credenciales incorrectas"}), 401
        
        access_token = create_access_token(identity=username)
        
        return jsonify({
            "ok": True,
            "token": access_token,
            "user": username,
            "message": "Autenticación exitosa"
        })
    
    except Exception as e:
        return jsonify({"error": f"Error en autenticación: {str(e)}"}), 500

@app.route("/api/protected-test", methods=["GET"])
@jwt_required()
def protected_test():
    current_user = get_jwt_identity()
    return jsonify({
        "message": f"Acceso autorizado para usuario: {current_user}",
        "user": current_user,
        "timestamp": time.time()
    })

@app.route("/api/logout", methods=["POST"])
def logout():
    return jsonify({
        "ok": True,
        "message": "Sesión cerrada. Elimina el token del cliente."
    })

@app.route("/api/health", methods=["GET"])
def health_check():
    """Endpoint de verificación de salud para Render"""
    return jsonify({
        "status": "healthy",
        "service": "flask-api",
        "timestamp": time.time(),
        "environment": os.environ.get("FLASK_ENV", "production")
    })

def login_required(f):
    @functools.wraps(f)
    @jwt_required()
    def decorated_function(*args, **kwargs):
        current_user = get_jwt_identity()
        app.logger.info(f"Usuario {current_user} accediendo a {request.path}")
        return f(*args, **kwargs)
    return decorated_function

def cached(ttl=300):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            cache_key = f"{func.__name__}_{args}_{tuple(kwargs.items())}"
            
            if cache_key in _cache:
                data, timestamp = _cache[cache_key]
                if time.time() - timestamp < ttl:
                    return data
            
            result = func(*args, **kwargs)
            _cache[cache_key] = (result, time.time())
            return result
        return wrapper
    return decorator

def retry(max_attempts=3, delay=1):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts - 1:
                        raise
                    time.sleep(delay * (attempt + 1))
            return None
        return wrapper
    return decorator

@cached(ttl=600) 
def load_csv() -> pd.DataFrame:
    df = pd.read_csv(CSV_URL, header=None, encoding="utf-8")
    return df.where(pd.notnull(df), "")

@cached(ttl=300)  
def cargar_configuracion() -> Dict:
    with open("data_config.json", "r", encoding="utf-8") as f:
        return json.load(f)

@cached(ttl=60) 
@retry(max_attempts=3)
def cargar_api_completa() -> Dict:
    response = requests.get(API_JSON_URL, timeout=10)
    response.raise_for_status()
    return response.json().get("dbdp", {})

@cached(ttl=60)  
@retry(max_attempts=3)
def cargar_votos_xml(distrito: str) -> Tuple[Dict, int, int]:
    url = XML_BASE_URL.format(distrito)
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    
    root = ET.fromstring(response.content)
    votos_map = {}
    votos_blancos = 0
    votos_nulos = 0
    
    for row in root.findall('ROW'):
        ambito = row.find('AMBITO').text
        votos = int(row.find('VOTOS').text)
        
        if ambito == "B":
            votos_blancos = votos
        elif ambito == "N":
            votos_nulos = votos
        else:
            votos_map[ambito] = votos
    
    return votos_map, votos_blancos, votos_nulos

def get_candidatos_por_distrito(df: pd.DataFrame, distrito: str) -> Optional[List[Dict]]:
    df_distrito = df[df[COLUMNS['distrito']].astype(str) == distrito]
    
    if df_distrito.empty:
        return None
    
    return [{
        "id": str(row[COLUMNS['id']]),
        "name": str(row[COLUMNS['nombre']]),
        "party": str(row[COLUMNS['partido']]),
        "cupo": str(row[COLUMNS['cupo']]),
        "pacto_letra": str(row[COLUMNS['pacto_letra']]),
        "sexo": str(row[COLUMNS['sexo']]),
        "zona": str(row[0]),
        "id_foto": str(row[21]) if len(row) > 21 else ""
    } for _, row in df_distrito.iterrows()]

def preprocesar_nombre(nombre: str) -> str:
    if not isinstance(nombre, str):
        return ""
    
    nombre = nombre.lower().strip()
    reemplazos = {'á': 'a', 'é': 'e', 'í': 'i', 'ó': 'o', 'ú': 'u', 'ü': 'u', 'ñ': 'n'}
    for orig, reemp in reemplazos.items():
        nombre = nombre.replace(orig, reemp)
    
    palabras_remover = {'sr', 'sra', 'dr', 'dra', 'don', 'doña'}
    palabras = [p for p in nombre.split() if p not in palabras_remover]
    return ' '.join(palabras)

def hacer_match_por_nombre(nombre_api: str, candidatos_csv: List[Dict]) -> Tuple[Optional[Dict], float]:
    if not nombre_api or not candidatos_csv:
        return None, 0
    
    nombres_csv = [c["name"] for c in candidatos_csv]
    nombre_api_procesado = preprocesar_nombre(nombre_api)
    nombres_csv_procesados = [preprocesar_nombre(n) for n in nombres_csv]
    
    matches = process.extract(
        nombre_api_procesado, 
        nombres_csv_procesados, 
        scorer=fuzz.WRatio,
        score_cutoff=50,
        limit=1
    )
    
    if matches:
        nombre_match, score, idx = matches[0]
        return (candidatos_csv[idx], score) if score >= 80 else (None, score)
    
    return None, 0

def crear_mapeo_ids_xml_a_api(distrito: str, datos_api_completos: Dict) -> Dict:
    distrito_data = datos_api_completos.get(distrito, {})
    candidatos_ordenados = distrito_data.get("h", [])
    
    mapeo = {}
    letras = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    
    for i, candidato in enumerate(candidatos_ordenados):
        if i < len(letras):
            letra = letras[i]
            nombre_buscar = candidato["n"]
            
            candidatos_distrito = distrito_data.get("c", {})
            for api_id, datos in candidatos_distrito.items():
                if datos.get("n") == nombre_buscar:
                    mapeo[letra] = api_id
                    break
    
    return mapeo

def integrar_tres_fuentes_limpio(distrito: str) -> List[Dict]:
    df = load_csv()
    datos_api_completos = cargar_api_completa()
    votos_xml, _, _ = cargar_votos_xml(distrito)
    
    candidatos_csv = get_candidatos_por_distrito(df, distrito)
    distrito_data = datos_api_completos.get(distrito, {})
    candidatos_api = distrito_data.get("c", {})
    mapeo_ids = crear_mapeo_ids_xml_a_api(distrito, datos_api_completos)
    
    if not (candidatos_csv and candidatos_api and votos_xml):
        return []
    
    candidatos_completos = []
    config = cargar_configuracion()
    pactos_nombre = config.get("pactos_nombre", {})
    
    for xml_id, votos in votos_xml.items():
        api_id = mapeo_ids.get(xml_id) if xml_id in mapeo_ids else (xml_id if xml_id in candidatos_api else None)
        
        if not api_id or api_id not in candidatos_api:
            continue
        
        datos_candidato = candidatos_api[api_id]
        nombre_api = datos_candidato.get("n", "")
        candidato_csv, score_similitud = hacer_match_por_nombre(nombre_api, candidatos_csv)
        
        if candidato_csv and score_similitud >= 80:
            id_foto = candidato_csv.get("id_foto", "")
            foto_url = FOTO_BASE_URL.format(id_foto) if id_foto else None
            pacto_letra = candidato_csv["pacto_letra"]
            match_exitoso = True
        else:
            foto_url = None
            pacto_letra = "X"
            match_exitoso = False
        
        candidatos_completos.append({
            "id_api": api_id,
            "votos": votos,
            "nombre": nombre_api,
            "partido": datos_candidato.get("c", ""),
            "pacto_letra": pacto_letra,
            "pacto_nombre": pactos_nombre.get(pacto_letra, f"Pacto {pacto_letra}"),
            "sexo": datos_candidato.get("s", ""),
            "cupo": candidato_csv["cupo"] if candidato_csv else "",
            "foto": foto_url,
            "zona": candidato_csv.get("zona", "") if candidato_csv else "",
            "match_quality": score_similitud,
            "match_exitoso": match_exitoso
        })
    
    return sorted(candidatos_completos, key=lambda x: x["votos"], reverse=True)

def aplicar_dhondt_entre_pactos(pactos: Dict, escanos: int) -> Tuple[Dict, List]:
    coeficientes = []
    
    for letra, info in pactos.items():
        total_votos = info["total_votos"]
        
        for divisor in range(1, escanos + 1):
            coeficientes.append({
                "pacto_letra": letra,
                "pacto_nombre": info["nombre"],
                "divisor": divisor,
                "coeficiente": total_votos / divisor,
                "votos_originales": total_votos
            })
    
    coeficientes.sort(key=lambda x: x["coeficiente"], reverse=True)
    mejores_coeficientes = coeficientes[:escanos]
    
    asignacion = {}
    for coef in mejores_coeficientes:
        letra = coef["pacto_letra"]
        asignacion[letra] = asignacion.get(letra, 0) + 1
    
    return asignacion, mejores_coeficientes

def calcular_dhondt_interno_pacto(pacto_info: Dict, escanos_pacto: int) -> Tuple[Dict, List]:
    partidos = {}
    
    for cand in pacto_info["candidatos"]:
        partido = cand.get("cupo", "") or "SIN_CUPO"
        if partido not in partidos:
            partidos[partido] = {"total_votos": 0.0, "candidatos": []}
        
        partidos[partido]["total_votos"] += cand["votos"]
        partidos[partido]["candidatos"].append(cand)
    
    for partido in partidos.values():
        partido["candidatos"].sort(key=lambda x: x["votos"], reverse=True)
    
    partidos_list = [
        {
            "partido": p,
            "total_votos": datos["total_votos"],
            "candidatos": datos["candidatos"],
            "candidatos_disponibles": len(datos["candidatos"])
        }
        for p, datos in partidos.items()
    ]
    partidos_list.sort(key=lambda x: x["total_votos"], reverse=True)
    
    coeficientes = []
    for partido in partidos_list:
        for d in range(1, escanos_pacto + 1):
            coeficientes.append({
                "partido": partido["partido"],
                "division": d,
                "valor": partido["total_votos"] / d,
                "candidatos_disponibles": partido["candidatos_disponibles"]
            })
    
    coeficientes.sort(key=lambda x: x["valor"], reverse=True)
    
    asignacion_final = {}
    escanos_asignados = 0
    
    for coef in coeficientes:
        if escanos_asignados >= escanos_pacto:
            break
            
        partido = coef["partido"]
        if asignacion_final.get(partido, 0) < coef["candidatos_disponibles"]:
            asignacion_final[partido] = asignacion_final.get(partido, 0) + 1
            escanos_asignados += 1
    
    return asignacion_final, partidos_list

def calcular_dhondt_distrito(distrito, mode="normal"):
    try:
        if mode != "normal":
            return calcular_dhondt_distrito_simulado(distrito, mode)
        config = cargar_configuracion()
        escanos = config["escanos"].get(distrito)
        pactos_nombre = config["pactos_nombre"]
        VALOR_UF = config.get("valor_uf", 500)
        
        if not escanos:
            return None
        
        candidatos = integrar_tres_fuentes_limpio(distrito)
        
        pactos = {}
        for candidato in candidatos:
            if not candidato.get("match_exitoso", True):
                continue
                
            letra = candidato["pacto_letra"]
            votos = candidato["votos"]
            
            if letra not in pactos:
                pactos[letra] = {
                    "nombre": pactos_nombre.get(letra, f"Pacto {letra}"),
                    "letra": letra,
                    "total_votos": 0.0,
                    "candidatos_completos": []  
                }
            
            pactos[letra]["total_votos"] += votos
            pactos[letra]["candidatos_completos"].append(candidato)
        
        asignacion, _ = aplicar_dhondt_entre_pactos(pactos, escanos)
    
        pactos_result = []
        total_mujeres_electas = 0
        total_bonificacion = 0

        for letra, info in pactos.items():
            pacto_seats = asignacion.get(letra, 0)
            
            if pacto_seats > 0:
                partidos_info = {}
                for candidato in info["candidatos_completos"]:
                    partido = candidato["partido"]
                    if partido not in partidos_info:
                        partidos_info[partido] = {
                            "total_votos": 0.0,
                            "candidatos": []
                        }
                    partidos_info[partido]["total_votos"] += candidato["votos"]
                    partidos_info[partido]["candidatos"].append({
                        "nombre": candidato["nombre"],
                        "votos": candidato["votos"],
                        "cupo": candidato["cupo"],
                        "sexo": candidato["sexo"]
                    })
                
                for partido_data in partidos_info.values():
                    partido_data["candidatos"].sort(key=lambda x: x["votos"], reverse=True)
                
                pacto_info_para_dhondt = {
                    "candidatos": [
                        {
                            "nombre": cand["nombre"],
                            "votos": cand["votos"],
                            "cupo": cand["cupo"],
                            "partido": partido,
                            "sexo": cand["sexo"]
                        }
                        for partido, datos in partidos_info.items()
                        for cand in datos["candidatos"]
                    ]
                }
                asignacion_partidos, _ = calcular_dhondt_interno_pacto(pacto_info_para_dhondt, pacto_seats)
                candidatos_electos = []
                mujeres_pacto = 0
                
                for partido, datos in partidos_info.items():
                    escaños_partido = asignacion_partidos.get(partido, 0)
                    
                    if escaños_partido > 0:
                        for i in range(min(escaños_partido, len(datos["candidatos"]))):
                            candidato_elegido = datos["candidatos"][i]
                            candidato_completo = next(
                                (c for c in info["candidatos_completos"] 
                                 if c["nombre"] == candidato_elegido["nombre"]),
                                None
                            )
                            
                            if candidato_completo:
                                candidatos_electos.append({
                                    "nombre": candidato_completo["nombre"],
                                    "cupo": candidato_completo["cupo"],
                                    "partido": candidato_completo["partido"],
                                    "votos": candidato_completo["votos"],
                                    "sexo": candidato_completo["sexo"],
                                    "foto": candidato_completo.get("foto"),  
                                    "distrito": distrito  
                                })
                                
                                if candidato_completo["sexo"] == "M":
                                    mujeres_pacto += 1
                
                candidatos_electos.sort(key=lambda x: x["votos"], reverse=True)
                
                bonificacion_pacto = mujeres_pacto * VALOR_UF
                total_mujeres_electas += mujeres_pacto
                total_bonificacion += bonificacion_pacto
                
                pactos_result.append({
                    "nombre": info["nombre"],
                    "letra": letra,
                    "total_votos": round(info["total_votos"], 4),
                    "escanos": pacto_seats,
                    "candidatos_electos": candidatos_electos,
                    "mujeres_electas": mujeres_pacto,
                    "bonificacion": bonificacion_pacto
                })
            else:
                pactos_result.append({
                    "nombre": info["nombre"],
                    "letra": letra,
                    "total_votos": round(info["total_votos"], 4),
                    "escanos": 0,
                    "candidatos_electos": [],
                    "mujeres_electas": 0,
                    "bonificacion": 0
                })
        
        pactos_result.sort(key=lambda p: (p["escanos"], p["total_votos"]), reverse=True)
        
        return {
            "distrito": distrito,
            "escanos": escanos,
            "pactos": pactos_result,
            "total_diputados": sum(pacto["escanos"] for pacto in pactos_result),
            "resumen_mujeres": {
                "total_mujeres_electas": total_mujeres_electas,
                "total_bonificacion": total_bonificacion,
                "valor_uf": VALOR_UF,
                "porcentaje_mujeres": round((total_mujeres_electas / escanos * 100), 2) if escanos > 0 else 0
            }
        }
        
    except Exception as e:
        return None

def fusionar_pactos_en_candidatos(candidatos, mode):
    
    if mode == "normal":
        return candidatos
    
    candidatos_fusionados = []
    
    if mode == "derechas":
    
        for candidato in candidatos:
            cand_copy = candidato.copy()
            if cand_copy["pacto_letra"] in ["J", "K"]:
                cand_copy["pacto_letra"] = "JK"
                cand_copy["pacto_nombre"] = "Derechas Unidas (J+K)"
            candidatos_fusionados.append(cand_copy)
    
    elif mode == "izquierdas":
        izquierda_letras = ["A", "B", "C", "D", "F", "G", "H"]
        for candidato in candidatos:
            cand_copy = candidato.copy()
            if cand_copy["pacto_letra"] in izquierda_letras:
                cand_copy["pacto_letra"] = "IZQ"
                cand_copy["pacto_nombre"] = "Izquierdas Unidas (A+B+C+D+F+G+H)"
            candidatos_fusionados.append(cand_copy)
    
    return candidatos_fusionados

def calcular_dhondt_distrito_simulado(distrito, mode):
    try:
        config = cargar_configuracion()
        escanos = config["escanos"].get(distrito)
        pactos_nombre = config["pactos_nombre"]
        VALOR_UF = config.get("valor_uf", 500)
        
        if mode == "derechas":
            pactos_nombre = pactos_nombre.copy()
            pactos_nombre["JK"] = "Derechas Unidas (J+K)"
            if "J" in pactos_nombre:
                del pactos_nombre["J"]
            if "K" in pactos_nombre:
                del pactos_nombre["K"]
        elif mode == "izquierdas":
            pactos_nombre = pactos_nombre.copy()
            pactos_nombre["IZQ"] = "Izquierdas Unidas (A+B+C+D+F+G+H)"
            for letra in ["A", "B", "C", "D", "F", "G", "H"]:
                if letra in pactos_nombre:
                    del pactos_nombre[letra]
        
        if not escanos:
            return None
        
        candidatos = integrar_tres_fuentes_limpio(distrito)
        candidatos_fusionados = fusionar_pactos_en_candidatos(candidatos, mode)
        
        pactos = {}
        for candidato in candidatos_fusionados:
            if not candidato.get("match_exitoso", True):
                continue
                
            letra = candidato["pacto_letra"]
            votos = candidato["votos"]
            
            if letra not in pactos:
                pactos[letra] = {
                    "nombre": pactos_nombre.get(letra, f"Pacto {letra}"),
                    "letra": letra,
                    "total_votos": 0.0,
                    "candidatos_completos": []
                }
            
            pactos[letra]["total_votos"] += votos
            pactos[letra]["candidatos_completos"].append(candidato)
        
        asignacion, _ = aplicar_dhondt_entre_pactos(pactos, escanos)

        pactos_result = []
        total_mujeres_electas = 0
        total_bonificacion = 0

        for letra, info in pactos.items():
            pacto_seats = asignacion.get(letra, 0)
            
            if pacto_seats > 0:
                partidos_info = {}
                for candidato in info["candidatos_completos"]:
                    partido = candidato["partido"]
                    if partido not in partidos_info:
                        partidos_info[partido] = {
                            "total_votos": 0.0,
                            "candidatos": []
                        }
                    partidos_info[partido]["total_votos"] += candidato["votos"]
                    partidos_info[partido]["candidatos"].append({
                        "nombre": candidato["nombre"],
                        "votos": candidato["votos"],
                        "cupo": candidato["cupo"],
                        "sexo": candidato["sexo"]
                    })
                for partido_data in partidos_info.values():
                    partido_data["candidatos"].sort(key=lambda x: x["votos"], reverse=True)
                
                pacto_info_para_dhondt = {
                    "candidatos": [
                        {
                            "nombre": cand["nombre"],
                            "votos": cand["votos"],
                            "cupo": cand["cupo"],
                            "partido": partido,
                            "sexo": cand["sexo"]
                        }
                        for partido, datos in partidos_info.items()
                        for cand in datos["candidatos"]
                    ]
                }
                
                asignacion_partidos, _ = calcular_dhondt_interno_pacto(pacto_info_para_dhondt, pacto_seats)
                
                candidatos_electos = []
                mujeres_pacto = 0
                for partido, datos in partidos_info.items():
                    escaños_partido = asignacion_partidos.get(partido, 0)
                    
                    if escaños_partido > 0:
                        for i in range(min(escaños_partido, len(datos["candidatos"]))):
                            candidato_elegido = datos["candidatos"][i]
                            candidato_completo = next(
                                (c for c in info["candidatos_completos"] 
                                 if c["nombre"] == candidato_elegido["nombre"]),
                                None
                            )
                            
                            if candidato_completo:
                                candidatos_electos.append({
                                    "nombre": candidato_completo["nombre"],
                                    "cupo": candidato_completo["cupo"],
                                    "partido": candidato_completo["partido"],
                                    "votos": candidato_completo["votos"],
                                    "sexo": candidato_completo["sexo"],
                                    "foto": candidato_completo.get("foto"),
                                    "distrito": distrito
                                })
                                
                                if candidato_completo["sexo"] == "M":
                                    mujeres_pacto += 1
                
                candidatos_electos.sort(key=lambda x: x["votos"], reverse=True)
                
                bonificacion_pacto = mujeres_pacto * VALOR_UF
                total_mujeres_electas += mujeres_pacto
                total_bonificacion += bonificacion_pacto
                
                pactos_result.append({
                    "nombre": info["nombre"],
                    "letra": letra,
                    "total_votos": round(info["total_votos"], 4),
                    "escanos": pacto_seats,
                    "candidatos_electos": candidatos_electos,
                    "mujeres_electas": mujeres_pacto,
                    "bonificacion": bonificacion_pacto
                })
            else:
                pactos_result.append({
                    "nombre": info["nombre"],
                    "letra": letra,
                    "total_votos": round(info["total_votos"], 4),
                    "escanos": 0,
                    "candidatos_electos": [],
                    "mujeres_electas": 0,
                    "bonificacion": 0
                })
        
        pactos_result.sort(key=lambda p: (p["escanos"], p["total_votos"]), reverse=True)
        
        return {
            "distrito": distrito,
            "escanos": escanos,
            "pactos": pactos_result,
            "total_diputados": sum(pacto["escanos"] for pacto in pactos_result),
            "resumen_mujeres": {
                "total_mujeres_electas": total_mujeres_electas,
                "total_bonificacion": total_bonificacion,
                "valor_uf": VALOR_UF,
                "porcentaje_mujeres": round((total_mujeres_electas / escanos * 100), 2) if escanos > 0 else 0
            }
        }
        
    except Exception as e:
        return None

@app.route("/")
def home():
    return jsonify({
        "status": "active", 
        "version": "1.0",
        "service": "Backend API Flask",
        "endpoints": {
            "auth": ["/api/login", "/api/logout", "/api/protected-test"],
            "data": ["/api/candidatos-limpios", "/api/votos-por-pacto", "/api/dhondt-actual", "/api/hemiciclo-nacional"],
            "health": "/api/health"
        }
    })

@app.route("/candidatos-limpios", methods=["GET"])
@login_required
def candidatos_limpios():
    distrito = request.args.get("distrito")
    if not distrito:
        return jsonify({"error": "Falta el parámetro 'distrito'"}), 400
    
    try:
        candidatos = integrar_tres_fuentes_limpio(distrito)
        return jsonify({
            "distrito": distrito,
            "total_candidatos": len(candidatos),
            "candidatos": candidatos,
            "request_by": get_jwt_identity()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/votos-por-pacto", methods=["GET"])
@login_required
def votos_por_pacto():
    distrito = request.args.get("distrito")
    if not distrito:
        return jsonify({"error": "Falta el parámetro 'distrito'"}), 400
    
    try:
        candidatos = integrar_tres_fuentes_limpio(distrito)
        votos_xml, votos_blancos, votos_nulos = cargar_votos_xml(distrito)
        config = cargar_configuracion()
        escanos_distrito = config.get("escanos", {}).get(distrito, 0)
        
        pactos = {}
        total_votos_pactos = 0
        
        for candidato in candidatos:
            if not candidato.get("match_exitoso", True):
                continue
                
            pacto_letra = candidato["pacto_letra"]
            votos_reales = votos_xml.get(candidato.get("id_api", ""), 0)
            
            if votos_reales > 0:
                if pacto_letra not in pactos:
                    pactos[pacto_letra] = {
                        "pacto_letra": pacto_letra,
                        "pacto_nombre": candidato["pacto_nombre"],
                        "votos_totales": 0,
                        "candidatos": [],
                        "partidos": set()
                    }
                
                pactos[pacto_letra]["votos_totales"] += votos_reales
                pactos[pacto_letra]["candidatos"].append({
                    "nombre": candidato["nombre"],
                    "votos": votos_reales,
                    "partido": candidato["partido"]
                })
                pactos[pacto_letra]["partidos"].add(candidato["partido"])
                total_votos_pactos += votos_reales
        
        resultado_pactos = []
        for letra, datos in pactos.items():
            porcentaje = (datos["votos_totales"] / total_votos_pactos * 100) if total_votos_pactos > 0 else 0
            resultado_pactos.append({
                "pacto_letra": letra,
                "pacto_nombre": datos["pacto_nombre"],
                "votos_totales": datos["votos_totales"],
                "porcentaje_total": round(porcentaje, 2),
                "cantidad_candidatos": len(datos["candidatos"]),
                "cantidad_partidos": len(datos["partidos"]),
                "partidos": list(datos["partidos"])
            })
        
        resultado_pactos.sort(key=lambda x: x["porcentaje_total"], reverse=True)
        
        return jsonify({
            "distrito": distrito,
            "escanos_disponibles": escanos_distrito,
            "total_votos_validos": total_votos_pactos,
            "total_votos_general": total_votos_pactos + votos_blancos + votos_nulos,
            "votos_blancos": votos_blancos,
            "votos_nulos": votos_nulos,
            "pactos": resultado_pactos,
            "request_by": get_jwt_identity()
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/dhondt-actual", methods=["GET"])
@jwt_required()
def dhondt_actual():
    distrito = request.args.get("distrito")
    mode = request.args.get("mode", "normal") 
    
    if not distrito:
        return jsonify({"error": "Falta el parámetro 'distrito'"}), 400
    
    resultado = calcular_dhondt_distrito(distrito, mode) 
    if not resultado:
        return jsonify({"error": f"No se pudo calcular D'Hondt para {distrito}"}), 404
    
    resultado["requested_by"] = get_jwt_identity()
    resultado["timestamp"] = time.time()
    
    return jsonify(resultado)

@app.route("/hemiciclo-nacional", methods=["GET"])
@login_required
def hemiciclo_nacional():
    try:
        mode = request.args.get("mode", "normal")
        config = cargar_configuracion()
        pactos_nombre = config["pactos_nombre"]
        VALOR_UF = config.get("valor_uf", 500)
        resultados_por_distrito_normal = []
        resultados_por_distrito_simulado = []
        
        todos_electos = []
        estadisticas = {
            "total_escanos": 0,
            "total_mujeres": 0,
            "pactos": {},
            "distritos_procesados": 0,
            "distritos_por_pacto": {}  
        }
        
        for distrito_num in range(1, 29):
            distrito_id = f"60{distrito_num:02d}"
            
            try:
                resultado_normal = calcular_dhondt_distrito(distrito_id)
                if not resultado_normal:
                    continue

                resultado_simulado = None
                if mode != "normal":
                    resultado_simulado = calcular_dhondt_distrito_simulado(distrito_id, mode)
                
                resultado_a_usar = resultado_simulado if (mode != "normal" and resultado_simulado) else resultado_normal
                
                if resultado_normal:
                    resultados_por_distrito_normal.append(resultado_normal)
                if resultado_simulado:
                    resultados_por_distrito_simulado.append(resultado_simulado)
                
                estadisticas["total_escanos"] += resultado_a_usar["escanos"]
                estadisticas["total_mujeres"] += resultado_a_usar["resumen_mujeres"]["total_mujeres_electas"]
                estadisticas["distritos_procesados"] += 1
                
                for pacto in resultado_a_usar["pactos"]:
                    letra = pacto["letra"]
                    
                    if letra not in estadisticas["pactos"]:
                        estadisticas["pactos"][letra] = {
                            "nombre": pacto["nombre"],
                            "escanos_totales": 0,
                            "mujeres_totales": 0,
                            "bonificacion_total": 0,
                            "candidatos_electos": []
                        }
                    
                    if pacto["escanos"] > 0:
                        if letra not in estadisticas["distritos_por_pacto"]:
                            estadisticas["distritos_por_pacto"][letra] = set()
                        estadisticas["distritos_por_pacto"][letra].add(distrito_id)
                    
                    estadisticas["pactos"][letra]["escanos_totales"] += pacto["escanos"]
                    estadisticas["pactos"][letra]["mujeres_totales"] += pacto["mujeres_electas"]
                    estadisticas["pactos"][letra]["bonificacion_total"] += pacto["bonificacion"]
                    
                    for candidato in pacto["candidatos_electos"]:
                        candidato_con_distrito = {
                            **candidato,
                            "distrito": distrito_id,
                            "distrito_numero": distrito_num,
                            "pacto_nombre": pacto["nombre"],
                            "pacto_letra": letra,
                            "foto": candidato.get("foto")
                        }
                        todos_electos.append(candidato_con_distrito)
                        estadisticas["pactos"][letra]["candidatos_electos"].append(candidato_con_distrito)
                        
            except Exception as e:
                continue
        
        todos_electos.sort(key=lambda x: x["votos"], reverse=True)
        
        pactos_final = []
        for letra, datos in estadisticas["pactos"].items():
            total_escanos = estadisticas["total_escanos"]
            porcentaje_nacional = round((datos["escanos_totales"] / total_escanos * 100), 2) if total_escanos > 0 else 0
            
            pactos_final.append({
                "letra": letra,
                "nombre": datos["nombre"],
                "escanos_totales": datos["escanos_totales"],
                "mujeres_totales": datos["mujeres_totales"],
                "bonificacion_total": datos["bonificacion_total"],
                "distritos_ganados": len(estadisticas["distritos_por_pacto"].get(letra, set())),
                "porcentaje_nacional": porcentaje_nacional,
                "candidatos_electos": datos["candidatos_electos"]
            })
        
        pactos_final.sort(key=lambda x: x["escanos_totales"], reverse=True)
 
        resultado_final = {
            "success": True,
            "mode": mode,
            "total_distritos": 28,
            "distritos_procesados": estadisticas["distritos_procesados"],
            "distritos_error": 28 - estadisticas["distritos_procesados"],
            "estadisticas_nacionales": {
                "total_escanos": estadisticas["total_escanos"],
                "total_diputados": len(todos_electos),
                "total_mujeres": estadisticas["total_mujeres"],
                "porcentaje_mujeres": round((estadisticas["total_mujeres"] / estadisticas["total_escanos"] * 100), 2) if estadisticas["total_escanos"] > 0 else 0,
            },
            "pactos_nacionales": pactos_final,
            "diputados_electos": todos_electos,
            "requested_by": get_jwt_identity(),
            "audit_timestamp": time.time()
        }
        return jsonify(resultado_final)
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": f"Error calculando hemiciclo nacional: {str(e)}"
        }), 500

if __name__ == "__main__":
    # Configuración para producción
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_ENV", "production") == "development"
    
    # Configurar logging
    import logging
    logging.basicConfig(level=logging.INFO)
    
    app.run(debug=debug_mode, host="0.0.0.0", port=port)