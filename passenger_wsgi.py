import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# app.py ya ejecuta migrar_bd() + _seed_admin() a nivel de módulo (mismo
# comportamiento que con gunicorn en Render — ver app.py líneas ~370-374),
# así que Passenger solo necesita importar la app ya lista.
from app import app as application
