import logging
import os
from flask import Flask
from pymongo import MongoClient
from gridfs import GridFS
from flask_cors import CORS  # Import CORS

# Initialize Flask app
app = Flask(__name__)
# Enable CORS for all routes
CORS(app)

# Configure app
app.config['JWT_SECRET_KEY'] = os.environ.get('JWT_SECRET_KEY', 'your-super-secret-jwt-key-change-this-in-production')
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

# Import routes to register them with the app
from app import routes

logger.info("Flask app initialized successfully")