# -*- coding: utf-8 -*-
"""
agente_soporte.py
Agente de Soporte al Cliente - Unidad 2
Desarrollo de Agentes Inteligentes (ACD-2504)
Ricardo Nuñez Hernandez - 23070505 - TecNM campus Ciudad Madero

Flujo: Google Forms -> Google Sheets -> Python -> Gemini -> Google Sheets

El agente lee las quejas nuevas de la hoja, le pide a Gemini que las
clasifique, valida la respuesta EN CODIGO y escribe el resultado de
vuelta en las columnas D, E y F.
"""

import json
import logging
import os
import re
import sys
import time
import unicodedata

import gspread
import schedule
from google import genai
from google.genai import types

# python-dotenv es opcional: si no esta instalado, el script sigue
# funcionando leyendo la variable de entorno del sistema.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs):
        return False

# Silencia el aviso informativo del SDK sobre "automatic function calling".
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


# =====================================================================
# CONFIGURACION
# =====================================================================

ARCHIVO_CREDENCIALES = "client_secrets.json"
ARCHIVO_TOKEN = "token_usuario.json"
NOMBRE_HOJA = "Respuestas de Quejas (Base de Datos)"

# El PDF pedia gemini-2.5-flash, pero Google lo retiro para cuentas
# nuevas (404 NOT_FOUND) y gemini-3.8-flash devuelve 503 por saturacion.
MODELO_GEMINI = "gemini-3.5-flash-lite"
INTERVALO_MINUTOS = 15

# Tope de llamadas a Gemini por corrida. La capa gratuita da 20
# peticiones al dia: si la hoja tuviera 40 filas pendientes, una sola
# corrida se comeria la cuota completa. Las filas que sobren se analizan
# en la corrida siguiente.
MAX_ANALISIS_POR_CORRIDA = 10

# Indices de columna (1 = A, 2 = B, ...)
COLUMNA_COMENTARIO = 3      # C - Comentario / Queja
COLUMNA_CLASIFICACION = 4   # D - Clasificacion IA
COLUMNA_SENTIMIENTO = 5     # E - Sentimiento IA
COLUMNA_EJECUTADO = 6       # F - Agente Ejecutado

PRIMERA_FILA_DATOS = 2      # la fila 1 son los encabezados
MARCA_EJECUTADO = "SI"

CATEGORIAS = ("Ventas", "Soporte Técnico", "Logística")
SENTIMIENTOS = ("Positivo", "Negativo", "Neutro")

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Modo de prueba sin gastar cuota: python agente_soporte.py --simulado
MODO_SIMULADO = "--simulado" in sys.argv

# Cliente de Gemini. Se crea una sola vez en el bloque __main__.
cliente_gemini = None

# Filas que fallaron en esta ejecucion. No se vuelven a intentar hasta
# que reinicies el script, para que un error no entre en bucle y queme
# las 20 llamadas diarias.
filas_con_error = set()


PROMPT_ANALISIS = """Eres un analista del área de atención al cliente.
Lee el comentario de un cliente y devuelve dos etiquetas.

Clasificación (elige exactamente una):
- Ventas: precios, cobros, facturas, promociones, cancelaciones de compra.
- Soporte Técnico: el producto no funciona, fallas, errores, instalación, configuración.
- Logística: envíos, entregas, paquetería, retrasos, paquetes dañados o perdidos.

Sentimiento (elige exactamente uno):
- Positivo: el cliente está satisfecho o agradece.
- Negativo: el cliente está molesto, se queja o reclama.
- Neutro: sólo pide información o informa, sin carga emocional.

Responde ÚNICAMENTE con este objeto JSON, sin texto antes ni después:
{{"clasificacion": "...", "sentimiento": "..."}}

Comentario del cliente:
\"\"\"{comentario}\"\"\"
"""


# =====================================================================
# UTILIDADES
# =====================================================================

