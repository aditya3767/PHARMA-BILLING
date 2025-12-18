from app import app
import os
import sys

# Add project root to Python path
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

# Vercel expects a WSGI application
application = app

# For Vercel serverless functions
if __name__ == "__main__":
    app.run()