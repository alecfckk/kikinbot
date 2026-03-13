import os
import json
import base64
import logging
import tempfile
import hashlib
import hmac
from datetime import datetime
from aiohttp import web
import asyncio
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
import anthropic
from openai import OpenAI

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Claves ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
OPENAI_API_KEY    = os.environ["OPENAI_API_KEY"]
MINI_APP_URL      = os.environ.get("MINI_APP_URL", "https://alecfckk.github.io/kikinbot/")
BOT_API_URL       = os.environ.get("BOT_API_URL", "")   # ej: https://tu-app.railway.app
PORT              = int(os.environ.get("PORT", 8080))
client        = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ── Estados de conversación ───────────────────────────────────────────────────
DOSE_WEIGHT, DOSE_DRUG = range(2)
APPT_PATIENT, APPT_DATE, APPT_NOTE, APPT_PHONE = range(3, 7)

# ── Almacenamiento en memoria (caché) ─────────────────────────────────────────
user_histories:  dict[int, list[dict]] = {}
user_reminders:  dict[int, list[str]]  = {}
patient_records: dict[int, list[dict]] = {}
appointments:    dict[int, list[dict]] = {}

# ── PostgreSQL ────────────────────────────────────────────────────────────────
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    """Crea las tablas si no existen."""
    if not DATABASE_URL:
        logger.warning("⚠ DATABASE_URL no definida — usando memoria (datos no persistentes)")
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS patients (
                        uid     BIGINT NOT NULL,
                        data    JSONB  NOT NULL,
                        PRIMARY KEY (uid)
                    );
                    CREATE TABLE IF NOT EXISTS appointments (
                        uid     BIGINT NOT NULL,
                        data    JSONB  NOT NULL,
                        PRIMARY KEY (uid)
                    );
                    CREATE TABLE IF NOT EXISTS reminders (
                        uid     BIGINT NOT NULL,
                        data    JSONB  NOT NULL,
                        PRIMARY KEY (uid)
                    );
                """)
        logger.info("✅ Base de datos inicializada")
    except Exception as e:
        logger.error(f"init_db error: {e}")

def load_data():
    """Carga todos los datos de PostgreSQL a memoria."""
    global user_reminders, patient_records, appointments
    if not DATABASE_URL:
        return
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT uid, data FROM patients")
                patient_records = {row["uid"]: row["data"] for row in cur.fetchall()}

                cur.execute("SELECT uid, data FROM appointments")
                appointments = {row["uid"]: row["data"] for row in cur.fetchall()}

                cur.execute("SELECT uid, data FROM reminders")
                user_reminders = {row["uid"]: row["data"] for row in cur.fetchall()}

        logger.info(f"✅ Datos cargados: {len(patient_records)} usuarios con pacientes")
    except Exception as e:
        logger.error(f"load_data error: {e}")

def _upsert(table: str, uid: int, data):
    """Guarda o actualiza un registro para un usuario."""
    if not DATABASE_URL:
        return
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    INSERT INTO {table} (uid, data) VALUES (%s, %s)
                    ON CONFLICT (uid) DO UPDATE SET data = EXCLUDED.data
                """, (uid, json.dumps(data, ensure_ascii=False)))
    except Exception as e:
        logger.error(f"_upsert {table} uid={uid} error: {e}")

def save_patients(uid: int):
    _upsert("patients", uid, patient_records.get(uid, []))

def save_appointments(uid: int):
    _upsert("appointments", uid, appointments.get(uid, []))

def save_reminders(uid: int):
    _upsert("reminders", uid, user_reminders.get(uid, []))

def save_data():
    """Guarda TODO (usado solo al arrancar para migrar datos viejos si los hubiera)."""
    for uid in patient_records:
        save_patients(uid)
    for uid in appointments:
        save_appointments(uid)
    for uid in user_reminders:
        save_reminders(uid)

