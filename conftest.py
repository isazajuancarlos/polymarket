"""Hace importable el paquete del proyecto al ejecutar pytest desde la raíz."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
