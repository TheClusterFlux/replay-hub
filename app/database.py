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

def get_db():
    """Get the database instance for direct access."""
    return db

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
    query_copy = dict(query)
    
    # Handle _id queries - try both string and ObjectId formats
    if '_id' in query_copy and isinstance(query_copy['_id'], str):
        # First try with string ID (for UUIDs)
        doc = collection.find_one({'_id': query_copy['_id']})
        if doc:
            if '_id' in doc:
                doc['_id'] = str(doc['_id'])
            logger.info(f"Found document with string _id: {doc}")
            return doc
        
        # Then try converting to ObjectId (for MongoDB ObjectIds)
        try:
            query_copy['_id'] = ObjectId(query_copy['_id'])
            doc = collection.find_one(query_copy)
            if doc:
                if '_id' in doc:
                    doc['_id'] = str(doc['_id'])
                logger.info(f"Found document with ObjectId _id: {doc}")
                return doc
        except:
            logger.info(f"Could not convert _id to ObjectId: {query_copy['_id']}")
    
    # Handle other queries (including short_id)
    doc = collection.find_one(query_copy)
    if doc and '_id' in doc:
        doc['_id'] = str(doc['_id'])
    logger.info(f"Fetched document: {doc}")
    return doc

def update_db(query, update):
    """Update documents in MongoDB."""
    logger.info(f"Updating documents with query: {query}, update: {update}")
    
    # Handle string _id - try as string first, then as ObjectId if needed
    query_copy = dict(query)
    if '_id' in query_copy and isinstance(query_copy['_id'], str):
        # First try with string ID (for UUIDs)
        result = collection.update_one(query_copy, update)
        if result.matched_count > 0:
            logger.info(f"Updated {result.modified_count} documents with string _id")
            return result.modified_count
        
        # If no match, try converting to ObjectId
        try:
            query_copy['_id'] = ObjectId(query_copy['_id'])
            result = collection.update_one(query_copy, update)
            logger.info(f"Updated {result.modified_count} documents with ObjectId _id")
            return result.modified_count
        except:
            logger.info(f"Could not convert _id to ObjectId: {query['_id']}")
            return 0
    
    result = collection.update_one(query_copy, update)
    logger.info(f"Updated {result.modified_count} documents")
    return result.modified_count

def delete_from_db(filters):
    """Delete data from MongoDB based on filters."""
    logger.info(f"Deleting data with filters: {filters}")
    result = collection.delete_many(filters)
    logger.info(f"Deleted {result.deleted_count} documents from the collection")
    return result.deleted_count