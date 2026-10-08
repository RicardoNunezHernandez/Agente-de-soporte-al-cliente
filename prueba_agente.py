# -*- coding: utf-8 -*-
"""Prueba de agente_soporte.py con las librerias externas simuladas.

gspread / schedule / dotenv / google-genai no estan instaladas en esta
maquina, asi que se registran versiones falsas en sys.modules ANTES de
importar el script. Eso permite ejercitar toda la logica real:
filtrado de filas, limpieza del JSON, validacion de etiquetas y
escritura en la hoja.
"""

import json
import sys
import types as pytypes


# ---------------------------------------------------------------- stubs

class HojaFalsa:
    def __init__(self, filas):
        self.filas = [list(f) for f in filas]
        self.escrituras = []          # (fila, columna, valor) en orden
        self.fallar_al_escribir = False

    def get_all_values(self):
        return [list(f) for f in self.filas]

    def update_cell(self, fila, columna, valor):
        if self.fallar_al_escribir:
            raise RuntimeError("APIError simulado de Sheets")
        self.escrituras.append((fila, columna, valor))
        while len(self.filas[fila - 1]) < columna:
            self.filas[fila - 1].append("")
        self.filas[fila - 1][columna - 1] = valor


class LibroFalso:
    def __init__(self, hoja):
        self.sheet1 = hoja


class ClienteGspreadFalso:
    def __init__(self, hoja):
        self.hoja = hoja
        self.abiertos = []

    def open(self, nombre):
        self.abiertos.append(nombre)
        return LibroFalso(self.hoja)


HOJA_ACTUAL = {"hoja": None, "cliente": None, "oauth_kwargs": None}

gspread_falso = pytypes.ModuleType("gspread")


def oauth_falso(**kwargs):
    HOJA_ACTUAL["oauth_kwargs"] = kwargs
    cliente = ClienteGspreadFalso(HOJA_ACTUAL["hoja"])
    HOJA_ACTUAL["cliente"] = cliente
    return cliente


gspread_falso.oauth = oauth_falso
sys.modules["gspread"] = gspread_falso

schedule_falso = pytypes.ModuleType("schedule")
schedule_falso.every = lambda *a, **k: None
schedule_falso.run_pending = lambda: None
sys.modules["schedule"] = schedule_falso

dotenv_falso = pytypes.ModuleType("dotenv")
dotenv_falso.load_dotenv = lambda *a, **k: False
sys.modules["dotenv"] = dotenv_falso


class RespuestaFalsa:
    def __init__(self, texto):
        self.text = texto


class ModelosFalsos:
    def __init__(self):
        self.guion = []               # respuestas a devolver, en orden
        self.llamadas = []            # prompts recibidos

    def generate_content(self, model=None, contents=None, config=None):
        self.llamadas.append(contents)
        if not self.guion:
            raise AssertionError("Gemini recibio mas llamadas de las previstas")
        siguiente = self.guion.pop(0)
        if isinstance(siguiente, Exception):
            raise siguiente
        return RespuestaFalsa(siguiente)


class ClienteGeminiFalso:
    def __init__(self, api_key=None, **_kwargs):
        self.models = ModelosFalsos()


genai_falso = pytypes.ModuleType("google.genai")
genai_falso.Client = ClienteGeminiFalso

types_falso = pytypes.ModuleType("google.genai.types")


class ConfigFalsa:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class HttpOptionsFalsas:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


types_falso.GenerateContentConfig = ConfigFalsa
types_falso.HttpOptions = HttpOptionsFalsas
types_falso.HttpRetryOptions = HttpOptionsFalsas
genai_falso.types = types_falso

google_falso = sys.modules.get("google") or pytypes.ModuleType("google")
google_falso.genai = genai_falso
sys.modules["google"] = google_falso
sys.modules["google.genai"] = genai_falso
sys.modules["google.genai.types"] = types_falso


# -------------------------------------------------------------- importa

import agente_soporte as ag

fallos = []


def revisar(nombre, condicion, detalle=""):
    if condicion:
        print("  OK   %s" % nombre)
    else:
        print("  FALLA %s  %s" % (nombre, detalle))
        fallos.append(nombre)


