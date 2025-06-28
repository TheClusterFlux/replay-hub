from flask import Blueprint, request, jsonify, current_app
from functools import wraps
import jwt
from datetime import datetime, timedelta
import os
from .models import User
from .utils import allowed_file
from .s3 import upload_to_s3
import tempfile

# Create authentication blueprint
auth_bp = Blueprint('auth', __name__)

def jwt_required(f):
    """Decorator to require JWT authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        auth_header = request.headers.get('Authorization')
        
        if auth_header:
            try:
                token = auth_header.split(' ')[1]  # Bearer <token>
            except IndexError:
                return jsonify({'error': 'Invalid authorization header format'}), 401
        
        if not token:
            return jsonify({'error': 'Authentication token is missing'}), 401
        
        try:
            data = jwt.decode(token, current_app.config['JWT_SECRET_KEY'], algorithms=['HS256'])
            current_user_id = data['user_id']
            current_user = User.get_by_id(current_user_id)
            if not current_user:
                return jsonify({'error': 'Invalid token - user not found'}), 401
            
            # Set current_user_id on request object for routes to access
            request.current_user_id = current_user_id
            request.current_user = current_user
            
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token has expired'}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Invalid token'}), 401
        
        return f(*args, **kwargs)
    
    return decorated

def optional_jwt(f):
    """Decorator for optional JWT authentication"""
    @wraps(f)
    def decorated(*args, **kwargs):
        current_user = None
        token = None
        auth_header = request.headers.get('Authorization')
        
        if auth_header:
            try:
                token = auth_header.split(' ')[1]  # Bearer <token>
                data = jwt.decode(token, current_app.config['JWT_SECRET_KEY'], algorithms=['HS256'])
                current_user_id = data['user_id']
                current_user = User.get_by_id(current_user_id)
            except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, IndexError):
                # Token is invalid but that's okay for optional auth
                pass
        
        return f(current_user, *args, **kwargs)
    
    return decorated

@auth_bp.route('/api/auth/register', methods=['POST'])
def register():
    """Register a new user"""
    try:
        # Handle multipart form data (for profile picture)
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        first_name = request.form.get('first_name', '')
        last_name = request.form.get('last_name', '')
        bio = request.form.get('bio', '')
        
        # Validate required fields
        if not username or not email or not password:
            return jsonify({'error': 'Username, email, and password are required'}), 400
        
        # Check if user already exists
        if User.get_by_username(username):
            return jsonify({'error': 'Username already exists'}), 400
        
        if User.get_by_email(email):
            return jsonify({'error': 'Email already registered'}), 400
        
        # Handle profile picture upload
        profile_picture_url = None
        if 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and file.filename != '' and allowed_file(file.filename):
                try:
                    # Save to temporary file
                    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as temp_file:
                        file.save(temp_file.name)
                        
                        # Upload to S3
                        s3_key = f"profile_pictures/{username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{os.path.splitext(file.filename)[1]}"
                        profile_picture_url = upload_to_s3(temp_file.name, s3_key)
                        
                        # Clean up temp file
                        os.unlink(temp_file.name)
                        
                except Exception as e:
                    current_app.logger.error(f"Profile picture upload failed: {e}")
                    return jsonify({'error': 'Failed to upload profile picture'}), 500
        
        # Create new user
        user_data = {
            'username': username,
            'email': email,
            'password': password,
            'first_name': first_name,
            'last_name': last_name,
            'bio': bio,
            'profile_picture': profile_picture_url
        }
        
        user = User.create(**user_data)
        if not user:
            return jsonify({'error': 'Failed to create user'}), 500
        
        # Generate JWT token
        token = user.generate_token()
        
        return jsonify({
            'message': 'User registered successfully',
            'user': user.to_dict(),
            'token': token
        }), 201
        
    except Exception as e:
        current_app.logger.error(f"Registration error: {e}")
        return jsonify({'error': 'Registration failed'}), 500

@auth_bp.route('/api/auth/login', methods=['POST'])
def login():
    """Authenticate user and return JWT token"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Request data is required'}), 400
        
        username = data.get('username')
        password = data.get('password')
        
        if not username or not password:
            return jsonify({'error': 'Username and password are required'}), 400
        
        # Authenticate user
        user = User.authenticate(username, password)
        if not user:
            return jsonify({'error': 'Invalid username or password'}), 401
        
        # Generate JWT token
        token = user.generate_token()
        
        return jsonify({
            'message': 'Login successful',
            'user': user.to_dict(),
            'token': token
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Login error: {e}")
        return jsonify({'error': 'Login failed'}), 500

@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout():
    """Logout user (client-side token removal)"""
    return jsonify({'message': 'Logout successful'}), 200

@auth_bp.route('/api/auth/me', methods=['GET'])
@jwt_required
def get_profile():
    """Get current user profile"""
    current_user = request.current_user
    return jsonify({'user': current_user.to_dict()}), 200

@auth_bp.route('/api/auth/me', methods=['PUT'])
@jwt_required
def update_profile():
    """Update current user profile"""
    try:
        current_user = request.current_user
        # Handle multipart form data (for profile picture)
        first_name = request.form.get('first_name', current_user.first_name)
        last_name = request.form.get('last_name', current_user.last_name)
        bio = request.form.get('bio', current_user.bio)
        
        # Handle profile picture upload
        profile_picture_url = current_user.profile_picture
        if 'profile_picture' in request.files:
            file = request.files['profile_picture']
            if file and file.filename != '' and allowed_file(file.filename):
                try:
                    # Save to temporary file
                    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as temp_file:
                        file.save(temp_file.name)
                        
                        # Upload to S3
                        s3_key = f"profile_pictures/{current_user.username}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{os.path.splitext(file.filename)[1]}"
                        profile_picture_url = upload_to_s3(temp_file.name, s3_key)
                        
                        # Clean up temp file
                        os.unlink(temp_file.name)
                        
                except Exception as e:
                    current_app.logger.error(f"Profile picture upload failed: {e}")
                    return jsonify({'error': 'Failed to upload profile picture'}), 500
        
        # Update user
        update_data = {
            'first_name': first_name,
            'last_name': last_name,
            'bio': bio,
            'profile_picture': profile_picture_url
        }
        
        updated_user = current_user.update(**update_data)
        if not updated_user:
            return jsonify({'error': 'Failed to update profile'}), 500
        
        return jsonify({
            'message': 'Profile updated successfully',
            'user': updated_user.to_dict()
        }), 200
        
    except Exception as e:
        current_app.logger.error(f"Profile update error: {e}")
        return jsonify({'error': 'Profile update failed'}), 500

@auth_bp.route('/api/auth/change-password', methods=['POST'])
@jwt_required
def change_password():
    """Change user password"""
    try:
        current_user = request.current_user
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Request data is required'}), 400
        
        current_password = data.get('current_password')
        new_password = data.get('new_password')
        
        if not current_password or not new_password:
            return jsonify({'error': 'Current password and new password are required'}), 400
        
        # Verify current password
        if not current_user.check_password(current_password):
            return jsonify({'error': 'Current password is incorrect'}), 400
        
        # Update password
        if not current_user.change_password(new_password):
            return jsonify({'error': 'Failed to change password'}), 500
        
        return jsonify({'message': 'Password changed successfully'}), 200
        
    except Exception as e:
        current_app.logger.error(f"Password change error: {e}")
        return jsonify({'error': 'Password change failed'}), 500

@auth_bp.route('/api/auth/verify-token', methods=['POST'])
def verify_token():
    """Verify JWT token validity"""
    try:
        token = None
        auth_header = request.headers.get('Authorization')
        
        if auth_header:
            try:
                token = auth_header.split(' ')[1]  # Bearer <token>
            except IndexError:
                return jsonify({'error': 'Invalid authorization header format'}), 401
        
        if not token:
            return jsonify({'error': 'Authentication token is missing'}), 401
        
        try:
            data = jwt.decode(token, current_app.config['JWT_SECRET_KEY'], algorithms=['HS256'])
            user_id = data['user_id']
            user = User.get_by_id(user_id)
            if not user:
                return jsonify({'error': 'Invalid token - user not found'}), 401
            
            return jsonify({
                'valid': True,
                'user': user.to_dict()
            }), 200
            
        except jwt.ExpiredSignatureError:
            return jsonify({'error': 'Token has expired', 'valid': False}), 401
        except jwt.InvalidTokenError:
            return jsonify({'error': 'Invalid token', 'valid': False}), 401
            
    except Exception as e:
        current_app.logger.error(f"Token verification error: {e}")
        return jsonify({'error': 'Token verification failed', 'valid': False}), 500 