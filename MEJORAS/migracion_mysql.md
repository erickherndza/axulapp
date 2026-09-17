# Migración Axula → Banahosting/MySQL — estado y checklist

> Repo de trabajo: `axulapp` (fork de `axula`, igual patrón que `dipromes` →
> `dipromesapp`). `axula` original NO se toca — sigue en producción en Render
> con SQLite tal cual. Todo lo de esta migración vive aquí.

## ESTADO: esquema y datos reales ya están en Banahosting (2026-09-17)

- Base de datos `mybcfcli_axulapp` creada en cPanel (usuario propio, privilegios
  `ALL`). Repo `axulapp` clonado en `/home/mybcfcli/axulapp` con venv Python 3.11.
- `migrar_bd()` corrido contra el MySQL real → **55 tablas creadas**, idéntico
  al esquema de SQLite.
- **Datos reales migrados desde Render** (fila por fila, conexión directa
  Render Shell → MySQL de Banahosting por red, reutilizando la misma cuenta
  MySQL — no un dump intermedio): **26 tablas con datos, 1300 filas, 0
  errores** tras las correcciones de esta sesión. Incluye el roster completo
  (`estudiantes`: 608 filas), `usuarios` (31), `conducta_registro` (33),
  `casos` (1), `materias` (193), `recovery_tokens` (19), `ia_cache` (23), etc.
  `materias_calificaciones`/`calificaciones_periodo`/`cuaderno_anecdotico`
  están en 0 filas — consistente con el estado real (reset de notas para el
  año escolar 2026-2027, documentado en sesión 19, notas aún no cargadas al
  momento de esta migración).
- **27 tablas se omitieron a propósito** — existen en la BD vieja de Render
  pero no en el esquema actual del código: son remanentes de los módulos
  institucionales eliminados en la purga de sesión 14 (`normativa_chunks`
  con 3815 filas del viejo sistema RAG, `proveedores`, `nomina_pagos`,
  `raciones_diarias`, etc.). Correcto no migrarlas — nada en el código actual
  las usa.
- **Acceso remoto a MySQL** se habilitó temporalmente (`uapi Mysql add_host
  host=%`) solo para la ventana de la migración y se revirtió (`delete_host`)
  al terminar — no queda abierto.

**2 problemas reales encontrados y corregidos durante la migración de datos
(ninguno visible en las pruebas con Docker, porque Docker partía de una BD
vacía sin datos reales — solo se detectan corriendo contra los datos de
producción de verdad):**
1. **Deuda de esquema (schema drift)**: la BD real de Render tiene columnas
   que el esquema actual del código (`TABLAS_NUEVAS`) no crea —
   `estudiantes.ind_conducta`, `asignaciones.instrucciones`,
   `asistencia.metodo`, `notificaciones.origen_tipo`,
   `movimientos_financieros.proveedor_id`, `escaneos_documentos.categoria`.
   Son columnas agregadas en algún momento por scripts/migraciones puntuales
   que nunca se reflejaron de vuelta en `core/constants.py`. El script de
   migración de datos las detecta automáticamente comparando
   `PRAGMA table_info` (origen) contra `SHOW COLUMNS` (destino) y agrega las
   que falten como `TEXT NULL` antes de copiar — no se pierde ningún dato,
   pero **queda pendiente** decidir si estas columnas deben incorporarse
   formalmente al esquema en `core/constants.py` (probablemente sí, ya que
   claramente se usan en producción).
2. **Colación case/accent-insensitive de MySQL vs. SQLite case-sensitive**:
   `materias.nombre_canonico` tiene `UNIQUE`, y dos filas reales
   ("ARMONIA I" sin acento/mayúscula vs. "Armonía I" con acento/minúscula)
   son válidas y distintas en SQLite (comparación binaria) pero MySQL las
   trata como duplicadas bajo su colación por defecto (`utf8mb4_0900_ai_ci`,
   *ai*=accent-insensitive, *ci*=case-insensitive). Fix: se cambió la
   columna a `COLLATE utf8mb4_bin` (sensible a mayúsculas y acentos, mismo
   comportamiento que SQLite) — preserva ambas filas sin fusionarlas ni
   perder información. Es un dato real duplicado por inconsistencia de
   captura (mismo patrón que `_MATERIA_SINONIMOS` ya documentado en sesiones
   anteriores) — **queda pendiente** que Erick decida si conviene
   consolidarlas más adelante, no se tocó el contenido, solo se preservó.

**Pendiente de esta sesión, no bloqueante:**
- Formalizar en `core/constants.py` las columnas de drift descubiertas
  (punto 1 arriba) para que el esquema oficial coincida con lo que la app
  realmente usa en producción.
- Falta correr el Setup Python App real en cPanel (por ahora todo se hizo
  con un venv manual + gunicorn no configurado aún) para que la app sirva
  tráfico HTTP real desde Banahosting.

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