def preparar(filas, guion):
    """Deja el modulo listo para una corrida con una hoja nueva."""
    hoja = HojaFalsa(filas)
    HOJA_ACTUAL["hoja"] = hoja
    ag.filas_con_error.clear()
    ag.MODO_SIMULADO = False
    cliente = ClienteGeminiFalso(api_key="falsa")
    cliente.models.guion = list(guion)
    ag.cliente_gemini = cliente
    return hoja, cliente


JSON_OK = '{"clasificacion": "Soporte Técnico", "sentimiento": "Negativo"}'


print("")
print("1. limpiar_json")
revisar("json pelon", ag.limpiar_json(JSON_OK) == JSON_OK)
revisar("cerca ```json", ag.limpiar_json("```json\n" + JSON_OK + "\n```") == JSON_OK)
revisar("cerca ``` simple", ag.limpiar_json("```\n" + JSON_OK + "\n```") == JSON_OK)
revisar("espacios de sobra", ag.limpiar_json("  \n```JSON\n" + JSON_OK + "\n```  \n") == JSON_OK)
revisar("carga con json.loads",
        json.loads(ag.limpiar_json("```json\n" + JSON_OK + "```"))["sentimiento"] == "Negativo")

print("")
print("2. normalizar / valor_permitido")
revisar("acentos fuera", ag.normalizar("Soporte Técnico") == "soporte tecnico")
revisar("sin acento -> canonico",
        ag.valor_permitido("soporte tecnico", ag.CATEGORIAS, "c") == "Soporte Técnico")
revisar("mayusculas -> canonico",
        ag.valor_permitido("LOGÍSTICA", ag.CATEGORIAS, "c") == "Logística")
revisar("espacios raros -> canonico",
        ag.valor_permitido("  Ventas  ", ag.CATEGORIAS, "c") == "Ventas")
try:
    ag.valor_permitido("Recursos Humanos", ag.CATEGORIAS, "clasificacion")
    revisar("categoria inventada revienta", False, "no lanzo ValueError")
except ValueError as e:
    revisar("categoria inventada revienta", "Recursos Humanos" in str(e))
try:
    ag.valor_permitido(None, ag.SENTIMIENTOS, "sentimiento")
    revisar("None revienta", False, "no lanzo ValueError")
except ValueError:
    revisar("None revienta", True)

print("")
print("3. celda (filas cortas del Formulario)")
fila_corta = ["2026-10-08", "ana@mail.com", "No me llegó el paquete"]
revisar("comentario", ag.celda(fila_corta, ag.COLUMNA_COMENTARIO) == "No me llegó el paquete")
revisar("columna F inexistente", ag.celda(fila_corta, ag.COLUMNA_EJECUTADO) == "")
revisar("recorta espacios", ag.celda(["a", "b", "  hola  "], 3) == "hola")

print("")
print("4. filas_pendientes")
ag.filas_con_error.clear()
filas = [
    ["Fecha", "Correo", "Comentario", "Clasificación IA", "Sentimiento IA", "Agente Ejecutado"],
    ["f", "a@a.com", "No funciona la app", "Soporte Técnico", "Negativo", "SI"],   # ya hecha
    ["f", "b@b.com", "Me cobraron doble"],                                          # pendiente
    ["f", "c@c.com", "", "", "", ""],                                               # sin comentario
    ["f", "d@d.com", "El envío va con retraso", "", "", "si"],                      # ya hecha minuscula
    ["f", "e@e.com", "El cargador echa humo", "", "", ""],                           # pendiente
]
pend = ag.filas_pendientes(filas)
revisar("ignora encabezado, SI y vacios", [n for n, _ in pend] == [3, 6], str(pend))
ag.filas_con_error.add(3)
revisar("ignora filas con error previo", [n for n, _ in ag.filas_pendientes(filas)] == [6])
ag.filas_con_error.clear()

print("")
print("5. analizar_con_gemini")
_, cliente = preparar([["h"]], ["```json\n" + JSON_OK + "\n```"])
res = ag.analizar_con_gemini("La app se cierra sola")
revisar("devuelve dict validado",
        res == {"clasificacion": "Soporte Técnico", "sentimiento": "Negativo"}, str(res))