def ahora():
    """Marca de tiempo legible para los mensajes de consola."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def celda(fila, indice_columna):
    """Devuelve el texto de una celda, o "" si la fila es mas corta.

    get_all_values() recorta las columnas vacias del final, asi que una
    fila recien llegada del Formulario puede traer solo 3 elementos.
    """
    if indice_columna <= len(fila):
        return str(fila[indice_columna - 1]).strip()
    return ""


def limpiar_json(texto):
    """Quita las cercas ``` o ```json que Gemini suele agregar."""
    limpio = texto.strip()
    if limpio.startswith("```"):
        limpio = re.sub(r"^```[A-Za-z]*\s*", "", limpio)
        limpio = re.sub(r"\s*```$", "", limpio)
    return limpio.strip()


def normalizar(texto):
    """Minusculas, sin acentos y sin espacios de sobra, para comparar."""
    descompuesto = unicodedata.normalize("NFKD", str(texto))
    sin_acentos = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return " ".join(sin_acentos.lower().split())


def valor_permitido(valor, permitidos, etiqueta):
    """Regla de oro: el codigo valida, el prompt solo pide.

    Acepta "soporte tecnico", "Soporte Técnico" o "SOPORTE TECNICO" y
    devuelve siempre la forma canonica. Si Gemini inventa una categoria,
    lanza ValueError y la fila no se marca como procesada.
    """
    objetivo = normalizar(valor)
    for opcion in permitidos:
        if normalizar(opcion) == objetivo:
            return opcion
    raise ValueError(
        "%s invalido: %r (permitidos: %s)" % (etiqueta, valor, ", ".join(permitidos))
    )


# =====================================================================
# CONEXION A GOOGLE SHEETS
# =====================================================================

def conectar_sheets():
    """Abre la hoja de calculo y devuelve la primera pestaña.

    La primera vez abre el navegador para que autorices la cuenta; el
    permiso queda guardado en ARCHIVO_TOKEN y ya no vuelve a pedirlo.
    """
    cliente = gspread.oauth(
        credentials_filename=ARCHIVO_CREDENCIALES,
        authorized_user_filename=ARCHIVO_TOKEN,
    )
    libro = cliente.open(NOMBRE_HOJA)
    return libro.sheet1


# =====================================================================
# ANALISIS
# =====================================================================

def analizar_simulado(comentario):
    """Clasificador de palabras clave para el modo --simulado.

    Sirve para probar la conexion con Sheets sin gastar cuota de Gemini.
    """
    texto = normalizar(comentario)

    if any(p in texto for p in ("envio", "entrega", "paqueter", "retraso", "paquete", "llego")):
        clasificacion = "Logística"
    elif any(p in texto for p in ("precio", "cobr", "factur", "promocion", "pago", "cargo")):
        clasificacion = "Ventas"
    else:
        clasificacion = "Soporte Técnico"

    positivas = ("gracias", "excelente", "muy bien", "felicid", "encanta", "rapidisimo")
    negativas = (
        "no funciona", "no abre", "no sirve", "no llego", "no me llego",
        "retraso", "demora", "falla", "error", "roto", "danad", "perdid",
        "molest", "pesim", "enoj", "harto", "nunca", "reclam", "queja",
    )

    if any(p in texto for p in positivas):
        sentimiento = "Positivo"
    elif any(p in texto for p in negativas):
        sentimiento = "Negativo"
    else:
        sentimiento = "Neutro"

    return {"clasificacion": clasificacion, "sentimiento": sentimiento}


def analizar_con_gemini(comentario):
    """Manda el comentario a Gemini y devuelve un dict ya validado.

    Devuelve {"clasificacion": ..., "sentimiento": ...}.
    Lanza una excepcion si la respuesta no es un JSON valido o si trae
    una etiqueta que no esta en la lista permitida.
    """
    if MODO_SIMULADO:
        return analizar_simulado(comentario)

    if cliente_gemini is None:
        raise RuntimeError("El cliente de Gemini no esta inicializado.")

    respuesta = cliente_gemini.models.generate_content(
        model=MODELO_GEMINI,
        contents=PROMPT_ANALISIS.format(comentario=comentario),
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )

    crudo = (respuesta.text or "").strip()
    if not crudo:
        raise ValueError("Gemini devolvio una respuesta vacia.")

    datos = json.loads(limpiar_json(crudo))
    if not isinstance(datos, dict):
        raise ValueError("Gemini no devolvio un objeto JSON: %r" % (crudo[:120],))

    return {
        "clasificacion": valor_permitido(
            datos.get("clasificacion"), CATEGORIAS, "clasificacion"
        ),
        "sentimiento": valor_permitido(
            datos.get("sentimiento"), SENTIMIENTOS, "sentimiento"
        ),
    }


# =====================================================================
# CICLO DEL AGENTE
# =====================================================================

def filas_pendientes(filas):
    """Lista de (numero_de_fila, comentario) que falta analizar."""
    pendientes = []
    for numero_fila, fila in enumerate(filas, start=1):
        if numero_fila < PRIMERA_FILA_DATOS:
            continue
        if numero_fila in filas_con_error:
            continue
        if celda(fila, COLUMNA_EJECUTADO).upper() == MARCA_EJECUTADO:
            continue
        comentario = celda(fila, COLUMNA_COMENTARIO)
        if not comentario:
            continue
        pendientes.append((numero_fila, comentario))
    return pendientes


def ejecutar_agente():
    """Una pasada completa: leer, analizar y escribir."""
    print("")
    print("[%s] Revisando la hoja..." % ahora())

    try:
        hoja = conectar_sheets()
        filas = hoja.get_all_values()
    except Exception as error:
        print("  [ERROR] No se pudo leer la hoja: %s" % error)
        return

    pendientes = filas_pendientes(filas)
    if not pendientes:
        print("  Sin quejas nuevas. Nada que hacer.")
        return

    print("  %d queja(s) pendiente(s)." % len(pendientes))

    if len(pendientes) > MAX_ANALISIS_POR_CORRIDA:
        print("  Se procesaran %d en esta corrida (tope de cuota); el resto en la siguiente."
              % MAX_ANALISIS_POR_CORRIDA)
        pendientes = pendientes[:MAX_ANALISIS_POR_CORRIDA]

    procesadas = 0
    fallidas = 0

    for numero_fila, comentario in pendientes:
        resumen = comentario if len(comentario) <= 60 else comentario[:57] + "..."
        print('  Fila %d: "%s"' % (numero_fila, resumen))

        try:
            resultado = analizar_con_gemini(comentario)
        except Exception as error:
            fallidas += 1
            filas_con_error.add(numero_fila)
            print("    [ERROR] %s: %s" % (type(error).__name__, error))
            print("    La fila queda sin marcar y no se reintenta hasta reiniciar.")
            continue

        try:
            # El "SI" se escribe al final: si algo falla a medias, la
            # fila sigue contando como pendiente.
            hoja.update_cell(numero_fila, COLUMNA_CLASIFICACION, resultado["clasificacion"])
            hoja.update_cell(numero_fila, COLUMNA_SENTIMIENTO, resultado["sentimiento"])
            hoja.update_cell(numero_fila, COLUMNA_EJECUTADO, MARCA_EJECUTADO)
        except Exception as error:
            fallidas += 1
            filas_con_error.add(numero_fila)
            print("    [ERROR] No se pudo escribir en la hoja: %s" % error)
            continue

        procesadas += 1
        print("    -> %s / %s" % (resultado["clasificacion"], resultado["sentimiento"]))

    print("  Resumen: %d procesada(s), %d con error." % (procesadas, fallidas))


# =====================================================================
# ARRANQUE
# =====================================================================

if __name__ == "__main__":
    print("=" * 62)
    print(" AGENTE DE SOPORTE AL CLIENTE - Unidad 2")
    print(" Ricardo Nuñez Hernandez - 23070505")
    print("=" * 62)

    if MODO_SIMULADO:
        print("MODO SIMULADO: no se llama a Gemini (cuota intacta).")
    elif not GEMINI_API_KEY:
        print("[ERROR] Falta GEMINI_API_KEY.")
        print("        Crea un archivo .env junto al script con esta linea:")
        print("        GEMINI_API_KEY=tu_clave")
        sys.exit(1)

    if not os.path.exists(ARCHIVO_CREDENCIALES):
        print("[ERROR] No encuentro el archivo %s" % ARCHIVO_CREDENCIALES)
        print("        Descargalo de Google Cloud (OAuth, app de escritorio)")
        print("        y guardalo junto a este script con ese nombre exacto.")
        sys.exit(1)

    if not MODO_SIMULADO:
        # attempts=2 limita los reintentos del SDK. Cada reintento
        # consume cuota, y con el valor por defecto un modelo saturado
        # se come las 20 llamadas del dia en una sola fila.
        cliente_gemini = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=2),
            ),
        )
        print("Cliente de Gemini listo. Modelo: %s" % MODELO_GEMINI)

    print("Hoja objetivo: %s" % NOMBRE_HOJA)
    print("Intervalo: cada %d minutos." % INTERVALO_MINUTOS)

    # Primera corrida inmediata, para no esperar el intervalo.
    ejecutar_agente()

    schedule.every(INTERVALO_MINUTOS).minutes.do(ejecutar_agente)

    print("")
    print("[%s] Agente en marcha. Ctrl + C para detenerlo." % ahora())

    try:
        while True:
            schedule.run_pending()
            time.sleep(30)
    except KeyboardInterrupt:
        print("")
        print("[%s] Agente detenido por el usuario." % ahora())