# ── Fármacos odontológicos con dosis ─────────────────────────────────────────
FARMACOS = {
    "lidocaina":      {"nombre": "Lidocaína 2%", "dosis_mg_kg": 4.4,  "max_mg": 300, "presentacion": "36 mg/carpule (1.8 mL)"},
    "articaina":      {"nombre": "Articaína 4%",  "dosis_mg_kg": 7.0,  "max_mg": 500, "presentacion": "72 mg/carpule (1.8 mL)"},
    "mepivacaina":    {"nombre": "Mepivacaína 3%","dosis_mg_kg": 4.4,  "max_mg": 300, "presentacion": "54 mg/carpule (1.8 mL)"},
    "amoxicilina":    {"nombre": "Amoxicilina",   "dosis_mg_kg": 25,   "max_mg": 500, "presentacion": "500 mg/cápsula", "intervalo": "c/8h x 7 días"},
    "ibuprofeno":     {"nombre": "Ibuprofeno",    "dosis_mg_kg": 10,   "max_mg": 400, "presentacion": "400 mg/tableta", "intervalo": "c/8h"},
    "paracetamol":    {"nombre": "Paracetamol",   "dosis_mg_kg": 15,   "max_mg": 500, "presentacion": "500 mg/tableta", "intervalo": "c/6-8h"},
    "ketorolaco":     {"nombre": "Ketorolaco",    "dosis_mg_kg": 0.5,  "max_mg": 30,  "presentacion": "30 mg/ampolleta", "intervalo": "c/6h"},
    "dexametasona":   {"nombre": "Dexametasona",  "dosis_mg_kg": 0.15, "max_mg": 8,   "presentacion": "8 mg/2 mL", "intervalo": "dosis única"},
    "clindamicina":   {"nombre": "Clindamicina",  "dosis_mg_kg": 10,   "max_mg": 300, "presentacion": "300 mg/cápsula", "intervalo": "c/8h x 7 días"},
}

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Eres un asistente clínico odontológico de alto nivel, diseñado para estudiantes de último semestre de la Universidad Pablo Guardado Chávez (UPGCH), Tuxtla Gutiérrez, Chiapas.

PERFIL DEL ESTUDIANTE:
- 8° semestre, grupo A2 Mixto Salud, ciclo F-J 2526
- Materias: Administración, Ortodoncia II, Clínica de Cirugía Bucal, Clínica Integral de Adultos II, Clínica de Odontopediatría, Seminario de Tesis II
- Nivel: próximo a titularse, con experiencia clínica real

CAPACIDADES CLÍNICAS DETALLADAS:
1. DUDAS CLÍNICAS
   - Protocolos paso a paso (cirugía, endodoncia, periodoncia, ortodoncia, odontopediatría)
   - Diagnósticos diferenciales con criterios clínicos y radiográficos
   - Farmacología: mecanismo de acción, dosis, interacciones, contraindicaciones
   - Manejo de complicaciones trans y postoperatorias
   - Interpretación radiográfica y criterios de éxito

2. TESIS (Seminario de Tesis II)
   - Redacción académica en APA 7ma edición
   - Estructura IMRD completa
   - Estadística descriptiva e inferencial aplicada a odontología
   - Revisión crítica de literatura indexada (PubMed, Scopus, Lilacs)

3. FICHAS DE PACIENTE
   - Plan de tratamiento por fases: sistémica, higiénica, correctiva, mantenimiento
   - Consideraciones médicas por edad y comorbilidades
   - Secuencia de tratamiento por prioridad clínica

4. FARMACOLOGÍA
   - Anestésicos locales: dosis exacta por peso, carpules, técnica
   - Antibióticos, AINEs, corticosteroides
   - Interacciones medicamentosas frecuentes

5. ADMINISTRACIÓN DE CONSULTORIO
   - Gestión clínica, aspectos legales, consentimiento informado

ESTILO DE RESPUESTA:
- Responde SIEMPRE en español mexicano
- Sé clínico, preciso y directo — como un especialista consultado por un colega
- Para protocolos: usa pasos numerados
- Para fármacos: incluye SIEMPRE dosis, vía, intervalo y contraindicaciones
- Para diagnósticos: menciona criterios diferenciales
- Cita fuentes cuando sea relevante (ADA, AAE, AAP, guías clínicas)
- Si algo requiere criterio clínico presencial, menciónalo
"""

VISION_PROMPT = """Eres un experto en lectura de documentos clínicos odontológicos de México.
Se te envía la foto de una carpeta clínica médico-odontológica de la UPGCH (Universidad Pablo Guardado Chávez).

INSTRUCCIONES CRÍTICAS:
1. Analiza TODA la imagen con máximo detalle, incluyendo texto manuscrito, impreso y etiquetas
2. Infiere datos parcialmente visibles usando contexto (ej: si ves "17/09/5_" probablemente es 17/09/55)
3. Para texto manuscrito difícil, intenta leerlo aunque no estés 100% seguro — marca con (?) si hay duda
4. El número de expediente suele estar en una etiqueta lateral o esquina superior
5. La especialidad puede estar marcada con una palomita o subrayada en la lista impresa
6. Nombres en México suelen tener 2 nombres y 2 apellidos
7. El campo "alumno" corresponde al estudiante que atiende al paciente

Devuelve ÚNICAMENTE un JSON válido con esta estructura (sin texto adicional, sin markdown, sin explicaciones):
{
  "nombre": "",
  "fecha_nacimiento": "",
  "edad": "",
  "sexo": "",
  "estado_civil": "",
  "domicilio": "",
  "ocupacion": "",
  "alumno": "",
  "grupo": "",
  "semestre": "",
  "fecha_apertura": "",
  "especialidad": "",
  "numero_expediente": "",
  "alerta": "",
  "notas_adicionales": ""
}

