# Migración Axula → Banahosting/MySQL — estado y checklist

> Repo de trabajo: `axulapp` (fork de `axula`, igual patrón que `dipromes` →
> `dipromesapp`). `axula` original NO se toca — sigue en producción en Render
> con SQLite tal cual. Todo lo de esta migración vive aquí.

## Por qué esto no fue "cambiar el driver" (como dipromes)

`dipromes` ya usaba SQLAlchemy ORM sobre PostgreSQL — migrar a MySQL fue
cambiar el driver + 2 líneas de SQL crudo. Axula es sqlite3 crudo: ~807
llamadas `.execute()` con placeholders `?` repartidas en ~90 archivos, sin
ORM. Reescribir cada una habría sido inviable y arriesgaba datos reales de
estudiantes (notas, cuaderno anecdótico, casos disciplinarios). La solución:
centralizar la traducción SQLite↔MySQL en un solo módulo nuevo
(`core/db_compat.py`) y cambiar solo CÓMO se abre la conexión en cada
archivo — el resto de cada `.execute()` sigue exactamente igual.

## Hecho en esta sesión (verificado, no solo escrito)

- **`core/db_compat.py`** (nuevo) — capa de compatibilidad. `ENGINE` se
  decide por `DATABASE_URL`: sin esa variable (o si no empieza con
  `mysql://`/`mysql+pymysql://`) sigue usando sqlite3 real, cero cambio de
  comportamiento. Con `DATABASE_URL=mysql://...` envuelve PyMySQL y traduce
  automáticamente, verificado caso por caso contra el código real (no
  especulativo):
  - Placeholders `?` → `%s`.
  - `INTEGER PRIMARY KEY AUTOINCREMENT` → `INT AUTO_INCREMENT PRIMARY KEY`
    (56 ocurrencias en `core/constants.py`/`core/database.py`).
  - `INSERT OR IGNORE/REPLACE INTO` → `INSERT IGNORE INTO` / `REPLACE INTO`
    (11 ocurrencias).
  - Cualquier `PRAGMA` → no-op (WAL/journal_mode no aplican a MySQL).
  - `DEFAULT (date('now'))` → `DEFAULT (CURRENT_DATE)` (18 ocurrencias,
    defaults de columna en `core/constants.py`).
  - `date('now','-N days')` → `DATE_SUB(CURDATE(), INTERVAL N DAY)`,
    `date('now')` suelto → `CURDATE()` (usos en `core/helpers.py`,
    `routes/ocr.py`, `routes/casos.py`, `routes/estudiantes.py`,
    `routes/planificacion.py`).
  - `strftime('%Y-%m', x)` → `DATE_FORMAT(x, '%Y-%m')`,
    `strftime('%Y', x)` → `YEAR(x)` (usos en `core/promocion_engine.py`,
    `routes/asistencia.py`, `routes/calificaciones.py`).
  - Las 9 traducciones se probaron una por una contra el texto SQL real
    extraído del repo (no contra ejemplos inventados) — ver sesión de
    verificación, todas dieron el resultado correcto.
  - `GROUP_CONCAT(DISTINCT materia)` (única ocurrencia real, `core/helpers.py`)
    ya es válido en MySQL tal cual — no necesitó traducción.

- **Conversión mecánica de conexión** — 21 archivos (`app.py` +
  `core/database.py`, `core/helpers.py`, `core/analytics.py` + 18 blueprints
  bajo `routes/`) pasaron de `sqlite3.connect(DATABASE, ...)` a
  `db_compat.connect(DATABASE, ...)`. Nada más cambió en esos archivos — los
  ~800 `.execute()` con `?`, los `conn.row_factory = sqlite3.Row`, los
  `with conn:` como context manager, todo sigue igual porque el wrapper
  replica esa superficie.

- **`core/backup.py`** — ahora tiene 2 caminos: SQLite (backup API nativo,
  sin cambios) y MySQL (`mysqldump` vía subprocess, nuevo — `_dump_mysql()`).
  `_nombre_archivo()` usa `.sql` bajo MySQL, `.db` bajo SQLite; limpieza y
  listado de respaldos reconocen ambas extensiones.

- **`requirements.txt`** — agregado `PyMySQL==1.1.1` (driver pure-Python,
  mismo criterio que `dipromesapp`).

