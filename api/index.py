# api/index.py
from app import app
from flask import request, Response
import json

# Vercel expects a WSGI application
application = app

# Handler for Vercel serverless functions
def handler(event, context):
    # This allows Vercel to handle Flask app
    return application