IMPORTANTE: Prefiere un dato aproximado con (?) a dejar el campo vacío. Solo deja vacío si el campo realmente no existe en el documento.
"""

# ── Teclado principal ─────────────────────────────────────────────────────────
def _app_url() -> str:
    """Mini App URL con la URL del bot como parámetro para que la app pueda sincronizar."""
    if BOT_API_URL:
        from urllib.parse import urlencode
        return f"{MINI_APP_URL}?{urlencode({'api': BOT_API_URL})}"
    return MINI_APP_URL

def main_keyboard():
    buttons = [
        [KeyboardButton("🦷 Duda clínica"),   KeyboardButton("📝 Tesis")],
        [KeyboardButton("📅 Mi horario"),      KeyboardButton("⏰ Recordatorio")],
        [KeyboardButton("🗂️ Mis pacientes"),  KeyboardButton("🗓️ Agenda")],
        [KeyboardButton("💊 Calcular dosis"),  KeyboardButton("🔄 Nueva consulta")],
        [KeyboardButton("📱 Abrir App", web_app=WebAppInfo(url=_app_url()))],
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

def cancel_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("❌ Cancelar")]], resize_keyboard=True)

# ── Horario ───────────────────────────────────────────────────────────────────
HORARIO = """📅 *Tu horario — 8° A2 Mixto Salud Odontología*
_Ciclo F-J 2526_

*LUNES*
• 12:50–14:30 → Odontopediatría
• 14:30–16:10 → Ortodoncia II
• 16:10–17:50 → Clínica Integral de Adultos II
• 17:50–18:40 → Administración

*MARTES*
• 14:30–16:10 → Administración
• 16:10–17:50 → Odontopediatría

*MIÉRCOLES*
• 13:40–14:30 → Odontopediatría
• 14:30–16:10 → Seminario de Tesis II
• 16:10–17:50 → Clínica Integral de Adultos II
• 17:50–19:30 → Cirugía Bucal

*JUEVES*
• 14:30–16:10 → Odontopediatría
• 16:10–17:00 → Clínica Integral de Adultos II
• 17:00–17:50 → Cirugía Bucal
• 17:50–19:30 → Seminario de Tesis II

