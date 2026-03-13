# 🦷 OdontoBot — Tu asistente de Odontología en Telegram

Bot inteligente para estudiantes de último semestre, impulsado por Claude (Anthropic).

---

## ✅ Funciones

- 🦷 Responde dudas clínicas (Cirugía, Ortodoncia, Odontopediatría, etc.)
- 📝 Ayuda con tu tesis (redacción, metodología, APA 7)
- 📅 Muestra tu horario del semestre
- ⏰ Guarda recordatorios importantes
- 🔄 Limpia el contexto para una nueva consulta

---

## 🚀 Cómo hacer el deploy (paso a paso)

### PASO 1 — Obtener tu Token de Telegram

1. Abre Telegram y busca **@BotFather**
2. Escribe `/newbot`
3. Dale un nombre: ej. `OdontoAsistente`
4. Dale un username: ej. `odonto_asesor_bot`
5. **Copia el token** que te da (se ve así: `123456789:ABCdef...`)

---

### PASO 2 — Obtener tu API Key de Anthropic

1. Ve a https://console.anthropic.com
2. Inicia sesión (o crea cuenta gratis)
3. Ve a **API Keys** → **Create Key**
4. **Copia la clave** (empieza con `sk-ant-...`)

---

### PASO 3 — Subir el código a GitHub

1. Ve a https://github.com y crea una cuenta si no tienes
2. Crea un **repositorio nuevo** (público o privado), llámalo `odontobot`
3. Sube estos 3 archivos:
   - `bot.py`
   - `requirements.txt`
   - `Procfile`

   Puedes hacerlo desde el botón **"Add file → Upload files"** en GitHub.

---

### PASO 4 — Deploy en Railway (recomendado, gratis)

1. Ve a https://railway.app
2. Inicia sesión con tu cuenta de GitHub
3. Click en **"New Project"** → **"Deploy from GitHub repo"**
4. Selecciona tu repositorio `odontobot`
5. Railway detectará automáticamente el `Procfile`

6. Ve a **Variables** (en la barra lateral del proyecto) y agrega:

   | Variable | Valor |
   |---|---|
   | `TELEGRAM_TOKEN` | El token de @BotFather |
   | `ANTHROPIC_API_KEY` | Tu clave de Anthropic |

7. Railway desplegará el bot automáticamente ✅

---

### PASO 4 alternativo — Deploy en Render (también gratis)

1. Ve a https://render.com
2. Crea cuenta con GitHub
3. **New → Web Service** → conecta tu repo `odontobot`
4. Configura:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python bot.py`
5. En **Environment Variables** agrega `TELEGRAM_TOKEN` y `ANTHROPIC_API_KEY`
6. Click **Create Web Service** ✅

---

## 💬 Comandos del bot

| Comando | Función |
|---|---|
| `/start` | Iniciar el bot |
| `/horario` | Ver tu horario completo |
| `/recordatorios` | Ver tus recordatorios guardados |
| `/borrar_recordatorios` | Limpiar recordatorios |
| `/nuevo` | Nueva consulta (limpia contexto) |

---

## 🔒 Seguridad

Nunca compartas tus variables de entorno (`TELEGRAM_TOKEN`, `ANTHROPIC_API_KEY`) con nadie.
Ambas plataformas (Railway y Render) las guardan de forma segura y encriptada.
