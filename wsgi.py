"""
WSGI entry point for Vercel
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

# Vercel — lazy init, 唔喺 module level call init_db
# 防止 import 時 connect DB 失敗
_initialized = False

from app import app

# Vercel expects WSGI 'app' — wrap FastAPI ASGI with a2wsgi
try:
    from a2wsgi import ASGIMiddleware
    application = ASGIMiddleware(app)
    app = application
except ImportError:
    pass
