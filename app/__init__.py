import secrets
import logging
import os
from flask import Flask
from pymongo import MongoClient
from gridfs import GridFS
from flask_cors import CORS  # Import CORS

# Initialize Flask app
app = Flask(__name__)

# Configure CORS for production
CORS(app, 
     origins=['*'],  # Allow all origins for now - can be restricted later
     allow_headers=['Content-Type', 'Authorization'],
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
     supports_credentials=True)

# Configure app
jwt_secret = os.environ.get('JWT_SECRET_KEY')
if not jwt_secret or jwt_secret in {'change-me', 'your-super-secret-jwt-key-change-this-in-production'}:
    logging.getLogger(__name__).warning("Set JWT_SECRET_KEY to keep sessions valid across workers and restarts")
    jwt_secret = secrets.token_hex(32)
app.config['JWT_SECRET_KEY'] = jwt_secret
app.config['JWT_EXPIRATION_HOURS'] = int(os.environ.get('JWT_EXPIRATION_HOURS', '24'))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Import and register authentication blueprint
from app.auth import auth_bp
app.register_blueprint(auth_bp)

# Add global CORS headers to all responses
@app.after_request
def after_request(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Max-Age'] = '86400'  # 24 hours
    return response

# Global OPTIONS handler for preflight requests
@app.before_request
def handle_preflight():
    from flask import request, jsonify
    if request.method == "OPTIONS":
        response = jsonify({'status': 'ok'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Max-Age'] = '86400'
        return response

# Import routes to register them with the app
from app import routes  # noqa: F401
from app import hls_routes  # noqa: F401
from app import stream_routes  # noqa: F401

@app.before_request
def start_wsgi_worker():
    if not app.testing:
        from app.encoding_queue import start_workers
        start_workers()


logger.info("Flask app initialized successfully")