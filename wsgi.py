"""
WSGI entry point for PythonAnywhere
Simple synchronous wrapper around FastAPI
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from core.database import init_db
init_db()

from app import app

# PythonAnywhere Apache expects 'application' as WSGI callable
# FastAPI's app is ASGI, but we wrap it with a2wsgi
try:
    from a2wsgi import ASGIMiddleware
    application = ASGIMiddleware(app)
    # Vercel expects 'app'
    app = application
except ImportError:
    # Fallback: Starlette app works as ASGI only
    # This won't work on PythonAnywhere but prevents import error
    application = app
