from pymongo import MongoClient
from gridfs import GridFS
from app.config import MONGO_URI, IS_LOCAL
from app import logger

# Connect to MongoDB
try:
    client = MongoClient(MONGO_URI)
    db = client.replay_hub
    fs = GridFS(db)
    logger.info("Successfully connected to MongoDB")
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {e}")
    raise

# Select collection based on environment
collection = db.test_data if IS_LOCAL else db.prod_data
logger.info(f"Using collection: {'test_data' if IS_LOCAL else 'prod_data'}")

# Database utility functions
def save_to_db(data):
    """Save data to MongoDB."""
    logger.info(f"Saving data: {data}")
    result = collection.insert_one(data)
    logger.info(f"Data saved with ID: {result.inserted_id}")
    return str(result.inserted_id)

def fetch_from_db(query):
    """Fetch data from MongoDB."""
    logger.info(f"Fetching data with query: {query}")
    data = list(collection.find(query, {"_id": 0}))  # Exclude the MongoDB `_id` field
    logger.info(f"Fetched data: {data}")
    return data