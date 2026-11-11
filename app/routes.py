import os
from werkzeug.utils import secure_filename
import re
import json
import threading
from flask import request, jsonify, current_app
from bson.objectid import ObjectId
from pymongo.errors import DuplicateKeyError
from app import app, logger
from app.database import collection, save_to_db, fetch_from_db, fs, delete_from_db, update_db, get_single_document, get_db
from app.config import UPLOAD_FOLDER
from app.storage import upload_file as upload_to_storage
from app.storage import build_video_urls, video_storage_prefix
from app.encoding_queue import slots, reserve, release, accept_source, check_queue
from app.encoding_capacity import check_capacity, check_space, SERVER_LIMIT, MAX_CHUNK, MIB, RESERVE as DISK_RESERVE
from app.upload_cleanup import mark_deleted, clean_session
from app.url_import import download_url
import errno
from werkzeug.exceptions import BadRequest, HTTPException, ServiceUnavailable, RequestEntityTooLarge
from app.video_metadata import (
    format_video_document,
    generate_short_id,
)
from app.auth import auth_bp, jwt_required, optional_jwt
import uuid
from urllib.parse import urlsplit
import datetime
import tempfile
import filecmp
from pathlib import Path
import shutil
import random
import string
import time

# Authentication blueprint is registered in __init__.py

# Ensure the upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Allow room for multipart headers as well as the source file.
app.config['MAX_CONTENT_LENGTH'] = max(MAX_CHUNK, SERVER_LIMIT) + MIB

@app.before_request
def protect_upload_memory():
    if not request.path.startswith("/upload"):
        return
    if (request.path != "/upload/chunk" and
            (request.path != "/upload" or request.mimetype != "multipart/form-data") and
            (request.content_length or 0) > MIB):
        raise RequestEntityTooLarge("Upload metadata is too large.")
    request.max_form_memory_size = MIB


@app.route('/thumbnail/<thumbnail_id>', methods=['GET'])
def get_thumbnail(thumbnail_id):
    """Retrieve a thumbnail from GridFS."""
    try:
        thumbnail = fs.get(ObjectId(thumbnail_id))
        return app.response_class(thumbnail.read(), mimetype='image/jpeg', headers={
            'Cross-Origin-Resource-Policy': 'cross-origin',
            'Cache-Control': 'public, max-age=3600',
        })
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
        formatted_metadata = [format_video_document(item) for item in metadata]
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
        return jsonify(format_video_document(video)), 200
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
    

@app.errorhandler(HTTPException)
def http_error(error):
    return jsonify(success=False, error=error.description), error.code


@app.route('/upload/server-check', methods=['POST'])
@jwt_required
def server_check():
    data = request.get_json(silent=True) or {}
    check_capacity(data.get("size") if isinstance(data, dict) else None, app.config["UPLOAD_FOLDER"])
    check_queue()
    return jsonify(available=True)


