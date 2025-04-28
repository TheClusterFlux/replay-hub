from pymongo import MongoClient
from gridfs import GridFS
from bson.objectid import ObjectId
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

# Select collections based on environment
collection = db.test_data if IS_LOCAL else db.prod_data
comments_collection = db.comments
reactions_collection = db.reactions
logger.info(f"Using collection: {'test_data' if IS_LOCAL else 'prod_data'}")
logger.info("Initialized comments and reactions collections")

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

def get_single_document(query):
    """Fetch a single document from MongoDB."""
    logger.info(f"Fetching single document with query: {query}")
    # Convert string _id to ObjectId if needed
    if '_id' in query and isinstance(query['_id'], str):
        try:
            query['_id'] = ObjectId(query['_id'])
        except:
            # If conversion fails, keep it as is (might be a custom ID)
            pass
            
    doc = collection.find_one(query)
    
    # Convert ObjectId to string for JSON serialization
    if doc and '_id' in doc:
        doc['_id'] = str(doc['_id'])
        
    logger.info(f"Fetched document: {doc}")
    return doc

def update_db(query, update):
    """Update documents in MongoDB."""
    logger.info(f"Updating documents with query: {query}, update: {update}")
    
    # Convert string _id to ObjectId if needed
    if '_id' in query and isinstance(query['_id'], str):
        try:
            query['_id'] = ObjectId(query['_id'])
        except:
            # If conversion fails, keep it as is (might be a custom ID)
            pass
            
    result = collection.update_one(query, update)
    logger.info(f"Updated {result.modified_count} documents")
    return result.modified_count

def delete_from_db(filters):
    """Delete data from MongoDB based on filters."""
    logger.info(f"Deleting data with filters: {filters}")
    result = collection.delete_many(filters)
    logger.info(f"Deleted {result.deleted_count} documents from the collection")
    return result.deleted_count