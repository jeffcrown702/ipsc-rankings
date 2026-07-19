"""
WSGI entry for Vercel
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

# Import FastAPI app — module-level code handles Vercel guards
from app import app

# Wrap ASGI to WSGI for Vercel
try:
    from a2wsgi import ASGIMiddleware
    application = ASGIMiddleware(app)
    app = application
except ImportError:
    pass
