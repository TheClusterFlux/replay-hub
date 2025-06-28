import os
import threading
from flask import request, jsonify, current_app
from bson.objectid import ObjectId
from app import app, logger
from app.database import save_to_db, fetch_from_db, fs, delete_from_db, update_db, get_single_document, get_db
from app.utils import extract_video_metadata, schedule_delete, process_video_for_web_compatibility, process_video_async
from app.config import UPLOAD_FOLDER
from app.s3 import upload_to_s3
from app.auth import auth_bp, jwt_required, optional_jwt
import uuid
import requests
import datetime
import tempfile
import shutil
import random
import string

# Authentication blueprint is registered in __init__.py

# Ensure the upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Configure Flask for large file uploads (10GB max)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB in bytes

# Set streaming threshold to handle large files without loading them into memory
app.config['MAX_CONTENT_LENGTH_FOR_STREAMING'] = 10 * 1024 * 1024  # Use streaming for files larger than 10MB


@app.route('/thumbnail/<thumbnail_id>', methods=['GET'])
def get_thumbnail(thumbnail_id):
    """Retrieve a thumbnail from GridFS."""
    try:
        thumbnail = fs.get(ObjectId(thumbnail_id))
        return app.response_class(thumbnail.read(), mimetype='image/jpeg')
    except Exception as e:
        logger.error(f"Error fetching thumbnail: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/metadata', methods=['GET', 'OPTIONS'])
def get_metadata():
    """Retrieve metadata from MongoDB."""
    # Handle preflight requests
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'ok'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return response
    
    try:
        filters = request.args.to_dict()
        metadata = fetch_from_db(filters)
        # Transform the response to match the expected format in the API docs
        formatted_metadata = []
        for item in metadata:
            formatted_item = {
                "id": item.get("_id", str(uuid.uuid4())),
                "short_id": item.get("short_id", ""),
                "title": item.get("title", ""),
                "description": item.get("description", ""),
                "s3_url": item.get("s3_url", ""),
                "thumbnail_id": item.get("thumbnail_id", ""),
                "duration": item.get("duration", 0),
                "resolution": item.get("resolution", ""),
                "upload_date": item.get("upload_date", datetime.datetime.now().isoformat()),
                "uploader": item.get("uploader", "Anonymous"),
                "views": item.get("views", 0),
                "likes": item.get("likes", 0),
                "dislikes": item.get("dislikes", 0),
                "players": item.get("players", [])
            }
            formatted_metadata.append(formatted_item)
        logger.info(f"Fetched metadata: {formatted_metadata}")
        response = jsonify(formatted_metadata)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return response, 200
    except Exception as e:
        logger.error(f"Error fetching metadata: {e}")
        response = jsonify({"error": str(e)})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return response, 500

@app.route('/metadata/<video_id>', methods=['GET'])
def get_video_metadata(video_id):
    """Retrieve metadata for a specific video by _id, short_id, or legacy UUID."""
    try:
        # Try to find by _id first
        video = get_single_document({"_id": video_id})
        if not video:
            # Try to find by short_id
            video = get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"error": "Video not found"}), 404
        # Format the response to match API specs
        formatted_video = {
            "id": video.get("_id", video_id),
            "short_id": video.get("short_id", ""),
            "title": video.get("title", ""),
            "description": video.get("description", ""),
            "s3_url": video.get("s3_url", ""),
            "thumbnail_id": video.get("thumbnail_id", ""),
            "duration": video.get("duration", 0),
            "resolution": video.get("resolution", ""),
            "upload_date": video.get("upload_date", datetime.datetime.now().isoformat()),
            "uploader": video.get("uploader", "Anonymous"),
            "views": video.get("views", 0),
            "likes": video.get("likes", 0),
            "dislikes": video.get("dislikes", 0),
            "players": video.get("players", [])
        }
        return jsonify(formatted_video), 200
    except Exception as e:
        logger.error(f"Error fetching video metadata: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/metadata/<video_id>/view', methods=['POST'])
def increment_view_count(video_id):
    """Increment view count for a specific video by _id or short_id."""
    try:
        # Try to find by _id (ObjectId or string)
        video = get_single_document({"_id": video_id})
        if not video:
            # Try to find by short_id
            video = get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"success": False, "error": "Video not found"}), 404
        
        # Use the actual _id from the found video for updating
        actual_id = video.get("_id")
        if not actual_id:
            return jsonify({"success": False, "error": "Video has no _id"}), 500
            
        update_db({"_id": actual_id}, {"$inc": {"views": 1}})
        
        # Get updated view count
        updated_video = get_single_document({"_id": actual_id})
        current_views = updated_video.get("views", 0) if updated_video else 0
        
        return jsonify({
            "success": True,
            "videoId": actual_id,
            "views": current_views
        }), 200
    except Exception as e:
        logger.error(f"Error incrementing view count: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    

@app.route('/metadata', methods=['DELETE'])
def delete_metadata():
    """Delete metadata from MongoDB with optional filters."""
    try:
        filters = request.args.to_dict()
        deleted_count = delete_from_db(filters)
        return jsonify({"message": f"Deleted {deleted_count} documents"}), 200
    except Exception as e:
        logger.error(f"Error deleting metadata: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/upload', methods=['POST'])
@jwt_required
def upload_file():
    """Upload and save a video file."""
    try:
        save_to_s3 = True  
        if 'url' in request.form:
            file_path, internal_name = process_url_request(request.form['url'])
            file = None  # No file object for URL-based uploads
        else:
            # Validate and process the uploaded file
            file, file_path, internal_name, save_to_s3 = process_upload_request()

        # Process video for web compatibility (convert H.265 to H.264 if needed)
        logger.info(f"Processing video for web compatibility: {file_path}")
        processed_file_path = process_video_for_web_compatibility(file_path)
        
        # Update the file path if conversion occurred
        if processed_file_path != file_path:
            logger.info(f"Video converted from H.265 to H.264: {processed_file_path}")
            file_path = processed_file_path

        # Handle S3 or local saving (using the processed file)
        s3_url = handle_file_storage(file, file_path, save_to_s3)

        # Extract video metadata
        video_metadata = extract_video_metadata(file_path)

        # Save the thumbnail to GridFS and delete it locally
        thumbnail_id = save_thumbnail_to_gridfs(video_metadata, internal_name)

        # Combine metadata and save to MongoDB
        combined_metadata = combine_and_save_metadata(
            video_metadata, request.form.to_dict(), internal_name, thumbnail_id, s3_url
        )
        
        # Start async processing if we uploaded an H.265 file and it wasn't converted yet
        # This handles the case where async processing is enabled
        video_id = combined_metadata.get("_id")
        if video_id and os.path.exists(file_path):
            from app.utils import detect_video_codec, ASYNC_PROCESSING
            if ASYNC_PROCESSING:
                codec = detect_video_codec(file_path)
                if codec in ['hevc', 'h265']:
                    logger.info(f"Starting async H.265 processing for video {video_id}")
                    
                    # Create S3 re-upload callback
                    def s3_reupload_callback(converted_path):
                        return upload_to_s3(converted_path, content_type='video/mp4')
                    
                    process_video_async(file_path, video_id, s3_reupload_callback)
        
        # Now that all processing is done, schedule the local file for deletion
        if save_to_s3 and os.path.exists(file_path):
            logger.info(f"Processing complete, scheduling deletion of local file: {file_path}")
            schedule_delete(file_path, delay=3600)  # Delete after 1 hour
        
        # Format response to match API documentation
        response_data = {
            "success": True,
            "metadata": {
                "id": combined_metadata.get("_id", ""),
                "title": combined_metadata.get("title", ""),
                "description": combined_metadata.get("description", ""),
                "s3_url": combined_metadata.get("s3_url", ""),
                "thumbnail_id": combined_metadata.get("thumbnail_id", ""),
                "duration": combined_metadata.get("duration", 0),
                "resolution": combined_metadata.get("resolution", ""),
                "upload_date": combined_metadata.get("upload_date", datetime.datetime.now().isoformat()),
                "uploader": combined_metadata.get("uploader", "Anonymous")
            }
        }

        return jsonify(response_data), 201
    except Exception as e:
        logger.error(f"Error uploading file: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500
    
# Comments API endpoints
@app.route('/comments/<video_id>', methods=['GET'])
def get_comments(video_id):
    """Get all comments for a specific video."""
    try:
        from app.database import comments_collection
        
        # Get top-level comments
        top_comments = list(comments_collection.find({"videoId": video_id, "parent_comment_id": None}))
        
        # Process each comment to include replies
        result = []
        for comment in top_comments:
            comment_id = str(comment.get("_id"))
            
            # Convert ObjectId to string for JSON serialization
            comment["_id"] = comment_id
            
            # Get replies for this comment
            replies = list(comments_collection.find({"parent_comment_id": comment_id}))
            for reply in replies:
                reply["_id"] = str(reply.get("_id"))
            
            # Format the comment according to API docs
            formatted_comment = {
                "id": comment_id,
                "videoId": comment.get("videoId"),
                "userId": comment.get("userId"),
                "username": comment.get("username"),
                "text": comment.get("text"),
                "timestamp": comment.get("timestamp"),
                "likes": comment.get("likes", 0),
                "dislikes": comment.get("dislikes", 0),
                "replies": [{
                    "id": str(reply.get("_id")),
                    "userId": reply.get("userId"),
                    "username": reply.get("username"),
                    "text": reply.get("text"),
                    "timestamp": reply.get("timestamp"),
                    "likes": reply.get("likes", 0),
                    "dislikes": reply.get("dislikes", 0)
                } for reply in replies]
            }
            
            result.append(formatted_comment)
            
        return jsonify(result), 200
    except Exception as e:
        logger.error(f"Error fetching comments: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

@app.route('/comments', methods=['POST'])
def add_comment():
    """Add a new comment."""
    try:
        from app.database import comments_collection
        
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided", "code": 400}), 400
            
        # Validate required fields
        required_fields = ["videoId", "userId", "username", "text"]
        for field in required_fields:
            if field not in data:
                return jsonify({"success": False, "error": f"Missing required field: {field}", "code": 400}), 400
                
        # Create comment object with default values
        comment = {
            "videoId": data.get("videoId"),
            "userId": data.get("userId"),
            "username": data.get("username"),
            "text": data.get("text"),
            "timestamp": data.get("timestamp", datetime.datetime.now().isoformat()),
            "likes": 0,
            "dislikes": 0,
            "parent_comment_id": None  # This is a top-level comment
        }
        
        # Insert into database
        result = comments_collection.insert_one(comment)
        comment_id = str(result.inserted_id)
        
        # Format response
        response = {
            "id": comment_id,
            "videoId": comment.get("videoId"),
            "userId": comment.get("userId"),
            "username": comment.get("username"),
            "text": comment.get("text"),
            "timestamp": comment.get("timestamp"),
            "likes": 0,
            "dislikes": 0
        }
        
        return jsonify(response), 201
    except Exception as e:
        logger.error(f"Error adding comment: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

@app.route('/comments/<comment_id>/reply', methods=['POST'])
def add_reply(comment_id):
    """Add a reply to a comment."""
    try:
        from app.database import comments_collection
        
        # Check if parent comment exists
        parent = comments_collection.find_one({"_id": ObjectId(comment_id)})
        if not parent:
            return jsonify({"success": False, "error": "Parent comment not found", "code": 404}), 404
            
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided", "code": 400}), 400
            
        # Validate required fields
        required_fields = ["videoId", "userId", "username", "text"]
        for field in required_fields:
            if field not in data:
                return jsonify({"success": False, "error": f"Missing required field: {field}", "code": 400}), 400
                
        # Create reply object
        reply = {
            "videoId": data.get("videoId"),
            "userId": data.get("userId"),
            "username": data.get("username"),
            "text": data.get("text"),
            "timestamp": data.get("timestamp", datetime.datetime.now().isoformat()),
            "likes": 0,
            "dislikes": 0,
            "parent_comment_id": comment_id  # Link to parent comment
        }
        
        # Insert into database
        result = comments_collection.insert_one(reply)
        reply_id = str(result.inserted_id)
        
        # Format response
        response = {
            "id": reply_id,
            "videoId": reply.get("videoId"),
            "userId": reply.get("userId"),
            "username": reply.get("username"),
            "text": reply.get("text"),
            "timestamp": reply.get("timestamp"),
            "likes": 0,
            "dislikes": 0
        }
        
        return jsonify(response), 201
    except Exception as e:
        logger.error(f"Error adding reply: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

# Reactions API endpoints
@app.route('/reactions', methods=['POST'])
def add_reaction():
    """Add or update a reaction to a video."""
    try:
        from app.database import reactions_collection
        
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided", "code": 400}), 400
            
        # Validate required fields
        required_fields = ["videoId", "userId", "type"]
        for field in required_fields:
            if field not in data:
                return jsonify({"success": False, "error": f"Missing required field: {field}", "code": 400}), 400
                
        video_id = data.get("videoId")
        user_id = data.get("userId")
        reaction_type = data.get("type")
        
        # First check if the video exists before proceeding
        video = get_single_document({"_id": video_id})
        if not video:
            # Try to find by short_id
            video = get_single_document({"short_id": video_id})
            
        if not video:
            logger.error(f"Cannot add reaction: Video with id {video_id} not found")
            return jsonify({"success": False, "error": "Video not found", "code": 404}), 404
            
        # Get the actual _id for database operations
        actual_video_id = video.get("_id")
            
        # Validate reaction type
        if reaction_type not in ["like", "dislike", "none"]:
            return jsonify({"success": False, "error": "Invalid reaction type", "code": 400}), 400
            
        # Check if user already has a reaction
        existing_reaction = reactions_collection.find_one({
            "videoId": video_id,
            "userId": user_id,
            "commentId": None  # This is for a video, not a comment
        })
        
        # Process based on the reaction type
        if reaction_type == "none":
            # Remove reaction if it exists
            if existing_reaction:
                reactions_collection.delete_one({"_id": existing_reaction["_id"]})
                
                # Update video like/dislike counts
                old_type = existing_reaction.get("type")
                if old_type == "like":
                    update_db({"_id": actual_video_id}, {"$inc": {"likes": -1}})
                elif old_type == "dislike":
                    update_db({"_id": actual_video_id}, {"$inc": {"dislikes": -1}})
        else:
            # Add new reaction or update existing
            if existing_reaction:
                old_type = existing_reaction.get("type")
                
                # Only update if the reaction type is changing
                if old_type != reaction_type:
                    reactions_collection.update_one(
                        {"_id": existing_reaction["_id"]},
                        {"$set": {"type": reaction_type, "timestamp": data.get("timestamp", datetime.datetime.now().isoformat())}}
                    )
                    
                    # Update video metrics
                    if old_type == "like":
                        update_db({"_id": actual_video_id}, {"$inc": {"likes": -1}})
                    elif old_type == "dislike":
                        update_db({"_id": actual_video_id}, {"$inc": {"dislikes": -1}})
                        
                    if reaction_type == "like":
                        update_db({"_id": actual_video_id}, {"$inc": {"likes": 1}})
                    elif reaction_type == "dislike":
                        update_db({"_id": actual_video_id}, {"$inc": {"dislikes": 1}})
            else:
                # Create new reaction
                reaction = {
                    "videoId": video_id,
                    "userId": user_id,
                    "type": reaction_type,
                    "commentId": None,
                    "timestamp": data.get("timestamp", datetime.datetime.now().isoformat())
                }
                reactions_collection.insert_one(reaction)
                
                # Update video metrics
                if reaction_type == "like":
                    update_db({"_id": actual_video_id}, {"$inc": {"likes": 1}})
                elif reaction_type == "dislike":
                    update_db({"_id": actual_video_id}, {"$inc": {"dislikes": 1}})
        
        # Get current likes/dislikes count for the video
        updated_video = get_single_document({"_id": actual_video_id})
        
        if not updated_video:
            logger.error(f"Video not found after updating: {video_id}")
            # Since we already verified the video exists above, we'll use the original video object
            # to avoid a 500 error, even if it has outdated counts
            current_likes = video.get("likes", 0)
            current_dislikes = video.get("dislikes", 0)
        else:
            current_likes = updated_video.get("likes", 0)
            current_dislikes = updated_video.get("dislikes", 0)
        
        return jsonify({
            "success": True,
            "videoId": video_id,
            "currentLikes": current_likes,
            "currentDislikes": current_dislikes
        }, 200)
    except Exception as e:
        logger.error(f"Error adding reaction: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

@app.route('/comments/<comment_id>/reactions', methods=['POST'])
def add_comment_reaction(comment_id):
    """Add or update a reaction to a comment."""
    try:
        from app.database import reactions_collection, comments_collection
        
        # Check if comment exists
        comment = comments_collection.find_one({"_id": ObjectId(comment_id)})
        if not comment:
            return jsonify({"success": False, "error": "Comment not found", "code": 404}), 404
            
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided", "code": 400}), 400
            
        # Validate required fields
        required_fields = ["userId", "type"]
        for field in required_fields:
            if field not in data:
                return jsonify({"success": False, "error": f"Missing required field: {field}", "code": 400}), 400
                
        user_id = data.get("userId")
        reaction_type = data.get("type")
        
        # Validate reaction type
        if reaction_type not in ["like", "dislike", "none"]:
            return jsonify({"success": False, "error": "Invalid reaction type", "code": 400}), 400
            
        # Check if user already has a reaction to this comment
        existing_reaction = reactions_collection.find_one({
            "commentId": comment_id,
            "userId": user_id
        })
        
        # Process based on the reaction type
        if reaction_type == "none":
            # Remove reaction if it exists
            if existing_reaction:
                reactions_collection.delete_one({"_id": existing_reaction["_id"]})
                
                # Update comment like/dislike counts
                old_type = existing_reaction.get("type")
                if old_type == "like":
                    comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"likes": -1}})
                elif old_type == "dislike":
                    comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"dislikes": -1}})
        else:
            # Add new reaction or update existing
            if existing_reaction:
                old_type = existing_reaction.get("type")
                
                # Only update if the reaction type is changing
                if old_type != reaction_type:
                    reactions_collection.update_one(
                        {"_id": existing_reaction["_id"]},
                        {"$set": {"type": reaction_type, "timestamp": data.get("timestamp", datetime.datetime.now().isoformat())}}
                    )
                    
                    # Update comment metrics
                    if old_type == "like":
                        comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"likes": -1}})
                    elif old_type == "dislike":
                        comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"dislikes": -1}})
                        
                    if reaction_type == "like":
                        comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"likes": 1}})
                    elif reaction_type == "dislike":
                        comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"dislikes": 1}})
            else:
                # Create new reaction
                reaction = {
                    "commentId": comment_id,
                    "userId": user_id,
                    "type": reaction_type,
                    "videoId": None,
                    "timestamp": data.get("timestamp", datetime.datetime.now().isoformat())
                }
                reactions_collection.insert_one(reaction)
                
                # Update comment metrics
                if reaction_type == "like":
                    comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"likes": 1}})
                elif reaction_type == "dislike":
                    comments_collection.update_one({"_id": ObjectId(comment_id)}, {"$inc": {"dislikes": 1}})
        
        # Get current likes/dislikes count for the comment
        updated_comment = comments_collection.find_one({"_id": ObjectId(comment_id)})
        current_likes = updated_comment.get("likes", 0)
        current_dislikes = updated_comment.get("dislikes", 0)
        
        return jsonify({
            "success": True,
            "commentId": comment_id,
            "currentLikes": current_likes,
            "currentDislikes": current_dislikes
        }), 200
    except Exception as e:
        logger.error(f"Error adding comment reaction: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500

@app.route('/thumbnail/<video_id>', methods=['POST'])
def upload_thumbnail(video_id):
    """Upload a custom thumbnail for a video."""
    try:
        # Check if the video exists by _id or short_id
        video = get_single_document({"_id": video_id})
        if not video:
            video = get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"success": False, "error": "Video not found", "code": 404}), 404
            
        # Get the actual _id for database operations
        actual_video_id = video.get("_id")
            
        # Validate file upload
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "No file part in the request", "code": 400}), 400
            
        file = request.files['file']
        if file.filename == '':
            return jsonify({"success": False, "error": "No file selected", "code": 400}), 400
            
        # Save thumbnail to GridFS
        thumbnail_id = fs.put(file, filename=f"{video_id}_custom_thumbnail.jpg")
        
        # Update the video's thumbnail_id using the actual _id
        update_db({"_id": actual_video_id}, {"$set": {"thumbnail_id": str(thumbnail_id)}})
        
        # Generate the thumbnail URL
        thumbnail_url = f"{request.url_root.rstrip('/')}/thumbnail/{str(thumbnail_id)}"
        
        return jsonify({
            "success": True,
            "videoId": actual_video_id,
            "thumbnailId": str(thumbnail_id),
            "url": thumbnail_url
        }), 200
    except Exception as e:
        logger.error(f"Error uploading thumbnail: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500


@app.route('/api/user/saved-videos', methods=['POST'])
@jwt_required
def save_video():
    """Save or unsave a video for the current user."""
    try:
        current_user_id = request.current_user_id  # Set by @jwt_required decorator
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
        
        video_id = data.get('videoId')
        timestamp = data.get('timestamp', datetime.datetime.now().isoformat())
        
        if not video_id:
            return jsonify({"error": "Video ID is required"}), 400
        
        # Check if video exists
        video = get_single_document({"_id": video_id}) or get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"error": "Video not found"}), 404
        
        # Get saved videos collection using the correct database connection
        db = get_db()
        saved_videos_collection = db['saved_videos']
        
        # Check if video is already saved by this user
        existing_save = saved_videos_collection.find_one({
            "userId": current_user_id,
            "videoId": video_id
        })
        
        if existing_save:
            # Video is already saved - remove it (unsave)
            saved_videos_collection.delete_one({"_id": existing_save["_id"]})
            saved = False
            message = "Video removed from your watchlist"
        else:
            # Save the video
            save_data = {
                "_id": str(uuid.uuid4()),
                "userId": current_user_id,
                "videoId": video_id,
                "timestamp": timestamp
            }
            saved_videos_collection.insert_one(save_data)
            saved = True
            message = "Video saved to your watchlist"
        
        logger.info(f"User {current_user_id} {'saved' if saved else 'unsaved'} video {video_id}")
        
        return jsonify({
            "success": True,
            "saved": saved,
            "message": message,
            "videoId": video_id,
            "userId": current_user_id
        }), 200
        
    except Exception as e:
        logger.error(f"Error saving/unsaving video: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/user/saved-videos/<video_id>', methods=['GET'])
@jwt_required
def get_video_save_status(video_id):
    """Check if a video is saved by the current user."""
    try:
        current_user_id = request.current_user_id  # Set by @jwt_required decorator
        
        # Get saved videos collection using the correct database connection
        db = get_db()
        saved_videos_collection = db['saved_videos']
        
        # Check if video is saved by this user
        existing_save = saved_videos_collection.find_one({
            "userId": current_user_id,
            "videoId": video_id
        })
        
        return jsonify({
            "success": True,
            "saved": existing_save is not None,
            "videoId": video_id,
            "userId": current_user_id
        }), 200
        
    except Exception as e:
        logger.error(f"Error checking video save status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/user/saved-videos', methods=['GET'])
@jwt_required
def get_saved_videos():
    """Get all videos saved by the current user."""
    try:
        current_user_id = request.current_user_id  # Set by @jwt_required decorator
        
        # Get saved videos collection using the correct database connection
        db = get_db()
        saved_videos_collection = db['saved_videos']
        
        # Get all saved videos for this user
        saved_videos = list(saved_videos_collection.find({
            "userId": current_user_id
        }).sort("timestamp", -1))  # Most recent first
        
        # Get full video details for each saved video
        video_details = []
        for saved_video in saved_videos:
            video_id = saved_video['videoId']
            video = get_single_document({"_id": video_id}) or get_single_document({"short_id": video_id})
            
            if video:
                formatted_video = {
                    "id": video.get("_id", video_id),
                    "short_id": video.get("short_id", ""),
                    "title": video.get("title", ""),
                    "description": video.get("description", ""),
                    "s3_url": video.get("s3_url", ""),
                    "thumbnail_id": video.get("thumbnail_id", ""),
                    "duration": video.get("duration", 0),
                    "resolution": video.get("resolution", ""),
                    "upload_date": video.get("upload_date", datetime.datetime.now().isoformat()),
                    "uploader": video.get("uploader", "Anonymous"),
                    "views": video.get("views", 0),
                    "likes": video.get("likes", 0),
                    "dislikes": video.get("dislikes", 0),
                    "players": video.get("players", []),
                    "saved_timestamp": saved_video.get("timestamp")
                }
                video_details.append(formatted_video)
        
        return jsonify({
            "success": True,
            "videos": video_details,
            "count": len(video_details),
            "userId": current_user_id
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting saved videos: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/videos/<video_id>', methods=['PUT'])
@jwt_required
def update_video(video_id):
    """Update video metadata for the video owner."""
    try:
        current_user_id = request.current_user_id  # Set by @jwt_required decorator
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400
        
        # Find the video
        video = get_single_document({"_id": video_id}) or get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"error": "Video not found"}), 404
        
        # Check if current user is the owner
        video_uploader_id = video.get('user_id')  # If we store user IDs
        video_uploader_name = video.get('uploader')  # If we store usernames
        
        # Get current user details for comparison using the correct database connection
        db = get_db()
        users_collection = db['users']
        try:
            current_user = users_collection.find_one({"_id": ObjectId(current_user_id)})
        except Exception as e:
            logger.error(f"Invalid user ID format: {current_user_id}, error: {e}")
            return jsonify({"error": "Invalid user ID"}), 400
        
        if not current_user:
            return jsonify({"error": "User not found"}), 404
        
        # Check ownership
        is_owner = (
            video_uploader_id == current_user_id or
            video_uploader_name == current_user.get('username') or
            video_uploader_name == current_user.get('display_name')
        )
        
        if not is_owner:
            return jsonify({"error": "You can only edit your own videos"}), 403
        
        # Update allowed fields
        allowed_fields = ['title', 'description', 'players']
        update_data = {}
        
        for field in allowed_fields:
            if field in data:
                update_data[field] = data[field]
        
        if not update_data:
            return jsonify({"error": "No valid fields to update"}), 400
        
        # Update the video
        actual_id = video.get("_id")
        update_db({"_id": actual_id}, {"$set": update_data})
        
        logger.info(f"User {current_user_id} updated video {video_id} with fields: {list(update_data.keys())}")
        
        return jsonify({
            "success": True,
            "message": "Video updated successfully",
            "videoId": actual_id,
            "updatedFields": list(update_data.keys())
        }), 200
        
    except Exception as e:
        logger.error(f"Error updating video: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/videos/<video_id>', methods=['DELETE'])
@jwt_required
def delete_video(video_id):
    """Delete a video and all associated data."""
    try:
        current_user_id = request.current_user_id  # Set by @jwt_required decorator
        
        # Find the video
        video = get_single_document({"_id": video_id}) or get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"error": "Video not found"}), 404
        
        # Check if current user is the owner
        video_uploader_id = video.get('user_id')
        video_uploader_name = video.get('uploader')
        
        # Get current user details for comparison using the correct database connection
        db = get_db()
        users_collection = db['users']
        try:
            current_user = users_collection.find_one({"_id": ObjectId(current_user_id)})
        except Exception as e:
            logger.error(f"Invalid user ID format: {current_user_id}, error: {e}")
            return jsonify({"error": "Invalid user ID"}), 400
        
        if not current_user:
            return jsonify({"error": "User not found"}), 404
        
        # Check ownership
        is_owner = (
            video_uploader_id == current_user_id or
            video_uploader_name == current_user.get('username') or
            video_uploader_name == current_user.get('display_name')
        )
        
        if not is_owner:
            return jsonify({"error": "You can only delete your own videos"}), 403
        
        actual_id = video.get("_id")
        
        # Delete associated data using the correct database connection
        comments_collection = db['comments']
        reactions_collection = db['reactions']
        comment_reactions_collection = db['comment_reactions']
        saved_videos_collection = db['saved_videos']
        
        # Delete comments and their reactions
        comments = comments_collection.find({"videoId": actual_id})
        for comment in comments:
            comment_reactions_collection.delete_many({"commentId": comment["_id"]})
        comments_collection.delete_many({"videoId": actual_id})
        
        # Delete video reactions
        reactions_collection.delete_many({"videoId": actual_id})
        
        # Delete saved video references
        saved_videos_collection.delete_many({"videoId": actual_id})
        
        # Delete thumbnail from GridFS if it exists
        thumbnail_id = video.get('thumbnail_id')
        if thumbnail_id:
            try:
                fs.delete(ObjectId(thumbnail_id))
            except Exception as e:
                logger.warning(f"Could not delete thumbnail {thumbnail_id}: {e}")
        
        # Delete video metadata
        delete_from_db({"_id": actual_id})
        
        # TODO: Delete video file from S3 if needed
        # s3_url = video.get('s3_url')
        # if s3_url:
        #     delete_from_s3(s3_url)
        
        logger.info(f"User {current_user_id} deleted video {video_id}")
        
        return jsonify({
            "success": True,
            "message": "Video deleted successfully",
            "videoId": actual_id
        }), 200
        
    except Exception as e:
        logger.error(f"Error deleting video: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/comments/<comment_id>', methods=['DELETE'])
@jwt_required
def delete_comment(comment_id):
    """Delete a comment (only by video owner)."""
    try:
        current_user_id = request.current_user_id  # Set by @jwt_required decorator
        
        # Get the comment using the correct database connection
        db = get_db()
        comments_collection = db['comments']
        comment = comments_collection.find_one({"_id": comment_id})
        
        if not comment:
            return jsonify({"error": "Comment not found"}), 404
        
        # Get the video to check ownership
        video_id = comment.get('videoId')
        video = get_single_document({"_id": video_id}) or get_single_document({"short_id": video_id})
        
        if not video:
            return jsonify({"error": "Video not found"}), 404
        
        # Check if current user is the video owner
        video_uploader_id = video.get('user_id')
        video_uploader_name = video.get('uploader')
        
        # Get current user details for comparison using the correct database connection
        users_collection = db['users']
        try:
            current_user = users_collection.find_one({"_id": ObjectId(current_user_id)})
        except Exception as e:
            logger.error(f"Invalid user ID format: {current_user_id}, error: {e}")
            return jsonify({"error": "Invalid user ID"}), 400
        
        if not current_user:
            return jsonify({"error": "User not found"}), 404
        
        # Check ownership
        is_video_owner = (
            video_uploader_id == current_user_id or
            video_uploader_name == current_user.get('username') or
            video_uploader_name == current_user.get('display_name')
        )
        
        if not is_video_owner:
            return jsonify({"error": "Only video owners can delete comments"}), 403
        
        # Delete comment reactions using the correct database connection
        comment_reactions_collection = db['comment_reactions']
        comment_reactions_collection.delete_many({"commentId": comment_id})
        
        # Delete replies to this comment
        comments_collection.delete_many({"parentId": comment_id})
        
        # Delete the comment
        comments_collection.delete_one({"_id": comment_id})
        
        logger.info(f"Video owner {current_user_id} deleted comment {comment_id}")
        
        return jsonify({
            "success": True,
            "message": "Comment deleted successfully",
            "commentId": comment_id
        }), 200
        
    except Exception as e:
        logger.error(f"Error deleting comment: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/replies/<reply_id>', methods=['DELETE'])
@jwt_required
def delete_reply(reply_id):
    """Delete a reply (only by video owner)."""
    try:
        current_user_id = request.current_user_id  # Set by @jwt_required decorator
        
        # Get the reply using the correct database connection
        db = get_db()
        comments_collection = db['comments']
        reply = comments_collection.find_one({"_id": reply_id})
        
        if not reply:
            return jsonify({"error": "Reply not found"}), 404
        
        # Get the video to check ownership
        video_id = reply.get('videoId')
        video = get_single_document({"_id": video_id}) or get_single_document({"short_id": video_id})
        
        if not video:
            return jsonify({"error": "Video not found"}), 404
        
        # Check if current user is the video owner
        video_uploader_id = video.get('user_id')
        video_uploader_name = video.get('uploader')
        
        # Get current user details for comparison using the correct database connection
        users_collection = db['users']
        try:
            current_user = users_collection.find_one({"_id": ObjectId(current_user_id)})
        except Exception as e:
            logger.error(f"Invalid user ID format: {current_user_id}, error: {e}")
            return jsonify({"error": "Invalid user ID"}), 400
        
        if not current_user:
            return jsonify({"error": "User not found"}), 404
        
        # Check ownership
        is_video_owner = (
            video_uploader_id == current_user_id or
            video_uploader_name == current_user.get('username') or
            video_uploader_name == current_user.get('display_name')
        )
        
        if not is_video_owner:
            return jsonify({"error": "Only video owners can delete replies"}), 403
        
        # Delete reply reactions using the correct database connection
        comment_reactions_collection = db['comment_reactions']
        comment_reactions_collection.delete_many({"commentId": reply_id})
        
        # Delete the reply
        comments_collection.delete_one({"_id": reply_id})
        
        logger.info(f"Video owner {current_user_id} deleted reply {reply_id}")
        
        return jsonify({
            "success": True,
            "message": "Reply deleted successfully",
            "replyId": reply_id
        }), 200
        
    except Exception as e:
        logger.error(f"Error deleting reply: {e}")
        return jsonify({"error": str(e)}), 500

# Keep the existing code for process_upload_request, handle_file_storage, etc.

def process_url_request(url):
    """Process a video from a URL.
    
    :param url: URL of the video to download
    :return: tuple of (file_path, internal_name)
    """
    logger.info(f"Processing video from URL: {url}")
    
    # Generate a unique name for the file
    internal_name = str(uuid.uuid4())
    file_ext = url.split('.')[-1].split('?')[0] if '.' in url.split('/')[-1] else 'mp4'
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{internal_name}.{file_ext}")
    
    # Download the file in chunks to handle large files
    try:
        with requests.get(url, stream=True) as response:
            response.raise_for_status()
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
        logger.info(f"Successfully downloaded video from URL to: {file_path}")
        return file_path, internal_name
    except Exception as e:
        logger.error(f"Error downloading video from URL: {e}")
        raise

def process_upload_request():
    """Process an uploaded file from a multipart/form-data request.
    
    :return: tuple of (file, file_path, internal_name, save_to_s3)
    """
    logger.info("Processing file upload request")
    
    # Check if the post request has the file part
    if 'file' not in request.files:
        logger.error("No file part in the request")
        raise ValueError("No file part in the request")
        
    file = request.files['file']
    
    # If user does not select file, browser may submit an empty file without filename
    if file.filename == '':
        logger.error("No file selected for uploading")
        raise ValueError("No file selected for uploading")
        
    # Generate a unique name for the file
    internal_name = str(uuid.uuid4())
    file_ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else 'mp4'
    filename = f"{internal_name}.{file_ext}"
    file_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    
    # Check for streaming threshold
    content_length = request.content_length or 0
    if content_length > app.config.get('MAX_CONTENT_LENGTH_FOR_STREAMING', 1024 * 1024):
        logger.info(f"File size exceeds streaming threshold ({content_length} bytes). Using streaming upload.")
        
        # Stream the file to disk in chunks
        try:
            file.save(file_path)
            logger.info(f"Successfully saved large file to: {file_path}")
        except Exception as e:
            logger.error(f"Error saving large file: {e}")
            raise
    else:
        # For smaller files, use the standard approach
        try:
            file.save(file_path)
            logger.info(f"Successfully saved file to: {file_path}")
        except Exception as e:
            logger.error(f"Error saving file: {e}")
            raise
            
    # Determine if we should save to S3
    save_to_s3 = request.form.get('save_to_s3', 'true').lower() != 'false'
    
    return file, file_path, internal_name, save_to_s3

def handle_file_storage(file, file_path, save_to_s3=True):
    """Handle S3 or local storage for an uploaded file.
    
    :param file: The file object (may be None for URL-based uploads)
    :param file_path: Path where the file is temporarily stored
    :param save_to_s3: Whether to save to S3 (True) or keep locally (False)
    :return: URL where the file is accessible
    """
    logger.info(f"Handling file storage for: {file_path}, Save to S3: {save_to_s3}")
    
    try:
        if save_to_s3:
            # Determine content type based on file extension
            content_type = None
            if file_path:
                ext = os.path.splitext(file_path)[1].lower()
                if ext == '.mp4':
                    content_type = 'video/mp4'
                elif ext in ['.m3u8', '.m3u']:
                    content_type = 'application/x-mpegURL'
                elif ext == '.ts':
                    content_type = 'video/MP2T'
            
            # Upload to S3 directly from the file path (streaming)
            logger.info(f"Uploading file to S3: {file_path}")
            s3_url = upload_to_s3(file_path, content_type=content_type)
            
            if s3_url:
                logger.info(f"File uploaded successfully to S3. URL: {s3_url}")
                # Don't schedule deletion here - let the upload_file function handle this
                # after processing is complete
                return s3_url
            else:
                logger.error("Failed to upload to S3, falling back to local storage")
        
        # If not saving to S3 or S3 upload failed, return local URL
        filename = os.path.basename(file_path)
        local_url = f"/uploads/{filename}"
        logger.info(f"Using local storage. URL: {local_url}")
        return local_url
        
    except Exception as e:
        logger.error(f"Error handling file storage: {e}")
        raise

def save_thumbnail_to_gridfs(video_metadata, internal_name):
    """Save thumbnail to GridFS and return its ID.
    
    :param video_metadata: Metadata dict that contains the thumbnail path
    :param internal_name: Internal name of the video
    :return: ObjectId of the saved thumbnail
    """
    logger.info("Saving thumbnail to GridFS")
    
    thumbnail_path = video_metadata.get('thumbnail_path')
    if not thumbnail_path or not os.path.exists(thumbnail_path):
        logger.warning("No thumbnail available")
        return None
        
    try:
        with open(thumbnail_path, 'rb') as f:
            thumbnail_id = fs.put(f, filename=f"{internal_name}_thumbnail.jpg")
        logger.info(f"Thumbnail saved to GridFS with ID: {thumbnail_id}")
        
        # Delete local thumbnail file as it's now in GridFS
        os.remove(thumbnail_path)
        logger.info(f"Local thumbnail file deleted: {thumbnail_path}")
        
        return str(thumbnail_id)
    except Exception as e:
        logger.error(f"Error saving thumbnail to GridFS: {e}")
        return None

def combine_and_save_metadata(video_metadata, form_data, internal_name, thumbnail_id, s3_url):
    """Combine metadata from various sources and save to the database.
    
    :param video_metadata: Metadata extracted from the video file
    :param form_data: Metadata from the form submission
    :param internal_name: Internal name of the video
    :param thumbnail_id: ID of the thumbnail in GridFS
    :param s3_url: URL of the video in S3 or local storage
    :return: Combined metadata dict as saved to the database
    """
    logger.info("Combining and saving metadata")
    
    # Process players if provided
    players = []
    if form_data.get('players'):
        try:
            import json
            players = json.loads(form_data.get('players', '[]'))
        except Exception as e:
            logger.error(f"Error parsing players JSON: {e}")
    
    # Generate or use existing short_id
    short_id = form_data.get('short_id') or generate_short_id()
    
    # Get uploader information from authenticated user or form data
    uploader = form_data.get('uploader', 'Anonymous')
    uploader_id = None
    uploader_username = None
    
    # Check if user is authenticated (from request context)
    if hasattr(request, 'current_user') and request.current_user:
        user = request.current_user
        uploader_id = user.id
        uploader_username = user.username
        uploader = user.display_name or user.username
        logger.info(f"Video uploaded by authenticated user: {uploader_username}")
    else:
        logger.info("Video uploaded by anonymous user")
    
    # Create the metadata document
    metadata = {
        "_id": form_data.get('id', str(uuid.uuid4())),
        "short_id": short_id,
        "title": form_data.get('title', os.path.basename(video_metadata.get('file_path', ''))),
        "description": form_data.get('description', ''),
        "s3_url": s3_url,
        "internal_name": internal_name,
        "thumbnail_id": thumbnail_id,
        "duration": video_metadata.get('duration', 0),
        "resolution": video_metadata.get('resolution', ''),
        "upload_date": datetime.datetime.now().isoformat(),
        "uploader": uploader,
        "uploader_id": uploader_id,  # Store authenticated user ID
        "uploader_username": uploader_username,  # Store username for easy reference
        "views": 0,
        "likes": 0,
        "dislikes": 0,
        "players": players
    }
    
    # Save to database
    save_to_db(metadata)
    logger.info(f"Metadata saved to database with ID: {metadata['_id']}")
    
    return metadata

def generate_short_id(length=8):
    """Generate a short, unique, URL-safe ID."""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choices(chars, k=length))

@app.route('/upload/init', methods=['POST'])
@jwt_required
def init_chunked_upload():
    """Initialize a chunked upload session."""
    try:
        logger.info("Initializing chunked upload")
        
        if 'action' not in request.form or request.form.get('action') != 'init_chunked_upload':
            logger.error("Invalid action for chunked upload initialization")
            return jsonify({"success": False, "error": "Invalid action", "code": 400}), 400
            
        file_id = request.form.get('fileId')
        if not file_id:
            logger.error("No fileId provided for chunked upload")
            return jsonify({"success": False, "error": "No fileId provided", "code": 400}), 400
            
        # Create a temporary directory for this upload
        upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], f"chunks_{file_id}")
        os.makedirs(upload_dir, exist_ok=True)
        
        # Store basic metadata for this upload
        metadata = {
            "fileId": file_id,
            "filename": request.form.get('filename', ''),
            "fileSize": request.form.get('fileSize', '0'),
            "totalChunks": request.form.get('totalChunks', '0'),
            "chunksReceived": 0,
            "title": request.form.get('title', ''),
            "description": request.form.get('description', ''),
            "uploader": request.form.get('uploader', 'Anonymous'),
            "players": request.form.get('players', '[]'),
            "status": "initialized",
            "created_at": datetime.datetime.now().isoformat()
        }
        
        # Save metadata to a JSON file
        with open(os.path.join(upload_dir, "metadata.json"), 'w') as f:
            import json
            json.dump(metadata, f)
            
        logger.info(f"Chunked upload initialized with ID: {file_id}")
        
        return jsonify({
            "success": True,
            "fileId": file_id,
            "message": "Chunked upload initialized successfully"
        }), 200
        
    except Exception as e:
        logger.error(f"Error initializing chunked upload: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500
        
@app.route('/upload/chunk', methods=['POST'])
@jwt_required
def upload_chunk():
    """Handle uploading individual chunks of a file."""
    try:
        logger.info("Processing chunk upload")
        
        # Validate request
        if 'file' not in request.files:
            logger.error("No file part in chunk upload request")
            return jsonify({"success": False, "error": "No file part in the request", "code": 400}), 400
            
        file = request.files['file']
        if file.filename == '':
            logger.error("No file selected for chunk upload")
            return jsonify({"success": False, "error": "No file selected", "code": 400}), 400
            
        file_id = request.form.get('fileId')
        chunk_index = request.form.get('chunkIndex')
        total_chunks = request.form.get('totalChunks')
        
        if not file_id or not chunk_index or not total_chunks:
            logger.error("Missing required parameters for chunk upload")
            return jsonify({"success": False, "error": "Missing required parameters", "code": 400}), 400
            
        # Check if the upload was initialized
        upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], f"chunks_{file_id}")
        if not os.path.exists(upload_dir):
            logger.error(f"Upload directory not found for fileId: {file_id}")
            return jsonify({"success": False, "error": "Upload not initialized", "code": 400}), 400
            
        # Save this chunk to the upload directory
        chunk_path = os.path.join(upload_dir, f"chunk_{chunk_index}")
        file.save(chunk_path)
        
        # Update metadata
        import json
        metadata_path = os.path.join(upload_dir, "metadata.json")
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
            
        metadata["chunksReceived"] = metadata.get("chunksReceived", 0) + 1
        
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f)
            
        logger.info(f"Chunk {chunk_index} of {total_chunks} saved for file ID {file_id}")
        
        return jsonify({
            "success": True,
            "fileId": file_id,
            "chunkIndex": chunk_index,
            "chunksReceived": metadata["chunksReceived"],
            "totalChunks": total_chunks
        }), 200
        
    except Exception as e:
        logger.error(f"Error uploading chunk: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500
        
@app.route('/upload/finalize', methods=['POST'])
@jwt_required
def finalize_upload():
    """Finalize a chunked upload by combining all chunks and processing the complete file."""
    try:
        logger.info("Finalizing chunked upload")
        
        if 'action' not in request.form or request.form.get('action') != 'finalize_chunked_upload':
            logger.error("Invalid action for chunked upload finalization")
            return jsonify({"success": False, "error": "Invalid action", "code": 400}), 400
            
        file_id = request.form.get('fileId')
        if not file_id:
            logger.error("No fileId provided for chunked upload finalization")
            return jsonify({"success": False, "error": "No fileId provided", "code": 400}), 400
            
        # Check if the upload was initialized
        upload_dir = os.path.join(app.config["UPLOAD_FOLDER"], f"chunks_{file_id}")
        if not os.path.exists(upload_dir):
            logger.error(f"Upload directory not found for fileId: {file_id}")
            return jsonify({"success": False, "error": "Upload not initialized", "code": 400}), 400
            
        # Load metadata
        import json
        metadata_path = os.path.join(upload_dir, "metadata.json")
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
            
        # Check if all chunks have been received
        chunks_received = metadata.get("chunksReceived", 0)
        total_chunks = int(metadata.get("totalChunks", "0"))
        
        if chunks_received != total_chunks:
            logger.error(f"Not all chunks received. Got {chunks_received}/{total_chunks}")
            return jsonify({
                "success": False,
                "error": f"Not all chunks received. Got {chunks_received}/{total_chunks}",
                "code": 400
            }), 400
            
        # Generate a name for the complete file
        original_filename = request.form.get('filename', metadata.get("filename", ""))
        file_ext = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else 'mp4'
        internal_name = str(uuid.uuid4())
        complete_file_path = os.path.join(app.config["UPLOAD_FOLDER"], f"{internal_name}.{file_ext}")
        
        # Combine all chunks into the complete file
        with open(complete_file_path, 'wb') as outfile:
            for i in range(total_chunks):
                chunk_path = os.path.join(upload_dir, f"chunk_{i}")
                
                if not os.path.exists(chunk_path):
                    logger.error(f"Chunk {i} not found at {chunk_path}")
                    return jsonify({"success": False, "error": f"Chunk {i} not found", "code": 400}), 400
                
                with open(chunk_path, 'rb') as infile:
                    outfile.write(infile.read())
                    
        logger.info(f"All chunks combined into complete file: {complete_file_path}")
        
        # Update metadata status
        metadata["status"] = "combined"
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f)
            
        # Now process the complete file similar to the regular upload endpoint
        save_to_s3 = True
        
        # Process video for web compatibility (convert H.265 to H.264 if needed)
        logger.info(f"Processing chunked video for web compatibility: {complete_file_path}")
        processed_file_path = process_video_for_web_compatibility(complete_file_path)
        
        # Update the file path if conversion occurred
        if processed_file_path != complete_file_path:
            logger.info(f"Chunked video converted from H.265 to H.264: {processed_file_path}")
            complete_file_path = processed_file_path
        
        # Handle S3 or local saving
        s3_url = handle_file_storage(None, complete_file_path, save_to_s3)
        
        # Extract video metadata
        video_metadata = extract_video_metadata(complete_file_path)

        # Save the thumbnail to GridFS and delete it locally
        thumbnail_id = save_thumbnail_to_gridfs(video_metadata, internal_name)
        
        # Get form data from the original metadata
        form_data = {
            "title": request.form.get('title', metadata.get("title", "")),
            "description": request.form.get('description', metadata.get("description", "")),
            "uploader": request.form.get('uploader', metadata.get("uploader", "Anonymous")),
            "players": request.form.get('players', metadata.get("players", "[]"))
        }
        
        # Combine metadata and save to MongoDB
        combined_metadata = combine_and_save_metadata(
            video_metadata, form_data, internal_name, thumbnail_id, s3_url
        )
        
        # Start async processing for chunked uploads too
        video_id = combined_metadata.get("_id")
        if video_id and os.path.exists(complete_file_path):
            from app.utils import detect_video_codec, ASYNC_PROCESSING
            if ASYNC_PROCESSING:
                codec = detect_video_codec(complete_file_path)
                if codec in ['hevc', 'h265']:
                    logger.info(f"Starting async H.265 processing for chunked video {video_id}")
                    
                    # Create S3 re-upload callback
                    def s3_reupload_callback(converted_path):
                        return upload_to_s3(converted_path, content_type='video/mp4')
                    
                    process_video_async(complete_file_path, video_id, s3_reupload_callback)
        
        # Schedule deletion of the complete file now that processing is done
        if save_to_s3 and os.path.exists(complete_file_path):
            logger.info(f"Scheduling deletion of complete file: {complete_file_path}")
            schedule_delete(complete_file_path, delay=3600)  # Delete after 1 hour
            
        # Schedule deletion of the chunks directory
        logger.info(f"Scheduling deletion of chunks directory: {upload_dir}")
        schedule_delete(upload_dir, delay=3600)  # Delete after 1 hour
        
        # Format response to match API documentation
        response_data = {
            "success": True,
            "metadata": {
                "id": combined_metadata.get("_id", ""),
                "title": combined_metadata.get("title", ""),
                "description": combined_metadata.get("description", ""),
                "s3_url": combined_metadata.get("s3_url", ""),
                "thumbnail_id": combined_metadata.get("thumbnail_id", ""),
                "duration": combined_metadata.get("duration", 0),
                "resolution": combined_metadata.get("resolution", ""),
                "upload_date": combined_metadata.get("upload_date", datetime.datetime.now().isoformat()),
                "uploader": combined_metadata.get("uploader", "Anonymous")
            }
        }
        
        return jsonify(response_data), 201
        
    except Exception as e:
        logger.error(f"Error finalizing chunked upload: {e}")
        return jsonify({"success": False, "error": str(e), "code": 500}), 500