- **`passenger_wsgi.py`** (nuevo) — entry point para cPanel/Passenger.
  Trivial porque `app.py` ya corre `migrar_bd()` + `_seed_admin()` a nivel de
  módulo (igual que con gunicorn en Render) — Passenger solo importa
  `app.app` como `application`.

- **Password hashing** — verificado que `core/auth.py::_hash()` YA fuerza
  `method="pbkdf2:sha256"` explícito (línea 39). Axula nunca tuvo el bug de
  `hashlib.scrypt` que sí golpeó a `dipromesapp` en Python 3.9 — no hace
  falta ningún fix aquí.

- **Verificado en cPanel real** (mismo hosting de `dipromesapp`, sin crear ni
  cambiar nada): MySQL® Database Wizard / MySQL® Databases / Remote MySQL
  disponibles. Python Selector ofrece hasta **3.13.15** (no solo 3.9.23como
  se documentó para dipromesapp) — se puede usar Python 3.11/3.12 para
  axulapp, lo que evita por completo tener que tocar los 6 archivos que ya
  usan sintaxis `str | None` (3.10+).

- **Pruebas de humo (modo SQLite, Python 3.12 vía venv temporal)**:
  `import app` limpio, 21 blueprints registrados, `migrar_bd()` corre sin
  error a través de `db_compat.connect()`, `GET /health` responde 200 con
  chequeo real de conectividad a BD + respaldo. Cero regresión frente al
  comportamiento original.

## Pendiente — antes de tocar producción

1. **Probar contra un MySQL real** (Docker local o una BD de prueba en el
   mismo cPanel) — todo lo de arriba se verificó por unit-test de las
   regex de traducción y por passthrough en modo SQLite, pero **ningún query
   real corrió todavía contra un servidor MySQL de verdad**. Es el paso de
   mayor riesgo/incertidumbre restante.
2. **`scripts/` (57 archivos, herramientas de diagnóstico/mantenimiento por
   CLI)** — NO se convirtieron. Se ejecutan a mano, fuera del ciclo
   request/response de la app, así que no bloquean el deploy. Si alguno se
   necesita correr contra MySQL en el futuro, aplicar el mismo patrón
   (`db_compat.connect()` en vez de `sqlite3.connect()`).
3. **`core/migrations/002_motor_conductual.py`** — migración histórica
   puntual, ya aplicada en Render hace tiempo. No se tocó (no forma parte
   del arranque normal de la app).
4. **`login.py` (raíz)** — confirmado código muerto: nunca se importa ni se
   registra en ningún blueprint (la autenticación real vive en
   `core/auth.py`/`routes/auth.py`, con contraseñas hasheadas). No se tocó.
5. **Crear la base MySQL real en cPanel** — no se creó nada todavía (solo se
   confirmó que el Wizard está disponible). Falta decidir nombre de BD/app,
   elegir subdominio (dipromesapp usó `dipromes.erickhernandezarias.net` por
   el conflicto con el WordPress de `globalistinternational.org` — probable
   que Axula necesite el mismo tipo de subdominio limpio).
6. **Elegir versión de Python** en Setup Python App — recomendado 3.11 o
   3.12 (evita el rework de sintaxis 3.10+, y pandas/reportlab/anthropic ya
   tienen wheels para esas versiones).
7. **Migrar los datos reales** — el `database.db` local está vacío (solo 2
   usuarios de prueba); los datos reales (7,493+ notas, 772 estudiantes,
   cuaderno anecdótico, conducta, casos) solo existen en el disco persistente
   de Render. Falta: bajar ese archivo (Render Shell / disco), y cargarlo a
   MySQL con un script de transformación (se puede reusar `db_compat` como
   destino de escritura, leyendo con sqlite3 puro como origen).
8. **Variables de entorno en cPanel** — `DATABASE_URL=mysql://usuario:pass@
   localhost:3306/nombre_bd`, más las que ya usa Axula en Render
   (`SECRET_KEY`, `GROQ_API_KEY`, `ANTHROPIC_API_KEY`, `FOTOS_DIR`,
   `LOG_DIR`, `BACKUP_DIR`).
9. **`mysqldump` en el PATH del entorno cPanel** — verificar que el
   subprocess de `core/backup.py::_dump_mysql()` encuentra el binario
   (Banahosting normalmente lo tiene en `/usr/bin/mysqldump`, pero no se
   confirmó en esta sesión).
