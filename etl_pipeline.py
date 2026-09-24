import os
import json
import time 
import feedparser
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# ==========================================
# 1. CONFIGURACIÓN Y CLIENTES
# ==========================================
load_dotenv()

# Cliente usando el NUEVO SDK oficial de Google
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# Conexión nativa a PostgreSQL (Supabase)
def obtener_conexion_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

# ==========================================
# 2. CONTRATO DE DATOS (STRUCTURED OUTPUT)
# ==========================================
class AnalisisSociologico(BaseModel):
    categoria_sociologica: str = Field(description="Clasificación del tema (Ej: Política, Economía, Seguridad, Movimientos Sociales).")
    sentimiento_titular: str = Field(description="Tono general (Positivo, Negativo, Neutro).")
    polarizacion: str = Field(description="Nivel de polarización inducida (Alta, Media, Baja).")
    actores_involucrados: str = Field(description="Lista separada por comas de los actores mencionados o implícitos.")

# ==========================================
# 3. FUNCIONES DEL PIPELINE (ETL)
# ==========================================
def extraer_rss(fuentes_rss: list) -> list:
    """Extrae noticias camuflando el script y estandarizando las fechas."""
    feedparser.USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    
    noticias_crudas = []
    for fuente in fuentes_rss:
        feed = feedparser.parse(fuente['url'])
        
        if hasattr(feed, 'status') and feed.status not in [200, 301, 302, 308]:
            print(f"⚠️ Alerta: Status HTTP {feed.status} al intentar leer {fuente['nombre']}")
            
        for entry in feed.entries:
            # Estandarización de fecha a YYYY-MM-DD HH:MM:SS
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                fecha_limpia = time.strftime('%Y-%m-%d %H:%M:%S', entry.published_parsed)
            else:
                fecha_limpia = None # Evita fallos si un medio no publica la fecha
                
            noticias_crudas.append({
                "medio": fuente['nombre'],
                "titular": entry.title,
                "enlace": entry.link,
                "fecha_publicacion": fecha_limpia
            })
            
    print(f"-> Se extrajeron {len(noticias_crudas)} noticias en total desde los RSS.")
    return noticias_crudas

def deduplicar_batch(noticias: list) -> list:
    """Filtra los enlaces que ya existen en PostgreSQL (Batch SELECT)."""
    if not noticias:
        return []
    
    enlaces_extraidos = [n['enlace'] for n in noticias]
    
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    query = "SELECT enlace FROM discurso_prensa WHERE enlace = ANY(%s);"
    cursor.execute(query, (enlaces_extraidos,))
    resultados = cursor.fetchall()
    
    enlaces_existentes = {fila[0] for fila in resultados}
    cursor.close()
    conn.close()
    
    noticias_nuevas = [n for n in noticias if n['enlace'] not in enlaces_existentes]
    print(f"-> [{len(noticias)}] extraídas totales -> [{len(noticias_nuevas)}] son nuevas para la BD.")
    return noticias_nuevas

def procesar_nlp_gemini(noticia: dict) -> dict:
    """Llama a Gemini usando el nuevo SDK."""
    prompt = f"""
Actúa como un sociólogo experto en análisis de discurso y agenda setting.
Analiza el siguiente titular y enlace de una noticia:
Titular: {item['titular']}
Enlace: {item['enlace']}

Debes clasificar la noticia ESTRICTAMENTE en UNA de las siguientes categorías exactas (no inventes nuevas, elige la que mejor encaje):
- Política Nacional
- Economía y Negocios
- Seguridad y Orden Público
- Movimientos Sociales
- Derechos Humanos
- Política Internacional
- Cultura y Espectáculos
- Deportes
- Salud Pública
- Educación
- Ciencia y Tecnología
- Medio Ambiente
- Infraestructura y Transporte
- Justicia y Tribunales
- Medios de Comunicación
- Política Social y Pensiones
- Política Migratoria
- Misceláneo / Otro

Devuelve un JSON válido con estas claves:
"categoria_sociologica" (usa solo la lista anterior), "sentimiento_titular" (Positivo/Negativo/Neutro), "polarizacion" (Alta/Media/Baja).
"""
    try:
        respuesta = client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AnalisisSociologico,
                temperature=0.2 
            ),
        )
        analisis = json.loads(respuesta.text)
        # Esperar 5 segundos para no saturar la capa gratuita de la API
        time.sleep(5)
        return {**noticia, **analisis}
    except Exception as e:
        print(f"Error procesando NLP para {noticia['enlace']}: {e}")
        # Esperar 5 segundos para no saturar la capa gratuita de la API
        time.sleep(5)
        return None

def cargar_en_supabase(datos_procesados: list):
    """Inserta el lote de datos analizados en PostgreSQL."""
    if not datos_procesados:
        print("No hay datos nuevos para insertar.")
        return
    
    conn = obtener_conexion_db()
    cursor = conn.cursor()
    
    query = """
        INSERT INTO discurso_prensa (
            fecha_publicacion, medio, titular, enlace, 
            categoria_sociologica, sentimiento_titular, 
            polarizacion, actores_involucrados
        ) VALUES %s
        ON CONFLICT (enlace) DO NOTHING;
    """
    
    valores = [
        (
            d['fecha_publicacion'], d['medio'], d['titular'], d['enlace'],
            d['categoria_sociologica'], d['sentimiento_titular'], 
            d['polarizacion'], d['actores_involucrados']
        )
        for d in datos_procesados
    ]
    
    execute_values(cursor, query, valores)
    conn.commit()
    cursor.close()
    conn.close()
    print(f"Se insertaron {len(datos_procesados)} registros exitosamente en la BD.")

# ==========================================
# 4. ORQUESTADOR PRINCIPAL (PRODUCCIÓN)
# ==========================================
if __name__ == "__main__":
    FUENTES = [
        {"nombre": "Radio UChile", "url": "https://radio.uchile.cl/feed/"},
        {"nombre": "CIPER Chile", "url": "https://www.ciperchile.cl/feed/"},
        {"nombre": "The Clinic", "url": "https://www.theclinic.cl/feed/"}
    ]
    
    print("=== INICIANDO PIPELINE DE OBSERVATORIO (PRODUCCIÓN) ===")
    
    # 1. Extract
    raw_data = extraer_rss(FUENTES)
    
    # 2. Transform - Deduplicación
    datos_nuevos = deduplicar_batch(raw_data)
    
    if not datos_nuevos:
        print("No hay noticias nuevas para procesar. Pipeline finalizado.")
    else:
        # 3. Transform - NLP con Logs de progreso
        datos_finales = []
        for i, item in enumerate(datos_nuevos):
            print(f"Procesando NLP ({i+1}/{len(datos_nuevos)}): {item['medio']}...")
            resultado = procesar_nlp_gemini(item)
            if resultado:
                datos_finales.append(resultado)
                
        # 4. Load
        print("\nIniciando carga en base de datos...")
        cargar_en_supabase(datos_finales)
        
        print("\n=== PIPELINE FINALIZADO CON ÉXITO ===")
        print(f"Total procesado e insertado hoy: {len(datos_finales)} noticias.")