"""
WSGI entry point for Vercel
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from app import app

# Vercel expects WSGI 'app' — wrap FastAPI ASGI with a2wsgi
try:
    from a2wsgi import ASGIMiddleware
    application = ASGIMiddleware(app)
    app = application  # Vercel expects 'app'
except ImportError:
    # Fallback — won't work on PythonAnywhere WSGI but ok for Vercel ASGI
    pass
