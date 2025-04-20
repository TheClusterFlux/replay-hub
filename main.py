import os
from pymongo import MongoClient

# Get MongoDB connection details from environment variables
MONGO_SERVICE_NAME = os.getenv("MONGO_SERVICE_NAME", "mongodb")
MONGO_NAMESPACE = os.getenv("MONGO_NAMESPACE", "default")
MONGO_PORT = int(os.getenv("MONGO_PORT", 27017))
MONGO_USERNAME = os.getenv("MONGO_USERNAME", "root")  # Default MongoDB root username
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD")  # Password from Kubernetes secret

# Construct the MongoDB connection string with authentication
mongo_uri = f"mongodb://{MONGO_USERNAME}:{MONGO_PASSWORD}@{MONGO_SERVICE_NAME}.{MONGO_NAMESPACE}.svc.cluster.local:{MONGO_PORT}"

# Connect to MongoDB
client = MongoClient(mongo_uri)

# Test the connection
try:
    db = client.admin
    server_status = db.command("serverStatus")
    print("Connected to MongoDB:", server_status)
    # run a test query
    test_db = client.test_database
    test_collection = test_db.test_collection
    test_collection.insert_one({"test": "data"})
    print("Inserted test data into MongoDB")
    # Clean up test data
    test_collection.delete_one({"test": "data"})
    print("Cleaned up test data from MongoDB")
except Exception as e:
    print("Failed to connect to MongoDB:", e)