- **Probado contra un MySQL 8.0 real (Docker local, no solo unit-test de las
  regex)** — se corrió `migrar_bd()` completo contra un servidor MySQL de
  verdad, no solo passthrough en SQLite. Esto encontró y corrigió 6 problemas
  reales que el análisis de código no había anticipado:
  1. `datetime('now')` (73 ocurrencias) no estaba traducido — solo se cubrió
     `date('now')`. Agregado `datetime('now')` → `NOW()`.
  2. **PyMySQL usa `%` para interpolar parámetros** — cualquier `%` literal
     en el SQL (patrones `LIKE '%x%'`, `DATE_FORMAT(...,'%Y-%m')`, incluso
     comentarios SQL con "80%" o ejemplos JSON con `?`) rompía la ejecución
     ("not enough arguments for format string"). Fix en dos partes: (a) los
     comentarios `-- ...` se eliminan del SQL antes de traducir (MySQL los
     descarta igual, pero contienen `?`/`%` de ejemplo que confundían al
     traductor), (b) todo `%` literal restante se escapa a `%%` antes de
     convertir `?` en `%s`.
  3. `CREATE INDEX IF NOT EXISTS` no existe en MySQL (a diferencia de
     `CREATE TABLE IF NOT EXISTS`, que sí) — se quita el "IF NOT EXISTS" y
     la idempotencia se recupera absorbiendo el error 1061 (índice
     duplicado).
  4. **Columnas TEXT usadas como clave** (en `UNIQUE(...)`, `UNIQUE`/
     `PRIMARY KEY` inline, o en un `CREATE INDEX` separado) necesitan
     longitud de clave explícita en MySQL — SQLite nunca lo exige. Afecta,
     entre otras, a `usuarios.username` (de la que depende el login),
     `materia`, `anio_escolar`, `periodo`, `cedula`, `nombre`,
     `nombre_canonico`. En vez de mantener una lista de columnas a mano
     (frágil), `db_compat` detecta las columnas TEXT de cada `CREATE TABLE`
     al vuelo y les agrega `(191)` automáticamente donde haga falta —
     incluso cuando el índice es una sentencia `CREATE INDEX` separada de la
     tabla (se mantiene un registro `tabla → columnas TEXT` en memoria,
     poblado en el mismo orden en que corre `migrar_bd()`).
  5. **MySQL/InnoDB exige que la tabla referenciada por un FOREIGN KEY ya
     exista al momento del CREATE TABLE** — SQLite no lo exige nunca (FK sin
     `PRAGMA foreign_keys=ON`, que Axula nunca activa de forma persistente).
     Con ~90 archivos y un orden de `TABLAS_NUEVAS` pensado para SQLite,
     esto producía una cascada de "Failed to open the referenced table" para
     casi cualquier tabla que declarara una FK antes de que su tabla
     referenciada existiera. Fix: `SET FOREIGN_KEY_CHECKS=0` en cada
     conexión MySQL nueva — mismo comportamiento que ya tiene Axula en
     SQLite (las FK del esquema son documentación, no se validan).
  6. Bug real preexistente, no relacionado a esta migración, encontrado al
     probar un login real de punta a punta: `core/helpers.py` usaba
     `ROLES_PSICOLOGA` sin importarlo (`NameError`) al renderizar el
     dashboard para ciertos roles — corregido agregándolo al import de
     `.constants`. Habría fallado igual en Render/SQLite; simplemente nunca
     se había ejercitado ese código path.

  **Verificado con datos reales de negocio, no solo el esquema**: las 55
  tablas se crean idénticas a SQLite (mismo set exacto, comparado
  programáticamente), y un ciclo completo login → sesión → página protegida
  (`/mi-perfil`, `/health`) funciona contra el MySQL real vía
  `db_compat` — incluye una query real con `OR`/`LOWER()`/placeholders en el
  login (`routes/auth.py::login_post`).

  **Único gap conocido, de baja prioridad**: 1 índice funcional
  (`idx_exp_hist_nombre`, `LOWER(apellido), LOWER(nombre)` en
  `expedientes_historicos` — el archivo digitalizado histórico, no el
  roster activo) no se tradujo — MySQL exige `CAST(... AS CHAR(N))` para
  indexar una expresión que devuelve TEXT, no solo el paréntesis doble. El
  `CREATE TABLE` de esa tabla sí se crea bien; solo falta ese índice de
  case-insensitive, que ya fallaba de forma segura (capturado por el
  try/except del loop de `TABLAS_NUEVAS`, sin romper nada más).

## Pendiente — antes de tocar producción

1. **`scripts/` (57 archivos, herramientas de diagnóstico/mantenimiento por
   CLI)** — NO se convirtieron. Se ejecutan a mano, fuera del ciclo
   request/response de la app, así que no bloquean el deploy. Si alguno se
   necesita correr contra MySQL en el futuro, aplicar el mismo patrón
   (`db_compat.connect()` en vez de `sqlite3.connect()`).
2. **`core/migrations/002_motor_conductual.py`** — migración histórica
   puntual, ya aplicada en Render hace tiempo. No se tocó (no forma parte
   del arranque normal de la app). Nota: usa `conn.executescript()`, que el
   wrapper MySQL no implementa — pero está gateada por un
   `PRAGMA table_info(...)` que bajo MySQL siempre devuelve vacío (PRAGMA es
   no-op), así que ese bloque nunca se ejecuta bajo MySQL — no hay crash,
   simplemente esa migración histórica puntual queda sin efecto (correcto:
   solo aplicaba a bases viejas con un bug ya corregido en el esquema
   actual).
3. **`login.py` (raíz)** — confirmado código muerto: nunca se importa ni se
   registra en ningún blueprint (la autenticación real vive en
   `core/auth.py`/`routes/auth.py`, con contraseñas hasheadas). No se tocó.
4. **Índice funcional `idx_exp_hist_nombre`** (ver arriba) — pendiente de
   traducir con `CAST(... AS CHAR(191))` si se quiere recuperar la búsqueda
   case-insensitive en `expedientes_historicos`. No urgente.
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