revisar("el comentario viaja en el prompt",
        "La app se cierra sola" in cliente.models.llamadas[0])
revisar("el prompt no deja llaves de format",
        "{comentario}" not in cliente.models.llamadas[0])

_, cliente = preparar([["h"]], ['{"clasificacion": "Devoluciones", "sentimiento": "Negativo"}'])
try:
    ag.analizar_con_gemini("x")
    revisar("rechaza categoria inventada", False, "no lanzo")
except ValueError:
    revisar("rechaza categoria inventada", True)

_, cliente = preparar([["h"]], ["no soy json"])
try:
    ag.analizar_con_gemini("x")
    revisar("rechaza texto que no es json", False, "no lanzo")
except json.JSONDecodeError:
    revisar("rechaza texto que no es json", True)

_, cliente = preparar([["h"]], [""])
try:
    ag.analizar_con_gemini("x")
    revisar("rechaza respuesta vacia", False, "no lanzo")
except ValueError as e:
    revisar("rechaza respuesta vacia", "vacia" in str(e))

print("")
print("6. ejecutar_agente - corrida feliz")
filas_ok = [
    ["Fecha", "Correo", "Comentario", "Clasificación IA", "Sentimiento IA", "Agente Ejecutado"],
    ["f", "ana@mail.com", "Mi paquete lleva 2 semanas de retraso", "", "", ""],
    ["f", "carlos@mail.com", "La app no abre después de actualizar", "", "", ""],
    ["f", "maria@mail.com", "Gracias, me resolvieron el cobro doble", "", "", ""],
]
hoja, cliente = preparar(filas_ok, [
    '```json\n{"clasificacion": "Logística", "sentimiento": "Negativo"}\n```',
    '{"clasificacion": "soporte tecnico", "sentimiento": "negativo"}',
    '{"clasificacion": "Ventas", "sentimiento": "Positivo"}',
])
ag.ejecutar_agente()
revisar("abrio la hoja por su nombre exacto",
        HOJA_ACTUAL["cliente"].abiertos == [ag.NOMBRE_HOJA], str(HOJA_ACTUAL["cliente"].abiertos))
revisar("usa client_secrets.json y token_usuario.json",
        HOJA_ACTUAL["oauth_kwargs"] == {"credentials_filename": "client_secrets.json",
                                        "authorized_user_filename": "token_usuario.json"},
        str(HOJA_ACTUAL["oauth_kwargs"]))
revisar("3 llamadas a Gemini", len(cliente.models.llamadas) == 3)
revisar("fila 2 -> D/E/F", hoja.filas[1][3:6] == ["Logística", "Negativo", "SI"], str(hoja.filas[1]))
revisar("fila 3 normalizada -> D/E/F",
        hoja.filas[2][3:6] == ["Soporte Técnico", "Negativo", "SI"], str(hoja.filas[2]))
revisar("fila 4 -> D/E/F", hoja.filas[3][3:6] == ["Ventas", "Positivo", "SI"], str(hoja.filas[3]))
revisar("el SI se escribe al final de cada fila",
        [c for f, c, v in hoja.escrituras if f == 2] == [4, 5, 6], str(hoja.escrituras[:3]))
revisar("no reprocesa en la segunda corrida",
        ag.filas_pendientes(hoja.get_all_values()) == [])

print("")
print("7. ejecutar_agente - Gemini falla en una fila")
hoja, cliente = preparar(filas_ok, [
    RuntimeError("429 RESOURCE_EXHAUSTED simulado"),
    '{"clasificacion": "Soporte Técnico", "sentimiento": "Negativo"}',
    '{"clasificacion": "Ventas", "sentimiento": "Positivo"}',
])
ag.ejecutar_agente()
revisar("la fila que fallo queda sin marcar", hoja.filas[1][3:6] == ["", "", ""], str(hoja.filas[1]))
revisar("las demas si se procesan", hoja.filas[2][5] == "SI" and hoja.filas[3][5] == "SI")
revisar("la fila queda en la lista negra", ag.filas_con_error == {2}, str(ag.filas_con_error))
cliente.models.guion = ['{"clasificacion": "Ventas", "sentimiento": "Neutro"}']
ag.ejecutar_agente()
revisar("no la reintenta en la corrida siguiente", len(cliente.models.llamadas) == 3,
        "llamadas=%d" % len(cliente.models.llamadas))

