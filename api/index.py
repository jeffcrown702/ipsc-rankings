"""Vercel Serverless entry point"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from core.database import init_db
init_db()

from app import app

# Vercel expects WSGI 'app'
from a2wsgi import ASGIMiddleware
app = ASGIMiddleware(app)
