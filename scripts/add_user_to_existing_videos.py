#!/usr/bin/env python3
"""
Script to add user ownership to existing videos that were uploaded before the user system.
This will assign all existing videos to a specific user.
"""

import os
import sys
import getpass
from pymongo import MongoClient
from bson import ObjectId
from datetime import datetime

# Configuration
MONGO_HOST = "localhost"
MONGO_PORT = 27016
DATABASE_NAME = "replay_hub"

# User information to assign to existing videos
USER_ID = "685e14cbf7c5d352f5cfce1a"  # Your user ObjectId
USERNAME = "Ragian"
USER_EMAIL = "keanu.watts1@gmail.com"

def get_mongodb_credentials():
    """Get MongoDB credentials from user input"""
    print("🔐 MongoDB Authentication Required")
    print("-" * 40)
    
    username = input("MongoDB Username: ").strip()
    password = getpass.getpass("MongoDB Password: ").strip()
    
    # Ask for auth database (default to admin)
    auth_db = input("Authentication Database (default: admin): ").strip()
    if not auth_db:
        auth_db = "admin"
    
    if not username or not password:
        print("❌ Username and password are required")
        sys.exit(1)
    
    return username, password, auth_db

def connect_to_database():
    """Connect to MongoDB database"""
    try:
        # Get credentials
        username, password, auth_db = get_mongodb_credentials()
        
        print(f"\n🔌 Connecting to MongoDB...")
        
        # Try multiple connection methods
        connection_methods = [
            # Method 1: Connection string with authSource
            f"mongodb://{username}:{password}@{MONGO_HOST}:{MONGO_PORT}/{DATABASE_NAME}?authSource={auth_db}",
            # Method 2: Direct client with auth parameters
            None  # Will use MongoClient with separate auth params
        ]
        
        client = None
        for i, conn_string in enumerate(connection_methods):
            try:
                if conn_string:
                    print(f"  Trying connection method {i+1}: Connection string with authSource={auth_db}")
                    client = MongoClient(conn_string)
                else:
                    print(f"  Trying connection method {i+1}: Direct authentication")
                    client = MongoClient(
                        host=MONGO_HOST,
                        port=MONGO_PORT,
                        username=username,
                        password=password,
                        authSource=auth_db
                    )
                
                # Test connection
                client.admin.command('ping')
                print(f"  ✅ Connection successful!")
                break
                
            except Exception as method_error:
                print(f"  ❌ Method {i+1} failed: {method_error}")
                if client:
                    client.close()
                client = None
                continue
        
        if not client:
            raise Exception("All connection methods failed")
        
        db = client[DATABASE_NAME]
        
        print(f"✅ Connected to MongoDB at {MONGO_HOST}:{MONGO_PORT}")
        print(f"✅ Using database: {DATABASE_NAME}")
        print(f"✅ Authenticated as: {username} (authSource: {auth_db})")
        
        return db, client
        
    except Exception as e:
        print(f"❌ Failed to connect to MongoDB: {e}")
        print("💡 Common solutions:")
        print("   - Check username and password")
        print("   - Try 'admin' as authentication database")
        print("   - Try the target database name as authentication database")
        print("   - Ensure MongoDB is running and accessible")
        sys.exit(1)

def get_user_info(db, user_id):
    """Get user information from the database"""
    try:
        user = db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            print(f"❌ User with ID {user_id} not found in database")
            sys.exit(1)
        
        print(f"✅ Found user: {user.get('username')} ({user.get('email')})")
        return user
    except Exception as e:
        print(f"❌ Error retrieving user: {e}")
        sys.exit(1)

def find_videos_without_user(db):
    """Find videos that don't have user ownership information"""
    try:
        # Check both possible collections
        collections_to_check = ['prod_data', 'test_data']
        videos_found = []
        
        for collection_name in collections_to_check:
            if collection_name in db.list_collection_names():
                collection = db[collection_name]
                
                # Find videos without uploader_id or uploader_username
                query = {
                    "$or": [
                        {"uploader_id": {"$exists": False}},
                        {"uploader_id": None},
                        {"uploader_username": {"$exists": False}},
                        {"uploader_username": None}
                    ]
                }
                
                videos = list(collection.find(query))
                if videos:
                    videos_found.extend([(collection_name, video) for video in videos])
                    print(f"📹 Found {len(videos)} videos without user ownership in '{collection_name}' collection")
        
        if not videos_found:
            print("✅ All videos already have user ownership information")
            return []
        
        print(f"📊 Total videos to update: {len(videos_found)}")
        return videos_found
        
    except Exception as e:
        print(f"❌ Error finding videos: {e}")
        return []

def update_video_ownership(db, videos_to_update, user):
    """Update videos to add user ownership information"""
    try:
        updated_count = 0
        
        # Group videos by collection
        collections = {}
        for collection_name, video in videos_to_update:
            if collection_name not in collections:
                collections[collection_name] = []
            collections[collection_name].append(video)
        
        for collection_name, videos in collections.items():
            collection = db[collection_name]
            
            print(f"\n🔄 Updating {len(videos)} videos in '{collection_name}' collection...")
            
            for video in videos:
                try:
                    # Prepare update data
                    update_data = {
                        "uploader_id": user["_id"],
                        "uploader_username": user["username"],
                        "uploader": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user["username"]
                    }
                    
                    # Update the video
                    result = collection.update_one(
                        {"_id": video["_id"]},
                        {"$set": update_data}
                    )
                    
                    if result.modified_count > 0:
                        updated_count += 1
                        print(f"  ✅ Updated video: {video.get('title', 'Untitled')} (ID: {video['_id']})")
                    else:
                        print(f"  ⚠️  No changes made to video: {video.get('title', 'Untitled')} (ID: {video['_id']})")
                        
                except Exception as e:
                    print(f"  ❌ Failed to update video {video.get('_id')}: {e}")
        
        print(f"\n✅ Successfully updated {updated_count} videos")
        return updated_count
        
    except Exception as e:
        print(f"❌ Error updating videos: {e}")
        return 0

def main():
    """Main function"""
    print("🚀 Starting video ownership update script")
    print("=" * 50)
    
    # Connect to database
    db, client = connect_to_database()
    
    try:
        # Get user information
        print(f"\n👤 Retrieving user information for ID: {USER_ID}")
        user = get_user_info(db, USER_ID)
        
        # Find videos without user ownership
        print(f"\n🔍 Searching for videos without user ownership...")
        videos_to_update = find_videos_without_user(db)
        
        if not videos_to_update:
            print("\n🎉 No videos need updating!")
            return
        
        # Show summary and ask for confirmation
        print(f"\n📋 Summary:")
        print(f"   User: {user['username']} ({user['email']})")
        print(f"   Videos to update: {len(videos_to_update)}")
        
        # Ask for confirmation
        response = input(f"\n❓ Do you want to assign all {len(videos_to_update)} videos to user '{user['username']}'? (y/N): ")
        
        if response.lower() not in ['y', 'yes']:
            print("❌ Operation cancelled by user")
            return
        
        # Update videos
        print(f"\n🔄 Updating video ownership...")
        updated_count = update_video_ownership(db, videos_to_update, user)
        
        if updated_count > 0:
            print(f"\n🎉 Successfully updated {updated_count} videos!")
            print(f"   All videos are now owned by: {user['username']}")
        else:
            print(f"\n⚠️  No videos were updated")
            
    except KeyboardInterrupt:
        print(f"\n❌ Operation cancelled by user")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
    finally:
        # Close database connection
        client.close()
        print(f"\n🔌 Database connection closed")

if __name__ == "__main__":
    main() 