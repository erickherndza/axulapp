# -*- coding: utf-8 -*-
"""
Capa de compatibilidad SQLite/MySQL.

Axula se escribió 100% contra sqlite3 crudo: placeholders '?', PRAGMA,
AUTOINCREMENT, INSERT OR IGNORE/REPLACE, repartidos en ~800 llamadas
`.execute()` a lo largo de ~90 archivos, sin ORM. Reescribir cada una de esas
llamadas para MySQL habría sido inviable y de altísimo riesgo (datos reales
de estudiantes). En vez de eso, esta capa centraliza la traducción en un solo
lugar: cada archivo solo cambia CÓMO se abre la conexión
(`sqlite3.connect(DATABASE, ...)` → `db_compat.connect(DATABASE, ...)`); todo
lo demás (execute, executemany, row_factory, context manager, PRAGMA,
placeholders) sigue funcionando igual.

Motor activo:
  - ENGINE == "sqlite" (default): sin DATABASE_URL, o DATABASE_URL no apunta
    a MySQL. Usa sqlite3 real sin ninguna capa intermedia — cero cambio de
    comportamiento frente al código original (Render sigue así).
  - ENGINE == "mysql": DATABASE_URL empieza con mysql:// o mysql+pymysql://
    (Banahosting/cPanel). Envuelve PyMySQL para que el resto del código no
    note la diferencia.

Traducciones automáticas aplicadas SOLO bajo MySQL (ver _translate_sql):
  - Placeholders '?' → '%s' (paramstyle de PyMySQL).
  - 'INTEGER PRIMARY KEY AUTOINCREMENT' → 'INT AUTO_INCREMENT PRIMARY KEY'.
  - 'INSERT OR IGNORE INTO' → 'INSERT IGNORE INTO'.
  - 'INSERT OR REPLACE INTO' → 'REPLACE INTO'.
  - Cualquier PRAGMA se convierte en no-op (MySQL no los entiende y no hacen
    falta — WAL/journal_mode no aplican a un motor cliente-servidor).
  - "DEFAULT (date('now'))" → "DEFAULT (CURRENT_DATE)" (defaults de columna).
  - "date('now','-N days')" → "DATE_SUB(CURDATE(), INTERVAL N DAY)".
  - "date('now')" (uso suelto en INSERT/UPDATE/WHERE) → "CURDATE()".
  - "strftime('%Y-%m', x)" → "DATE_FORMAT(x, '%Y-%m')".
  - "strftime('%Y', x)" → "YEAR(x)".
  Estas 9 traducciones cubren el 100% de los usos reales encontrados en el
  repo (verificado con grep contra core/ y routes/, no es una traducción
  genérica especulativa) — no queda ninguna función de fecha SQLite sin
  traducir. GROUP_CONCAT(DISTINCT x) (única ocurrencia real, sin separador
  explícito) ya es sintaxis válida en MySQL tal cual, no necesita traducción.

NO se traduce automáticamente (fuera del alcance de esta capa, requiere
migración de datos real, no solo de sintaxis SQL):
  - julianday(...) — no hay usos reales en el repo actual, pero si se agrega
    en el futuro necesitará su propia traducción (equivalente aproximado:
    TO_DAYS() o DATEDIFF() en MySQL).
"""
import os
import re
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL", "")
ENGINE = "mysql" if DATABASE_URL.startswith(("mysql://", "mysql+pymysql://")) else "sqlite"

if ENGINE == "mysql":
    import pymysql
    import pymysql.cursors
    from urllib.parse import urlparse

_AUTOINCR_RE = re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.IGNORECASE)
_INSERT_OR_IGNORE_RE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)
_INSERT_OR_REPLACE_RE = re.compile(r"INSERT\s+OR\s+REPLACE\s+INTO", re.IGNORECASE)

# Funciones de fecha SQLite → equivalente MySQL. Verificado contra los ~30
# usos reales del repo (grep "date('now'\|strftime(" core/ routes/) — todas
# las ocurrencias reales calzan con uno de estos 4 patrones, no es una
# traducción genérica especulativa.
_DEFAULT_DATE_NOW_RE = re.compile(r"DEFAULT\s*\(\s*date\('now'\)\s*\)", re.IGNORECASE)
_DATE_NOW_MINUS_DAYS_RE = re.compile(r"date\(\s*'now'\s*,\s*'-(\d+)\s*days?'\s*\)", re.IGNORECASE)
_DATE_NOW_RE = re.compile(r"date\(\s*'now'\s*\)", re.IGNORECASE)
_STRFTIME_YM_RE = re.compile(r"strftime\(\s*'%Y-%m'\s*,\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)", re.IGNORECASE)
_STRFTIME_Y_RE = re.compile(r"strftime\(\s*'%Y'\s*,\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)", re.IGNORECASE)


