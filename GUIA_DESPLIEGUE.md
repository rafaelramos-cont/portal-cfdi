# Guía de Despliegue — Portal CFDI DIF Municipal La Paz BCS

## Opción A — Railway (Recomendada, gratis/económica)

### Requisitos previos
- Cuenta en [railway.app](https://railway.app) (registro con correo)
- Cuenta en [github.com](https://github.com) (registro gratuito)
- Git instalado en tu computadora: https://git-scm.com/downloads

---

### Paso 1 — Subir el código a GitHub

1. Abre una terminal (o PowerShell en Windows).
2. Navega a la carpeta `portal-cfdi`:
   ```
   cd ruta\a\portal-cfdi
   ```
3. Ejecuta:
   ```
   git init
   git add .
   git commit -m "Portal CFDI inicial"
   ```
4. Ve a github.com → **New repository** → nombre: `portal-cfdi` → **Create**.
5. Copia los comandos que GitHub te da en "push an existing repository" y ejecútalos.

---

### Paso 2 — Desplegar en Railway

1. Ve a [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
2. Selecciona el repositorio `portal-cfdi`.
3. Railway detectará el `Procfile` automáticamente.
4. Haz clic en **Add Service** → **Database** → **PostgreSQL** para agregar la base de datos.
5. Ve a **Variables** y agrega:
   ```
   SECRET_KEY = una-cadena-larga-y-aleatoria-de-al-menos-32-chars
   DATABASE_URL = (Railway la pone automáticamente al agregar PostgreSQL)
   ```
6. En **Settings** → **Networking** → **Generate Domain** para obtener tu URL pública.

---

### Paso 3 — Inicializar la base de datos

1. En Railway, ve al servicio de la app → **Shell** → ejecuta:
   ```
   python -c "from app import init_db; init_db()"
   ```
2. Esto crea las tablas y el primer usuario administrador.

---

### Paso 4 — Primer acceso

1. Abre tu URL de Railway en cualquier navegador.
2. Inicia sesión con:
   - **Correo:** `admin@dif.gob.mx`
   - **Contraseña:** `Admin2025!`
3. Ve a **Usuarios** → cambia la contraseña del admin inmediatamente.
4. Crea los usuarios para cada área.

---

## Opción B — Render (también gratuita)

1. Ve a [render.com](https://render.com) → **New Web Service** → conecta GitHub.
2. Selecciona el repositorio.
3. Configura:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
4. Agrega variable `SECRET_KEY`.
5. Agrega una base de datos PostgreSQL en Render y copia la URL a `DATABASE_URL`.

---

## Acceso desde las 5+ computadoras

Una vez desplegado, **cualquier computadora con internet** puede acceder al portal
abriendo el navegador y entrando a la URL de Railway/Render.

**No se instala nada** en las computadoras de los usuarios.
Solo necesitan un navegador (Chrome, Edge, Firefox).

---

## Roles del sistema

| Rol | Puede hacer |
|-----|-------------|
| `solicitante` | Crear solicitudes de compra, pago y comprobaciones |
| `supervisor` | Lo anterior + aprobar primer nivel |
| `director` | Lo anterior + aprobar segundo nivel de compras y pagos |
| `tesoreria` | Autorizar pagos finales + gestionar catálogo de proveedores |
| `admin` | Todo lo anterior + gestionar usuarios |

---

## Flujos de autorización

### Solicitud de Compra
```
Solicitante → Supervisor (Nivel 1) → Director (Nivel 2) → Aprobada
```

### Solicitud de Pago
```
Solicitante sube XML → SAT verifica automáticamente → Supervisor → Tesorería → Autorizado para Pago
```

### Comprobación de Gastos / Fondo Revolvente
```
Empleado sube XML → Sistema detecta duplicados → Supervisor → Contabilidad/Tesorería → Aprobada
```

---

## Verificaciones automáticas de CFDI

El sistema realiza automáticamente al recibir un XML:

1. **Extracción de datos** del XML (UUID, RFC emisor/receptor, importes, forma de pago)
2. **Consulta al SAT** para verificar si el CFDI está Vigente o Cancelado
3. **Detección de duplicados** revisando todos los UUIDs en el sistema (de pagos y comprobaciones)
4. **Alerta visual** si el CFDI está duplicado o cancelado — bloquea la aprobación hasta revisión manual

---

## Mantenimiento

- **Respaldo de base de datos:** En Railway/Render, la base PostgreSQL tiene respaldos automáticos.
- **Actualizaciones:** Haz cambios en el código, `git push`, Railway redesplegar automáticamente.
- **Soporte:** Contacta al administrador del sistema.

---

## Preguntas frecuentes

**¿Qué pasa si no tengo el XML del CFDI?**
En Solicitudes de Pago puedes llenar los datos manualmente. La verificación SAT seguirá funcionando si ingresas UUID, RFC emisor, RFC receptor y total.

**¿Se pueden subir PDFs?**
Sí, en Solicitudes de Compra como cotización adjunta. Para verificación CFDI se requiere el XML.

**¿Qué versiones de CFDI soporta?**
CFDI 3.3 y 4.0 (los vigentes del SAT).

**¿Cómo cambio el nombre de la organización?**
Busca "DIF Municipal La Paz BCS" en los archivos de templates y cambia por el nombre correcto.