@app.route('/upload', methods=['POST'])
@jwt_required
def upload_file():
    root = app.config["UPLOAD_FOLDER"]
    length = request.content_length
    if length is None:
        raise BadRequest("Provide the file size or use chunked upload.")
    if length and length > SERVER_LIMIT + MIB:
        raise RequestEntityTooLarge(f"The server accepts up to {SERVER_LIMIT // MIB} MiB. Trim or compress this video.")
    check_capacity(min(length or 1, SERVER_LIMIT) if SERVER_LIMIT else 1, root)
    ticket = reserve(uuid.uuid4().hex, request.current_user_id, length or 0)
    accepted = False
    custom_claim = None
    try:
        form = request.form.to_dict()
        url = form.get("url")
        file = request.files.get("file")
        if not url and not file:
            return jsonify(error="A video file or an approved media URL is required"), 400
        video_id = form.get("id") or str(uuid.uuid4())
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", video_id):
            return jsonify(error="Invalid video ID"), 400
        if form.get("id"):
            if get_single_document({"_id": video_id}):
                return jsonify(error="Video ID already exists"), 409
            try:
                get_db().hls_uploads.insert_one({"_id": video_id, "mode": "server", "updated": time.time(),
                                                 "collection": collection.name, "token": ticket["token"]})
            except DuplicateKeyError:
                return jsonify(error="Video ID already in use"), 409
            custom_claim = video_id
        form["id"] = video_id
        try:
            filename = urlsplit(url).path if url else file.filename
        except ValueError as exc:
            raise BadRequest("Invalid media URL.") from exc
        suffix = Path(secure_filename(filename)).suffix.lower() or ".mp4"
        with tempfile.TemporaryDirectory(prefix="receive-", dir=root) as work:
            path = Path(work) / ("source" + suffix)
            if url:
                # A remote stream may omit or misstate its length.
                allowance = min(SERVER_LIMIT, max(0, (shutil.disk_usage(root).free - DISK_RESERVE) // 3))
                download_url(url, path, allowance)
            else:
                file.save(path)
            check_capacity(path.stat().st_size, root)
            result = accept_source(ticket, path, form)
            accepted = True
            return jsonify(result), 202
    except OSError as error:
        raise ServiceUnavailable("The server could not stage this video. Check available disk space and retry.") from error
    finally:
        if custom_claim:
            get_db().hls_uploads.delete_one({"_id": custom_claim, "mode": "server", "token": ticket["token"]})
        if not accepted and not collection.find_one({"encoding_ticket": ticket["token"]}, {"_id": 1}):
            release(ticket)


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

def owns_video(video):
    owner_id = video.get('uploader_id') or video.get('user_id')
    if owner_id:
        return str(owner_id) == request.current_user_id
    return video.get('uploader') == request.current_user.username


@app.route('/thumbnail/<video_id>', methods=['POST'])
@jwt_required
def upload_thumbnail(video_id):
    """Upload a custom thumbnail for a video."""
    try:
        # Check if the video exists by _id or short_id
        video = get_single_document({"_id": video_id})
        if not video:
            video = get_single_document({"short_id": video_id})
        if not video:
            return jsonify({"success": False, "error": "Video not found", "code": 404}), 404
            
        if not owns_video(video):
            return jsonify(error="You can only change your own thumbnail"), 403

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
        
        if not owns_video(video):
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
        
        db = get_db()
        if not owns_video(video):
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
        
        # Record cleanup before removing the catalog entry so a crash cannot lose it.
        managed = video.get("storage_prefix") == video_storage_prefix(actual_id)
        if managed:
            mark_deleted(actual_id)
        query_id = actual_id
        if ObjectId.is_valid(actual_id) and not collection.find_one({"_id": actual_id}, {"_id": 1}):
            query_id = ObjectId(actual_id)
        collection.delete_one({"_id": query_id})
        
        if managed:
            try:
                clean_session(get_db().hls_uploads.find_one({"_id": actual_id}), time.time())
            except Exception:
                logger.exception("Media deletion queued for retry")

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
            s3_url = upload_to_storage(file_path, object_key=os.path.basename(file_path), content_type=content_type)
            
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

def chunk_directory(file_id):
    if not file_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", file_id):
        return None
    return Path(app.config["UPLOAD_FOLDER"]) / f"chunks_{request.current_user_id}_{file_id}"


@app.route('/upload/init', methods=['POST'])
@jwt_required
def init_chunked_upload():
    form = request.form
    file_id = form.get('fileId')
    directory = chunk_directory(file_id)
    size = form.get('fileSize', type=int)
    count = form.get('totalChunks', type=int)
    if (form.get('action') != 'init_chunked_upload' or not directory or not size or not count
            or size <= 0 or not 0 < count <= min(size, 100000)):
        return jsonify(error="Invalid chunked upload parameters"), 400
    check_capacity(size, app.config["UPLOAD_FOLDER"])
    destination = directory / "metadata.json"
    if destination.exists():
        previous = json.loads(destination.read_text())
        if not slots.find_one({"token": previous.get("ticket"), "state": "receiving"}):
            return jsonify(error="This upload has finished or expired. Start a new upload."), 409
    ticket = reserve(directory.name, request.current_user_id, size)
    slots.update_one({"token": ticket["token"]}, {"$set": {"local_dir": directory.name, "updated": time.time()}})
    metadata = form.to_dict()
    metadata.update(fileSize=size, totalChunks=count, ticket=ticket["token"])
    directory.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as tmp:
        json.dump(metadata, tmp)
    try:
        os.link(tmp.name, destination)
    except FileExistsError:
        if json.loads(destination.read_text()) != metadata:
            return jsonify(error="Upload ID already in use"), 409
    finally:
        os.unlink(tmp.name)
    return jsonify(success=True, fileId=file_id), 200


@app.route('/upload/chunk', methods=['POST'])
@jwt_required
def upload_chunk():
    if request.content_length is None or request.content_length > MAX_CHUNK + MIB:
        raise RequestEntityTooLarge("A chunk may contain at most 8 MiB.")
    file_id = request.form.get('fileId')
    directory = chunk_directory(file_id)
    if not directory or not (directory / "metadata.json").is_file():
        return jsonify(error="Upload not initialized"), 400
    metadata = json.loads((directory / "metadata.json").read_text())
    index = request.form.get('chunkIndex', type=int)
    count = metadata['totalChunks']
    file = request.files.get('file')
    if (not file or index is None or not 0 <= index < count
            or request.form.get('totalChunks', type=int) != count):
        return jsonify(error="Invalid chunk"), 400
    claim = {"token": metadata.get("ticket"), "state": "receiving"}
    if not slots.find_one(claim):
        return jsonify(error="Upload expired or already finished. Start a new upload."), 409
    check_space(directory, MAX_CHUNK)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp:
        path = Path(tmp.name)
    try:
        file.save(path)
        length = path.stat().st_size
        if not 0 < length <= min(MAX_CHUNK, metadata['fileSize']):
            return jsonify(error="Invalid chunk size"), 400
        part_key = f"parts.{index}"
        changed = slots.update_one({**claim, part_key: {"$exists": False},
                                    "$or": [{"received": {"$exists": False}},
                                            {"received": {"$lte": metadata["fileSize"] - length}}]},
                                   {"$inc": {"received": length}, "$set": {part_key: length, "updated": time.time()}})
        if not changed.matched_count and not slots.find_one({**claim, part_key: length}):
            return jsonify(error="Conflicting chunk or declared size exceeded"), 409
        target = directory / f"chunk_{index}"
        try:
            os.link(path, target)
        except FileExistsError:
            if not filecmp.cmp(path, target, shallow=False):
                return jsonify(error="Chunk already uploaded with different content"), 409
    finally:
        path.unlink(missing_ok=True)
    received = sum(1 for _ in directory.glob("chunk_*"))
    return jsonify(success=True, fileId=file_id, chunkIndex=index, chunksReceived=received, totalChunks=count)


@app.route('/upload/finalize', methods=['POST'])
@jwt_required
def finalize_upload():
    directory = chunk_directory(request.form.get('fileId'))
    if (request.form.get('action') != 'finalize_chunked_upload' or not directory
            or not (directory / "metadata.json").is_file()):
        return jsonify(error="Upload not initialized"), 400
    receipt = directory / "result.json"
    if receipt.exists():
        return jsonify(json.loads(receipt.read_text())), 202
    metadata = json.loads((directory / "metadata.json").read_text())
    token = metadata.get("ticket")
    if token:
        existing = collection.find_one({"_id": str(uuid.UUID(token)), "encoding_ticket": token})
        if existing:
            return jsonify(success=True, encoding_status=existing["encoding_status"],
                           metadata=format_video_document(existing)), 202
    # A process-owned lock is released by the OS if the uploader crashes.
    lock = (directory / "finalizing").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        if exc.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        return jsonify(error="Upload is already finalizing. Retry shortly or start a new upload."), 409

    source = None
    try:
        if receipt.exists():
            return jsonify(json.loads(receipt.read_text())), 202
        metadata = json.loads((directory / "metadata.json").read_text())
        chunks = [directory / f"chunk_{i}" for i in range(metadata["totalChunks"])]
        if (not all(path.is_file() for path in chunks)
                or sum(path.stat().st_size for path in chunks) != metadata["fileSize"]):
            return jsonify(error="Missing chunks or incorrect total size"), 400

        ticket = slots.find_one({"token": metadata.get("ticket"), "state": "receiving"})
        if not ticket:
            return jsonify(error="The upload expired. Start a new upload."), 409
        check_capacity(metadata["fileSize"], directory)
        video_id = str(uuid.UUID(ticket["token"]))
        ext = Path(secure_filename(metadata.get("filename", ""))).suffix or ".mp4"
        source = directory / ("assembled" + ext)
        with source.open("wb") as output:
            for path in chunks:
                with path.open("rb") as part:
                    shutil.copyfileobj(part, output)
        form_data = {key: request.form.get(key, metadata.get(key, ""))
                     for key in ("title", "description", "uploader", "players")}
        form_data["id"] = video_id
        result = accept_source(ticket, source, form_data)
        pending = directory / "result.tmp"
        pending.write_text(json.dumps(result))
        pending.replace(receipt)
        for chunk in chunks:
            chunk.unlink(missing_ok=True)
        return jsonify(result), 202
    except OSError as error:
        raise ServiceUnavailable("The server could not assemble this file. Retry later or choose a smaller clip.") from error
    except HTTPException as error:
        if error.code in (400, 413) and source:
            release(ticket)
            for chunk in chunks:
                chunk.unlink(missing_ok=True)
        raise
    finally:
        if source:
            source.unlink(missing_ok=True)
        lock.close()
