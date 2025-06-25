from datetime import datetime, timedelta
import bcrypt
import jwt
from flask import current_app
from email_validator import validate_email, EmailNotValidError
import re
from .database import get_db
from bson import ObjectId

class User:
    def __init__(self, _id=None, username=None, email=None, password_hash=None, 
                 first_name='', last_name='', bio='', profile_picture=None, 
                 created_at=None, updated_at=None):
        self._id = _id
        self.username = username
        self.email = email
        self.password_hash = password_hash
        self.first_name = first_name
        self.last_name = last_name
        self.bio = bio
        self.profile_picture = profile_picture
        self.created_at = created_at or datetime.utcnow()
        self.updated_at = updated_at or datetime.utcnow()
    
    @staticmethod
    def validate_username(username):
        """Validate username format"""
        if not username or len(username) < 3 or len(username) > 30:
            return False, "Username must be between 3 and 30 characters"
        
        if not re.match(r'^[a-zA-Z0-9_-]+$', username):
            return False, "Username can only contain letters, numbers, underscores, and hyphens"
        
        return True, ""
    
    @staticmethod
    def validate_email(email):
        """Validate email format"""
        try:
            validate_email(email)
            return True, ""
        except EmailNotValidError as e:
            return False, str(e)
    
    @staticmethod
    def validate_password(password):
        """Validate password strength"""
        if not password or len(password) < 8:
            return False, "Password must be at least 8 characters long"
        
        return True, ""
    
    @staticmethod
    def hash_password(password):
        """Hash password using bcrypt"""
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    
    def check_password(self, password):
        """Check if provided password matches the hash"""
        if not self.password_hash:
            return False
        return bcrypt.checkpw(password.encode('utf-8'), self.password_hash.encode('utf-8'))
    
    def generate_token(self):
        """Generate JWT token for the user"""
        expiration = datetime.utcnow() + timedelta(hours=int(current_app.config.get('JWT_EXPIRATION_HOURS', 24)))
        
        payload = {
            'user_id': str(self._id),
            'username': self.username,
            'exp': expiration,
            'iat': datetime.utcnow()
        }
        
        return jwt.encode(payload, current_app.config['JWT_SECRET_KEY'], algorithm='HS256')
    
    def to_dict(self):
        """Convert user object to dictionary"""
        return {
            'id': str(self._id) if self._id else None,
            'username': self.username,
            'email': self.email,
            'first_name': self.first_name,
            'last_name': self.last_name,
            'display_name': f"{self.first_name} {self.last_name}".strip() or self.username,
            'bio': self.bio,
            'profile_picture': self.profile_picture,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    @classmethod
    def from_dict(cls, data):
        """Create User object from dictionary"""
        return cls(
            _id=data.get('_id'),
            username=data.get('username'),
            email=data.get('email'),
            password_hash=data.get('password_hash'),
            first_name=data.get('first_name', ''),
            last_name=data.get('last_name', ''),
            bio=data.get('bio', ''),
            profile_picture=data.get('profile_picture'),
            created_at=data.get('created_at'),
            updated_at=data.get('updated_at')
        )
    
    @staticmethod
    def create(username, email, password, first_name='', last_name='', bio='', profile_picture=None):
        """Create a new user"""
        try:
            # Validate inputs
            username_valid, username_error = User.validate_username(username)
            if not username_valid:
                current_app.logger.error(f"Username validation failed: {username_error}")
                return None
            
            email_valid, email_error = User.validate_email(email)
            if not email_valid:
                current_app.logger.error(f"Email validation failed: {email_error}")
                return None
            
            password_valid, password_error = User.validate_password(password)
            if not password_valid:
                current_app.logger.error(f"Password validation failed: {password_error}")
                return None
            
            # Check if user already exists
            db = get_db()
            if db.users.find_one({'username': username}):
                current_app.logger.error(f"Username already exists: {username}")
                return None
            
            if db.users.find_one({'email': email}):
                current_app.logger.error(f"Email already registered: {email}")
                return None
            
            # Create user document
            user_doc = {
                'username': username,
                'email': email.lower(),
                'password_hash': User.hash_password(password),
                'first_name': first_name,
                'last_name': last_name,
                'bio': bio,
                'profile_picture': profile_picture,
                'created_at': datetime.utcnow(),
                'updated_at': datetime.utcnow()
            }
            
            # Insert into database
            result = db.users.insert_one(user_doc)
            user_doc['_id'] = result.inserted_id
            
            # Create indexes if they don't exist
            try:
                db.users.create_index('username', unique=True)
                db.users.create_index('email', unique=True)
            except Exception as e:
                current_app.logger.warning(f"Index creation warning: {e}")
            
            return User.from_dict(user_doc)
            
        except Exception as e:
            current_app.logger.error(f"Error creating user: {e}")
            return None
    
    @staticmethod
    def get_by_id(user_id):
        """Get user by ID"""
        try:
            db = get_db()
            user_doc = db.users.find_one({'_id': ObjectId(user_id)})
            if user_doc:
                return User.from_dict(user_doc)
            return None
        except Exception as e:
            current_app.logger.error(f"Error getting user by ID: {e}")
            return None
    
    @staticmethod
    def get_by_username(username):
        """Get user by username"""
        try:
            db = get_db()
            user_doc = db.users.find_one({'username': username})
            if user_doc:
                return User.from_dict(user_doc)
            return None
        except Exception as e:
            current_app.logger.error(f"Error getting user by username: {e}")
            return None
    
    @staticmethod
    def get_by_email(email):
        """Get user by email"""
        try:
            db = get_db()
            user_doc = db.users.find_one({'email': email.lower()})
            if user_doc:
                return User.from_dict(user_doc)
            return None
        except Exception as e:
            current_app.logger.error(f"Error getting user by email: {e}")
            return None
    
    @staticmethod
    def authenticate(username, password):
        """Authenticate user with username/password"""
        try:
            # Try to find user by username or email
            user = User.get_by_username(username)
            if not user:
                user = User.get_by_email(username)  # Allow login with email
            
            if user and user.check_password(password):
                return user
            
            return None
        except Exception as e:
            current_app.logger.error(f"Authentication error: {e}")
            return None
    
    def update(self, first_name=None, last_name=None, bio=None, profile_picture=None):
        """Update user information"""
        try:
            db = get_db()
            
            update_data = {'updated_at': datetime.utcnow()}
            
            if first_name is not None:
                update_data['first_name'] = first_name
                self.first_name = first_name
            
            if last_name is not None:
                update_data['last_name'] = last_name
                self.last_name = last_name
            
            if bio is not None:
                update_data['bio'] = bio
                self.bio = bio
            
            if profile_picture is not None:
                update_data['profile_picture'] = profile_picture
                self.profile_picture = profile_picture
            
            # Update in database
            result = db.users.update_one(
                {'_id': self._id},
                {'$set': update_data}
            )
            
            if result.modified_count > 0:
                self.updated_at = update_data['updated_at']
                return self
            
            return None
            
        except Exception as e:
            current_app.logger.error(f"Error updating user: {e}")
            return None
    
    def change_password(self, new_password):
        """Change user password"""
        try:
            # Validate new password
            password_valid, password_error = User.validate_password(new_password)
            if not password_valid:
                current_app.logger.error(f"New password validation failed: {password_error}")
                return False
            
            # Hash new password
            new_password_hash = User.hash_password(new_password)
            
            # Update in database
            db = get_db()
            result = db.users.update_one(
                {'_id': self._id},
                {'$set': {
                    'password_hash': new_password_hash,
                    'updated_at': datetime.utcnow()
                }}
            )
            
            if result.modified_count > 0:
                self.password_hash = new_password_hash
                self.updated_at = datetime.utcnow()
                return True
            
            return False
            
        except Exception as e:
            current_app.logger.error(f"Error changing password: {e}")
            return False 