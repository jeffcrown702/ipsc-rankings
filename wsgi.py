"""
WSGI entry point for PythonAnywhere
Convert FastAPI ASGI app to WSGI for PA's Apache setup
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from core.database import init_db
init_db()

import asyncio
from app import app

# Wrap FastAPI ASGI app as WSGI for PythonAnywhere
from a2wsgi import ASGIMiddleware
application = ASGIMiddleware(app)
