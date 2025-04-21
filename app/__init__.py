import logging
from flask import Flask
from pymongo import MongoClient
from gridfs import GridFS
from flask_cors import CORS  # Import CORS

# Initialize Flask app
app = Flask(__name__)
# Enable CORS for all routes
CORS(app)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Import routes to register them with the app
from app import routes