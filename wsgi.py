"""
WSGI entry for Vercel
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

# Import Flask app
from app import app

# Vercel looks for `app` (lowercase) as the WSGI callable
# Flask's `app` is already a WSGI callable, no wrapper needed