*VIERNES*
• 14:30–16:10 → Ortodoncia II
• 16:10–17:50 → Clínica Integral de Adultos II
• 17:50–19:30 → Cirugía Bucal
"""

# ── Helpers ───────────────────────────────────────────────────────────────────
def whatsapp_link(telefono: str, nombre: str, fecha: str, nota: str = "") -> str:
    """Genera enlace directo a WhatsApp con mensaje pregenerado."""
    # Limpiar número: solo dígitos, agregar 52 si es México
    numero = "".join(filter(str.isdigit, telefono))
    if len(numero) == 10:
        numero = "52" + numero
    procedimiento = f" para *{nota}*" if nota else ""
    mensaje = (
        f"Hola {nombre}, le saluda la clínica odontológica de la UPGCH. "
        f"Le recordamos su cita{procedimiento} el día *{fecha}*. "
        f"Por favor confírmenos su asistencia. ¡Muchas gracias! 🦷"
    )
    from urllib.parse import quote
    return f"https://wa.me/{numero}?text={quote(mensaje)}"

def format_patient_card(p: dict) -> str:
    lines = [
        "🗂️ *FICHA DE PACIENTE*",
        f"📋 Expediente: `{p.get('numero_expediente') or 'N/D'}`",
        "",
        f"👤 *{p.get('nombre') or 'Sin nombre'}*",
        f"🎂 Nacimiento: {p.get('fecha_nacimiento') or 'N/D'}  |  Edad: {p.get('edad') or 'N/D'}",
        f"⚧ Sexo: {p.get('sexo') or 'N/D'}  |  Estado civil: {p.get('estado_civil') or 'N/D'}",
        f"🏠 Domicilio: {p.get('domicilio') or 'N/D'}",
        f"📞 Teléfono: {p.get('telefono') or 'N/D'}",
        f"💼 Ocupación: {p.get('ocupacion') or 'N/D'}",
        "",
        f"🎓 Alumno: {p.get('alumno') or 'N/D'}",
        f"📚 Semestre: {p.get('semestre') or 'N/D'}  |  Grupo: {p.get('grupo') or 'N/D'}",
        f"📅 Fecha apertura: {p.get('fecha_apertura') or 'N/D'}",
    ]
    if p.get("especialidad"):
        lines.append(f"🦷 Especialidad: {p['especialidad']}")
    if p.get("alerta"):
        lines.append(f"⚠️ *ALERTA:* {p['alerta']}")
    if p.get("notas_adicionales"):
        lines.append(f"📌 Notas: {p['notas_adicionales']}")
    lines.append(f"\n🕐 Registrado: {p.get('_registrado', 'N/D')}")
    return "\n".join(lines)

def calcular_dosis(farmaco_key: str, peso_kg: float) -> str:
    f = FARMACOS.get(farmaco_key.lower())
    if not f:
        return None
    dosis_calculada = round(f["dosis_mg_kg"] * peso_kg, 1)
    dosis_final     = min(dosis_calculada, f["max_mg"])
    carpules = ""
    if "carpule" in f["presentacion"].lower():
        mg_por_carpule = float(f["presentacion"].split(" mg")[0])
        num_carpules   = round(dosis_final / mg_por_carpule, 1)
        carpules = f"\n🪥 Carpules: *{num_carpules}* ({f['presentacion']})"
    intervalo = f"\n🕐 Intervalo: {f['intervalo']}" if f.get("intervalo") else ""
    aviso = ""
    if dosis_calculada > f["max_mg"]:
        aviso = f"\n⚠️ Dosis calculada ({dosis_calculada} mg) supera el máximo. Se aplica dosis máxima."
    return (
        f"💊 *{f['nombre']}*\n"
        f"⚖️ Peso: {peso_kg} kg  |  {f['dosis_mg_kg']} mg/kg\n"
        f"✅ Dosis: *{dosis_final} mg*"
        f"{carpules}"
        f"\n💬 Presentación: {f['presentacion']}"
        f"{intervalo}"
        f"{aviso}"
    )

async def ai_reply(uid: int, user_content) -> str:
    if uid not in user_histories:
        user_histories[uid] = []
    user_histories[uid].append({"role": "user", "content": user_content})
    if len(user_histories[uid]) > 20:
        user_histories[uid] = user_histories[uid][-20:]
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=user_histories[uid]
        )
        reply = response.content[0].text
        user_histories[uid].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Anthropic error: {e}")
        return "⚠️ Error al consultar la IA. Intenta de nuevo."

# ══════════════════════════════════════════════════════════════════════════════
# CALCULADORA DE DOSIS — ConversationHandler
# ══════════════════════════════════════════════════════════════════════════════
async def dose_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lista = "\n".join(f"• `{k}` — {v['nombre']}" for k, v in FARMACOS.items())
    await update.message.reply_text(
        f"💊 *Calculadora de dosis odontológica*\n\n"
        f"Fármacos disponibles:\n{lista}\n\n"
        f"Primero dime el *peso del paciente en kg*:",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )
    return DOSE_WEIGHT

async def dose_weight(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=main_keyboard())
        return ConversationHandler.END
    try:
        peso = float(update.message.text.replace(",", "."))
        if peso <= 0 or peso > 300:
            raise ValueError
        context.user_data["dose_weight"] = peso
        lista = ", ".join(f"`{k}`" for k in FARMACOS.keys())
        await update.message.reply_text(
            f"✅ Peso: *{peso} kg*\n\nAhora escribe el nombre del fármaco:\n{lista}",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard()
        )
        return DOSE_DRUG
    except ValueError:
        await update.message.reply_text("⚠️ Ingresa un peso válido en kg (ej: 65):")
        return DOSE_WEIGHT

async def dose_drug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=main_keyboard())
        return ConversationHandler.END
    peso  = context.user_data.get("dose_weight", 0)
    drug  = update.message.text.strip().lower()
    result = calcular_dosis(drug, peso)
    if result:
        await update.message.reply_text(result, parse_mode="Markdown", reply_markup=main_keyboard())
    else:
        # Si no está en la lista, usa IA
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        uid = update.effective_user.id
        prompt = (
            f"Calcula la dosis de *{update.message.text}* para un paciente de {peso} kg "
            f"en el contexto odontológico. Incluye: dosis mg/kg, dosis total calculada, "
            f"dosis máxima, presentación comercial e intervalo de administración."
        )
        reply = await ai_reply(uid, prompt)
        await update.message.reply_text(reply, parse_mode="Markdown", reply_markup=main_keyboard())
    return ConversationHandler.END

# ══════════════════════════════════════════════════════════════════════════════
# AGENDA DE CITAS — ConversationHandler
# ══════════════════════════════════════════════════════════════════════════════
async def appt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    records = patient_records.get(uid, [])
    if not records:
        await update.message.reply_text(
            "No tienes pacientes registrados.\n📸 Primero envíame la foto de una carpeta clínica.",
            reply_markup=main_keyboard()
        )
        return ConversationHandler.END

    lista = "\n".join(
        f"{i+1}. *{p.get('nombre','Sin nombre')}* (Exp: {p.get('numero_expediente','N/D')})"
        for i, p in enumerate(records)
    )
    await update.message.reply_text(
        f"🗓️ *Nueva cita*\n\n¿Para qué paciente?\n\n{lista}\n\nEscribe el *número*:",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )
    return APPT_PATIENT

async def appt_patient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=main_keyboard())
        return ConversationHandler.END
    uid     = update.effective_user.id
    records = patient_records.get(uid, [])
    try:
        idx = int(update.message.text.strip()) - 1
        p   = records[idx]
        context.user_data["appt_patient"] = p.get("nombre", "Sin nombre")
        context.user_data["appt_exp"]     = p.get("numero_expediente", "N/D")
        await update.message.reply_text(
            f"👤 Paciente: *{p.get('nombre','Sin nombre')}*\n\n"
            f"Ahora escribe la *fecha y hora* de la cita:\nEjemplo: _Lunes 17 marzo 15:30_",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard()
        )
        return APPT_DATE
    except (ValueError, IndexError):
        await update.message.reply_text("⚠️ Número inválido. Intenta de nuevo:")
        return APPT_PATIENT

async def appt_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=main_keyboard())
        return ConversationHandler.END
    context.user_data["appt_date"] = update.message.text.strip()
    await update.message.reply_text(
        "📝 ¿Alguna nota para esta cita? (procedimiento, materiales, etc.)\nEscribe `ninguna` para omitir.",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )
    return APPT_NOTE

async def appt_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=main_keyboard())
        return ConversationHandler.END
    nota = update.message.text.strip()
    if nota.lower() == "ninguna":
        nota = ""
    context.user_data["appt_nota"] = nota
    await update.message.reply_text(
        "📞 ¿Cuál es el *teléfono del paciente* para el recordatorio de WhatsApp?\n"
        "Escribe `ninguno` para omitir.",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard()
    )
    return APPT_PHONE

async def appt_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "❌ Cancelar":
        await update.message.reply_text("Cancelado.", reply_markup=main_keyboard())
        return ConversationHandler.END
    uid      = update.effective_user.id
    telefono = update.message.text.strip()
    if telefono.lower() == "ninguno":
        telefono = ""
    nota = context.user_data.get("appt_nota", "")

    cita = {
        "paciente":   context.user_data.get("appt_patient", "?"),
        "expediente": context.user_data.get("appt_exp", "N/D"),
        "fecha":      context.user_data.get("appt_date", "?"),
        "nota":       nota,
        "telefono":   telefono,
        "creada":     datetime.now().strftime("%d/%m/%Y %H:%M"),
    }

    if uid not in appointments:
        appointments[uid] = []
    appointments[uid].append(cita)
    save_appointments(uid)

    if uid not in user_reminders:
        user_reminders[uid] = []
    user_reminders[uid].append(
        f"[{datetime.now().strftime('%d/%m %H:%M')}] 🗓️ Cita: {cita['paciente']} — {cita['fecha']}"
        + (f" — {nota}" if nota else "")
    )
    save_reminders(uid)

    msg = (
        f"✅ *Cita agendada*\n\n"
        f"👤 {cita['paciente']} (Exp: {cita['expediente']})\n"
        f"📅 {cita['fecha']}\n"
        + (f"📝 {nota}\n" if nota else "")
        + f"\n⏰ Recordatorio guardado automáticamente.\n"
        f"Usa /agenda para ver todas tus citas."
    )
    if telefono:
        wa_link = whatsapp_link(telefono, cita["paciente"], cita["fecha"], nota)
        msg += f"\n\n📲 [Recordatorio WhatsApp]({wa_link})"

    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelado.", reply_markup=main_keyboard())
    return ConversationHandler.END

# ── Ver agenda ────────────────────────────────────────────────────────────────
async def show_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    citas = appointments.get(uid, [])
    if not citas:
        await update.message.reply_text(
            "No tienes citas agendadas.\nUsa el botón 🗓️ *Agenda* para crear una.",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
        return
    msg = f"🗓️ *Agenda — {len(citas)} cita(s):*\n\n"
    for i, c in enumerate(citas, 1):
        msg += (
            f"{i}. 👤 *{c['paciente']}* (Exp: {c['expediente']})\n"
            f"   📅 {c['fecha']}\n"
            + (f"   📝 {c['nota']}\n" if c.get('nota') else "")
            + "\n"
        )
    msg += "Usa `/eliminar_cita N` para borrar una cita."
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())

async def delete_appointment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    citas = appointments.get(uid, [])
    try:
        idx     = int(context.args[0]) - 1
        removed = citas.pop(idx)
        save_appointments(uid)
        await update.message.reply_text(
            f"🗑️ Cita de *{removed['paciente']}* ({removed['fecha']}) eliminada.",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
    except (IndexError, ValueError, TypeError):
        await update.message.reply_text("Uso: `/eliminar_cita N`")

# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS EXISTENTES
# ══════════════════════════════════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_histories[user.id] = []
    await update.message.reply_text(
        f"¡Hola, {user.first_name}! 👋🦷\n\n"
        "Soy tu asistente odontológico v3.\n\n"
        "📸 Envíame la foto de una *carpeta clínica* → ficha + plan de tratamiento\n"
        "💊 Botón *Calcular dosis* → dosis por peso del paciente\n"
        "🗓️ Botón *Agenda* → programa citas por paciente\n\n"
        "¿En qué te ayudo?",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def show_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HORARIO, parse_mode="Markdown", reply_markup=main_keyboard())

async def new_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_histories[update.effective_user.id] = []
    await update.message.reply_text("🔄 Contexto limpiado.", reply_markup=main_keyboard())

async def show_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    reminders = user_reminders.get(uid, [])
    if not reminders:
        await update.message.reply_text("No tienes recordatorios. 📭", reply_markup=main_keyboard())
    else:
        msg = "📌 *Recordatorios:*\n\n" + "\n".join(f"• {r}" for r in reminders)
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())

async def clear_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_reminders[update.effective_user.id] = []
    save_reminders(uid)
    await update.message.reply_text("🗑️ Recordatorios borrados.", reply_markup=main_keyboard())

async def show_patients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    records = patient_records.get(uid, [])
    if not records:
        await update.message.reply_text(
            "No tienes pacientes registrados.\n📸 Envíame la foto de una carpeta clínica.",
            reply_markup=main_keyboard()
        )
        return
    msg = f"🗂️ *{len(records)} paciente(s):*\n\n"
    for i, p in enumerate(records, 1):
        msg += f"{i}. *{p.get('nombre','Sin nombre')}* — Exp: {p.get('numero_expediente','N/D')} — {p.get('_registrado','')}\n"
    msg += "\n`/paciente N` → ver detalle  |  `/eliminar_paciente N` → borrar"
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_keyboard())

async def patient_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        idx = int(context.args[0]) - 1
        await update.message.reply_text(
            format_patient_card(patient_records[uid][idx]),
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
    except (IndexError, ValueError, TypeError):
        await update.message.reply_text("Uso: `/paciente N`", parse_mode="Markdown")

async def delete_patient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        idx     = int(context.args[0]) - 1
        removed = patient_records[uid].pop(idx)
        save_patients(uid)
        await update.message.reply_text(
            f"🗑️ Paciente *{removed.get('nombre','?')}* eliminado.",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
    except (IndexError, ValueError, TypeError):
        await update.message.reply_text("Uso: `/eliminar_paciente N`")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await update.message.reply_text("📸 Leyendo la carpeta clínica... 🔍")

    photo      = update.message.photo[-1]
    file       = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    img_b64    = base64.standard_b64encode(bytes(file_bytes)).decode("utf-8")

    try:
        vision_resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}},
                {"type": "text",  "text": VISION_PROMPT}
            ]}]
        )
        raw     = vision_resp.content[0].text.strip().replace("```json","").replace("```","").strip()
        patient = json.loads(raw)
    except Exception as e:
        logger.error(f"Vision error: {e}")
        await update.message.reply_text(
            "⚠️ No pude leer la carpeta. Asegúrate de que la foto sea clara.",
            reply_markup=main_keyboard()
        )
        return

    patient["_registrado"] = datetime.now().strftime("%d/%m/%Y %H:%M")
    if uid not in patient_records:
        patient_records[uid] = []
    patient_records[uid].append(patient)
    patient_idx = len(patient_records[uid])
    save_patients(uid)

    await update.message.reply_text(format_patient_card(patient), parse_mode="Markdown")

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    plan_prompt = (
        f"Paciente registrado:\n{json.dumps(patient, ensure_ascii=False, indent=2)}\n\n"
        "Como clínico de último semestre de la UPGCH, genera un plan de tratamiento inicial estructurado por FASES:\n\n"
        "FASE 1 - SISTÉMICA: consideraciones médicas según edad, sexo y alertas del paciente\n"
        "FASE 2 - HIGIÉNICA: control de placa, motivación, instrucciones de higiene\n"
        "FASE 3 - CORRECTIVA: procedimientos clínicos sugeridos según la especialidad indicada\n"
        "FASE 4 - MANTENIMIENTO: frecuencia de citas de control\n\n"
        "Incluye consideraciones farmacológicas relevantes si aplica. "
        "Si la especialidad es Cirugía, incluye criterios quirúrgicos. "
        "Si es Odontopediatría, incluye técnica de manejo de conducta. "
        "Sé clínico y específico."
    )
    plan = await ai_reply(uid, plan_prompt)
    await update.message.reply_text(f"📋 *Plan de tratamiento sugerido:*\n\n{plan}", parse_mode="Markdown")

    nombre = patient.get("nombre") or "el paciente"
    if uid not in user_reminders:
        user_reminders[uid] = []
    user_reminders[uid].append(
        f"[{datetime.now().strftime('%d/%m %H:%M')}] Seguimiento: {nombre} — Exp: {patient.get('numero_expediente','N/D')}"
    )

    await update.message.reply_text(
        f"✅ *Paciente #{patient_idx} guardado.*\n"
        f"⏰ Recordatorio de seguimiento creado.\n\n"
        f"• /pacientes — lista de pacientes\n"
        f"• 🗓️ *Agenda* — programa su próxima cita",
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    await update.message.reply_text("🎤 Transcribiendo tu nota de voz...")

    # Descargar audio de Telegram
    voice_file = await context.bot.get_file(update.message.voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await voice_file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    # Transcribir con Whisper
    try:
        with open(tmp_path, "rb") as audio_file:
            transcription = openai_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="es"
            )
        texto = transcription.text.strip()
    except Exception as e:
        logger.error(f"Whisper error: {e}")
        await update.message.reply_text(
            "⚠️ No pude transcribir el audio. Intenta de nuevo o escribe tu pregunta.",
            reply_markup=main_keyboard()
        )
        return
    finally:
        os.unlink(tmp_path)

    if not texto:
        await update.message.reply_text("⚠️ No entendí el audio. Intenta hablar más claro o escribe tu pregunta.")
        return

    # Mostrar lo que entendió
    await update.message.reply_text(f"🗣️ *Entendí:* _{texto}_", parse_mode="Markdown")

    # Responder con Claude
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = await ai_reply(uid, texto)
    await update.message.reply_text(reply, reply_markup=main_keyboard())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text

    if text == "📅 Mi horario":      await show_schedule(update, context); return
    if text == "🔄 Nueva consulta":  await new_query(update, context);     return
    if text == "🗂️ Mis pacientes":  await show_patients(update, context);  return
    if text == "🗓️ Agenda":         await show_agenda(update, context);    return
    if text == "⏰ Recordatorio":
        await update.message.reply_text(
            "⏰ Escribe qué quieres recordar.\nEjemplo: _Entregar protocolo el viernes 21_",
            parse_mode="Markdown"
        ); return
    if text == "🦷 Duda clínica":
        await update.message.reply_text("🦷 Escribe tu duda clínica:"); return
    if text == "📝 Tesis":
        await update.message.reply_text("📝 ¿En qué parte de tu tesis necesitas ayuda?"); return

    lower = text.lower()
    if any(w in lower for w in ["recordar", "recordatorio", "no olvidar", "acuérdate", "avísame"]):
        if uid not in user_reminders:
            user_reminders[uid] = []
        user_reminders[uid].append(f"[{datetime.now().strftime('%d/%m %H:%M')}] {text}")
        save_reminders(uid)
        await update.message.reply_text(
            f"✅ Guardado. Tienes {len(user_reminders[uid])} recordatorio(s).\n/recordatorios para verlos.",
            reply_markup=main_keyboard()
        ); return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = await ai_reply(uid, text)
    await update.message.reply_text(reply, reply_markup=main_keyboard())

async def open_app(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🦷 Abrir OdontoApp", web_app=WebAppInfo(url=_app_url()))
    ]])
    await update.message.reply_text("📱 Toca para abrir tu app clínica:", reply_markup=keyboard)

# ── API HTTP para Mini App (datos reales) ─────────────────────────────────────
def verify_telegram_data(init_data: str) -> int | None:
    """Verifica initData de Telegram WebApp y retorna el user_id si es válido.
    
    NOTA: aiohttp ya URL-decodifica los query params, NO llamar unquote() de nuevo.
    """
    try:
        from urllib.parse import parse_qs
        # init_data ya viene decodificado por aiohttp — NO hacer unquote()
        parsed = parse_qs(init_data, keep_blank_values=True)
        hash_val = parsed.pop("hash", [None])[0]
        if not hash_val:
            logger.warning("verify_telegram_data: no hash en initData")
            return None
        # Construir data-check-string según spec de Telegram
        data_check = "\n".join(
            f"{k}={v[0]}" for k, v in sorted(parsed.items())
        )
        secret   = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, hash_val):
            logger.warning("verify_telegram_data: HMAC inválido")
            return None
        import json as _json
        user = _json.loads(parsed.get("user", ["{}"])[0])
        uid = user.get("id")
        logger.info(f"verify_telegram_data: uid={uid} OK")
        return uid
    except Exception as e:
        logger.error(f"verify_telegram_data error: {e}")
        return None

async def api_data(request: web.Request) -> web.Response:
    """GET /api/data?init_data=... → devuelve pacientes y citas del usuario."""
    headers = {
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
    }
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=headers)

    uid = None

    # 1. Verificar con initData de Telegram (producción)
    init_data = request.rel_url.query.get("init_data", "")
    if init_data:
        uid = verify_telegram_data(init_data)
        if not uid:
            logger.warning("api_data: initData presente pero inválido")

    # 2. Fallback por ?uid= (desarrollo / debug)
    if not uid:
        raw_uid = request.rel_url.query.get("uid", "")
        if raw_uid.isdigit():
            uid = int(raw_uid)
            logger.info(f"api_data: uid={uid} via fallback ?uid=")

    if not uid:
        logger.warning("api_data: sin uid — retornando 401")
        return web.Response(
            status=401,
            text=json.dumps({"error": "Unauthorized"}),
            content_type="application/json",
            headers=headers
        )

    data = {
        "patients":     patient_records.get(uid, []),
        "appointments": appointments.get(uid, []),
        "reminders":    user_reminders.get(uid, []),
    }
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
        headers=headers
    )

async def start_api_server():
    app_web = web.Application()
    app_web.router.add_get("/",             lambda r: web.Response(text="kikin bot ok"))
    app_web.router.add_get("/health",       lambda r: web.Response(text="ok"))
    app_web.router.add_get("/api/data",     api_data)
    app_web.router.add_options("/api/data", api_data)
    app_web.router.add_get("/miniapp/data", api_data)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 API server en http://0.0.0.0:{PORT}")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    init_db()
    load_data()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # ConversationHandler — Calculadora de dosis
    dose_conv = ConversationHandler(
        entry_points=[
            CommandHandler("dosis", dose_start),
            MessageHandler(filters.Regex("^💊 Calcular dosis$"), dose_start),
        ],
        states={
            DOSE_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, dose_weight)],
            DOSE_DRUG:   [MessageHandler(filters.TEXT & ~filters.COMMAND, dose_drug)],
        },
        fallbacks=[CommandHandler("cancelar", cancel_conv)],
    )

    # ConversationHandler — Agenda de citas
    appt_conv = ConversationHandler(
        entry_points=[
            CommandHandler("nueva_cita", appt_start),
            MessageHandler(filters.Regex("^🗓️ Agenda$"), appt_start),
        ],
        states={
            APPT_PATIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, appt_patient)],
            APPT_DATE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, appt_date)],
            APPT_NOTE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, appt_note)],
            APPT_PHONE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, appt_phone)],
        },
        fallbacks=[CommandHandler("cancelar", cancel_conv)],
    )

    app.add_handler(dose_conv)
    app.add_handler(appt_conv)
    app.add_handler(CommandHandler("start",                  start))
    app.add_handler(CommandHandler("app",                    open_app))
    app.add_handler(CommandHandler("horario",                show_schedule))
    app.add_handler(CommandHandler("recordatorios",          show_reminders))
    app.add_handler(CommandHandler("borrar_recordatorios",   clear_reminders))
    app.add_handler(CommandHandler("nuevo",                  new_query))
    app.add_handler(CommandHandler("pacientes",              show_patients))
    app.add_handler(CommandHandler("paciente",               patient_detail))
    app.add_handler(CommandHandler("eliminar_paciente",      delete_patient))
    app.add_handler(CommandHandler("agenda",                 show_agenda))
    app.add_handler(CommandHandler("eliminar_cita",          delete_appointment))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.VOICE,                   handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    WEBHOOK_BASE = (BOT_API_URL or "").rstrip("/")

    if not WEBHOOK_BASE:
        # Sin BOT_API_URL → polling local
        logger.info("🦷 OdontoBot iniciado en modo polling (local)")
        app.run_polling(allowed_updates=Update.ALL_TYPES)
        return

    # ── Modo webhook (Railway) ──
    # Corremos aiohttp nosotros mismos e integramos PTB manualmente.
    import asyncio

    async def telegram_webhook(request: web.Request) -> web.Response:
        """Recibe updates de Telegram y los pasa a PTB."""
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.process_update(update)
        return web.Response(text="ok")

    async def run():
        async with app:
            await app.bot.set_webhook(
                url=f"{WEBHOOK_BASE}/telegram",
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            logger.info(f"✅ Webhook: {WEBHOOK_BASE}/telegram")

            aio_app = web.Application()
            aio_app.router.add_get("/",             lambda r: web.Response(text="kikin ok"))
            aio_app.router.add_get("/health",       lambda r: web.Response(text="ok"))
            aio_app.router.add_get("/api/data",     api_data)
            aio_app.router.add_options("/api/data", api_data)
            aio_app.router.add_get("/miniapp/data", api_data)
            aio_app.router.add_post("/telegram",    telegram_webhook)

            await app.start()
            runner = web.AppRunner(aio_app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", PORT).start()
            logger.info(f"🌐 Servidor en 0.0.0.0:{PORT}")
            await asyncio.Event().wait()  # correr para siempre

    asyncio.run(run())

if __name__ == "__main__":
    main()
