"""
WSGI entry for Vercel — direct FastAPI, no module-level init_db
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from app import app

# Vercel expects WSGI 'app'
try:
    from a2wsgi import ASGIMiddleware
    application = ASGIMiddleware(app)
    app = application
except ImportError:
    pass