def _translate_sql(sql: str) -> str:
    if ENGINE != "mysql":
        return sql
    sql = _AUTOINCR_RE.sub("INT AUTO_INCREMENT PRIMARY KEY", sql)
    sql = _INSERT_OR_IGNORE_RE.sub("INSERT IGNORE INTO", sql)
    sql = _INSERT_OR_REPLACE_RE.sub("REPLACE INTO", sql)
    sql = _DEFAULT_DATE_NOW_RE.sub("DEFAULT (CURRENT_DATE)", sql)
    sql = _DATE_NOW_MINUS_DAYS_RE.sub(r"DATE_SUB(CURDATE(), INTERVAL \1 DAY)", sql)
    sql = _DATE_NOW_RE.sub("CURDATE()", sql)
    sql = _STRFTIME_YM_RE.sub(r"DATE_FORMAT(\1, '%Y-%m')", sql)
    sql = _STRFTIME_Y_RE.sub(r"YEAR(\1)", sql)
    sql = sql.replace("?", "%s")
    return sql


class _MySQLRow(dict):
    """dict con acceso posicional best-effort, para el código que trata las
    filas como sqlite3.Row (acceso por índice ademas de por nombre)."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _MySQLCursor:
    def __init__(self, raw_cursor):
        self._c = raw_cursor
        self._pragma_noop = False

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith("PRAGMA"):
            self._pragma_noop = True
            return self
        self._pragma_noop = False
        self._c.execute(_translate_sql(sql), params)
        return self

    def executemany(self, sql, seq_of_params):
        self._pragma_noop = False
        self._c.executemany(_translate_sql(sql), seq_of_params)
        return self

    def fetchone(self):
        if self._pragma_noop:
            return None
        row = self._c.fetchone()
        return _MySQLRow(row) if row is not None else None

    def fetchall(self):
        if self._pragma_noop:
            return []
        return [_MySQLRow(r) for r in self._c.fetchall()]

    def fetchmany(self, size=None):
        if self._pragma_noop:
            return []
        rows = self._c.fetchmany(size) if size is not None else self._c.fetchmany()
        return [_MySQLRow(r) for r in rows]

    @property
    def lastrowid(self):
        return self._c.lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount

    def close(self):
        self._c.close()

    def __iter__(self):
        return iter(self.fetchall())


class _MySQLConnection:
    """Envuelve una conexión PyMySQL para exponer la misma superficie que
    sqlite3.Connection: execute/executemany/commit/cursor/row_factory/context
    manager. row_factory se acepta y se ignora (ya usamos DictCursor)."""

    def __init__(self, pymysql_conn):
        self._conn = pymysql_conn
        self.row_factory = None

    def cursor(self):
        return _MySQLCursor(self._conn.cursor())

    def execute(self, sql, params=()):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self.cursor()
        cur.executemany(sql, seq_of_params)
        return cur

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Mismo comportamiento que sqlite3.Connection: commit/rollback al
        # salir del bloque `with`, pero NO cierra la conexión (sqlite3
        # tampoco lo hace — quien la abrió es responsable de cerrarla).
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def _mysql_connect():
    url = DATABASE_URL.replace("mysql+pymysql://", "mysql://", 1)
    parsed = urlparse(url)
    conn = pymysql.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password or "",
        database=parsed.path.lstrip("/"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    return _MySQLConnection(conn)


def connect(database=None, timeout=10):
    """
    Reemplazo drop-in de sqlite3.connect(DATABASE, timeout=N).
      - ENGINE == "sqlite": conexión sqlite3 real, sin cambios de comportamiento.
      - ENGINE == "mysql": PyMySQL envuelto; `database`/`timeout` se ignoran
        (la conexión real ya queda definida por DATABASE_URL).
    """
    if ENGINE == "mysql":
        return _mysql_connect()
    if database is None:
        from .constants import DATABASE as _DEFAULT_DB
        database = _DEFAULT_DB
    return sqlite3.connect(database, timeout=timeout)