print("")
print("8. ejecutar_agente - falla la escritura en Sheets")
hoja, cliente = preparar(filas_ok, ['{"clasificacion": "Ventas", "sentimiento": "Neutro"}'] * 3)
hoja.fallar_al_escribir = True
ag.ejecutar_agente()
revisar("nada queda marcado", all(f[5] == "" for f in hoja.filas[1:]))
revisar("las 3 filas van a la lista negra", ag.filas_con_error == {2, 3, 4}, str(ag.filas_con_error))

print("")
print("9. tope de cuota por corrida")
muchas = [["Fecha", "Correo", "Comentario", "D", "E", "F"]]
for i in range(25):
    muchas.append(["f", "x@x.com", "queja numero %d" % i, "", "", ""])
hoja, cliente = preparar(muchas, ['{"clasificacion": "Ventas", "sentimiento": "Neutro"}'] * 25)
ag.ejecutar_agente()
revisar("solo %d llamadas a Gemini" % ag.MAX_ANALISIS_POR_CORRIDA,
        len(cliente.models.llamadas) == ag.MAX_ANALISIS_POR_CORRIDA,
        "llamadas=%d" % len(cliente.models.llamadas))
revisar("quedan 15 pendientes para despues",
        len(ag.filas_pendientes(hoja.get_all_values())) == 15)

print("")
print("10. no se puede leer la hoja")
ag.filas_con_error.clear()
guardado = gspread_falso.oauth
gspread_falso.oauth = lambda **k: (_ for _ in ()).throw(RuntimeError("sin red"))
try:
    ag.ejecutar_agente()
    revisar("no tumba el programa", True)
except Exception as e:
    revisar("no tumba el programa", False, repr(e))
gspread_falso.oauth = guardado

print("")
print("11. modo --simulado (sin Gemini)")
hoja, cliente = preparar(filas_ok, [])
ag.MODO_SIMULADO = True
ag.cliente_gemini = None
ag.ejecutar_agente()
revisar("no llama a Gemini", cliente.models.llamadas == [])
revisar("retraso de paquete -> Logística/Negativo",
        hoja.filas[1][3:6] == ["Logística", "Negativo", "SI"], str(hoja.filas[1]))
revisar("la app no abre -> Soporte Técnico/Negativo",
        hoja.filas[2][3:6] == ["Soporte Técnico", "Negativo", "SI"], str(hoja.filas[2]))
revisar("gracias -> Positivo", hoja.filas[3][4] == "Positivo", str(hoja.filas[3]))
ag.MODO_SIMULADO = False

print("")
print("12. constantes que pide la practica")
revisar("NOMBRE_HOJA exacto", ag.NOMBRE_HOJA == "Respuestas de Quejas (Base de Datos)")
revisar("ARCHIVO_CREDENCIALES", ag.ARCHIVO_CREDENCIALES == "client_secrets.json")
# El PDF pedia gemini-2.5-flash. Google lo retiro para cuentas nuevas
# (404) y gemini-3.8-flash contesta 503, asi que la practica corre con
# gemini-3.5-flash-lite. La prueba fija el modelo que de verdad usamos.
revisar("MODELO_GEMINI", ag.MODELO_GEMINI == "gemini-3.5-flash-lite",
        ag.MODELO_GEMINI)
revisar("INTERVALO_MINUTOS", ag.INTERVALO_MINUTOS == 15)
revisar("columnas D/E/F = 4/5/6",
        (ag.COLUMNA_CLASIFICACION, ag.COLUMNA_SENTIMIENTO, ag.COLUMNA_EJECUTADO) == (4, 5, 6))
revisar("3 categorias", ag.CATEGORIAS == ("Ventas", "Soporte Técnico", "Logística"))
revisar("3 sentimientos", ag.SENTIMIENTOS == ("Positivo", "Negativo", "Neutro"))

print("")
print("=" * 58)
if fallos:
    print("FALLARON %d prueba(s): %s" % (len(fallos), ", ".join(fallos)))
    sys.exit(1)
print("TODAS LAS PRUEBAS PASARON")
