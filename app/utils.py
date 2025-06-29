import os
import time
import uuid
import threading
import subprocess
import tempfile
import shutil
from app import logger

def allowed_file(filename):
    """Check if the uploaded file has an allowed extension."""
    ALLOWED_EXTENSIONS = {
        # Video files
        'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv', 'wmv', 'm4v', '3gp',
        # Image files (for profile pictures)
        'png', 'jpg', 'jpeg', 'gif', 'webp'
    }
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Configuration
ENABLE_VIDEO_CONVERSION = os.getenv('ENABLE_VIDEO_CONVERSION', 'true').lower() == 'true'
CONVERSION_QUALITY = int(os.getenv('CONVERSION_QUALITY', '18'))  # CRF value (18 = visually lossless)
CONVERSION_PRESET = os.getenv('CONVERSION_PRESET', 'slow')  # slow preset for best quality
MAX_CONVERSION_TIME = int(os.getenv('MAX_CONVERSION_TIME', '36000'))  # seconds
ASYNC_PROCESSING = os.getenv('ASYNC_PROCESSING', 'true').lower() == 'true'
LOSSLESS_MODE = os.getenv('LOSSLESS_MODE', 'true').lower() == 'true'  # True lossless encoding
FAST_UPLOAD_MODE = os.getenv('FAST_UPLOAD_MODE', 'false').lower() == 'true'  # Skip metadata extraction for speed

def extract_video_metadata(file_path):
    """Extract metadata from a video file using ffmpeg for speed."""
    start_time = time.time()
    try:
        logger.info(f"🎬 Starting metadata extraction for: {file_path}")
        
        # Fast upload mode - return minimal metadata immediately
        if FAST_UPLOAD_MODE:
            elapsed = time.time() - start_time
            logger.info(f"⚡ FAST_UPLOAD_MODE enabled - returning minimal metadata (took {elapsed:.3f}s)")
            return {
                "duration": 0,
                "resolution": "unknown",
                "fps": 0,
                "file_path": file_path,
                "thumbnail_path": None
            }
        
        # Use ffmpeg for fast metadata extraction
        if not check_ffmpeg_available():
            elapsed = time.time() - start_time
            logger.warning(f"⚠️ FFmpeg not available - using basic metadata (took {elapsed:.3f}s)")
            return {
                "duration": 0,
                "resolution": "unknown",
                "fps": 0,
                "file_path": file_path,
                "thumbnail_path": None
            }
        
        # Get video info with ffprobe (much faster than moviepy)
        probe_start = time.time()
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', file_path
        ]
        
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            probe_elapsed = time.time() - probe_start
            logger.info(f"📊 ffprobe completed in {probe_elapsed:.3f}s")
            
            if result.returncode != 0:
                logger.error(f"ffprobe failed: {result.stderr}")
                raise Exception("ffprobe failed")
                
            import json
            info = json.loads(result.stdout)
            
            # Extract video stream info
            video_stream = None
            for stream in info.get('streams', []):
                if stream.get('codec_type') == 'video':
                    video_stream = stream
                    break
            
            if not video_stream:
                raise Exception("No video stream found")
            
            # Extract metadata
            duration = float(info.get('format', {}).get('duration', 0))
            width = video_stream.get('width', 0)
            height = video_stream.get('height', 0)
            fps = 0
            
            # Parse FPS
            fps_str = video_stream.get('r_frame_rate', '0/1')
            try:
                if '/' in fps_str:
                    num, den = fps_str.split('/')
                    fps = float(num) / float(den) if float(den) != 0 else 0
                else:
                    fps = float(fps_str)
            except:
                fps = 0
            
            metadata = {
                "duration": duration,
                "resolution": f"{width}x{height}",
                "fps": fps,
                "file_path": file_path
            }
            
            # Generate thumbnail with ffmpeg (much faster than moviepy)
            thumb_start = time.time()
            thumbnail_path = file_path + "_thumbnail.jpg"
            thumb_cmd = [
                'ffmpeg', '-i', file_path, '-ss', '00:00:01', '-vframes', '1',
                '-y', thumbnail_path
            ]
            
            try:
                thumb_result = subprocess.run(thumb_cmd, capture_output=True, text=True, timeout=10)
                thumb_elapsed = time.time() - thumb_start
                
                if thumb_result.returncode == 0 and os.path.exists(thumbnail_path):
                    metadata["thumbnail_path"] = thumbnail_path
                    logger.info(f"🖼️ Thumbnail generated in {thumb_elapsed:.3f}s: {thumbnail_path}")
                else:
                    logger.warning(f"⚠️ Thumbnail generation failed in {thumb_elapsed:.3f}s: {thumb_result.stderr}")
                    metadata["thumbnail_path"] = None
            except subprocess.TimeoutExpired:
                thumb_elapsed = time.time() - thumb_start
                logger.warning(f"⏰ Thumbnail generation timed out after {thumb_elapsed:.3f}s")
                metadata["thumbnail_path"] = None
            
            total_elapsed = time.time() - start_time
            logger.info(f"✅ Metadata extraction completed in {total_elapsed:.3f}s total (probe: {probe_elapsed:.3f}s, thumb: {thumb_elapsed:.3f}s)")
            return metadata
            
        except subprocess.TimeoutExpired:
            elapsed = time.time() - start_time
            logger.error(f"⏰ ffprobe timed out after {elapsed:.3f}s")
            raise Exception("Metadata extraction timed out")
            
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"❌ Error extracting metadata after {elapsed:.3f}s: {e}")
        # Fallback to basic metadata
        return {
            "duration": 0,
            "resolution": "unknown", 
            "fps": 0,
            "file_path": file_path,
            "thumbnail_path": None
        }

def check_ffmpeg_available():
    """Check if FFmpeg is available on the system."""
    try:
        result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return False

def detect_video_codec(file_path):
    """Detect video codec using ffprobe."""
    start_time = time.time()
    try:
        logger.info(f"🔍 Starting codec detection for: {file_path}")
        
        # First check if ffmpeg/ffprobe is available
        if not check_ffmpeg_available():
            elapsed = time.time() - start_time
            logger.warning(f"⚠️ FFmpeg not available - codec detection failed ({elapsed:.3f}s)")
            return "unknown"
            
        cmd = [
            'ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        elapsed = time.time() - start_time
        
        if result.returncode == 0:
            codec = result.stdout.strip().lower()
            logger.info(f"✅ Codec detected in {elapsed:.3f}s: '{codec}' for {file_path}")
            return codec
        else:
            logger.error(f"❌ ffprobe failed in {elapsed:.3f}s with return code {result.returncode}: {result.stderr}")
            return "unknown"
            
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start_time
        logger.error(f"⏰ Codec detection timed out after {elapsed:.3f}s for: {file_path}")
        return "unknown"
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"❌ Error detecting codec after {elapsed:.3f}s for {file_path}: {e}")
        return "unknown"

def get_video_info(file_path):
    """Get detailed video information including size and duration."""
    try:
        if not check_ffmpeg_available():
            return None
            
        cmd = [
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', '-show_streams', file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            import json
            info = json.loads(result.stdout)
            return info
        else:
            logger.error(f"ffprobe info failed: {result.stderr}")
            return None
            
    except Exception as e:
        logger.error(f"Error getting video info for {file_path}: {e}")
        return None

def convert_h265_to_h264(input_path):
    """Convert H.265/HEVC video to H.264 for browser compatibility with maximum quality preservation."""
    try:
        if not check_ffmpeg_available():
            logger.error("FFmpeg not available - cannot convert video")
            return None
            
        # Validate input file exists
        if not os.path.exists(input_path):
            logger.error(f"Input file does not exist: {input_path}")
            return None
            
        # Create output path
        base_name, ext = os.path.splitext(input_path)
        output_path = f"{base_name}_h264{ext}"
        
        # Make sure we don't overwrite an existing file
        counter = 1
        while os.path.exists(output_path):
            output_path = f"{base_name}_h264_{counter}{ext}"
            counter += 1
        
        logger.info(f"Converting H.265 to H.264: {input_path} -> {output_path}")
        
        # Determine quality settings based on mode
        if LOSSLESS_MODE:
            logger.info("Using LOSSLESS mode - true lossless H.264 encoding")
            quality_settings = ['-c:v', 'libx264', '-preset', 'veryslow', '-qp', '0']  # Lossless
        else:
            logger.info(f"Using high-quality mode - CRF {CONVERSION_QUALITY}, Preset: {CONVERSION_PRESET}")
            quality_settings = ['-c:v', 'libx264', '-preset', CONVERSION_PRESET, '-crf', str(CONVERSION_QUALITY)]
        
        # Get input file size for logging
        input_size = os.path.getsize(input_path)
        logger.info(f"Input file size: {input_size // 1024 // 1024}MB")
        
        # FFmpeg command for H.265 to H.264 conversion with maximum quality
        cmd = [
            'ffmpeg', '-i', input_path,
            *quality_settings,              # Quality settings (lossless or high-quality)
            '-c:a', 'copy',                 # Copy audio without re-encoding
            '-movflags', '+faststart',      # Optimize for web streaming
            '-y',                           # Overwrite output file
            output_path
        ]
        
        # Log the exact command for transparency
        logger.info(f"FFmpeg command: {' '.join(cmd)}")
        
        # Run conversion with configurable timeout
        logger.info("Starting H.265 to H.264 conversion...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_CONVERSION_TIME)
        
        if result.returncode == 0:
            # Validate output file was created and has content
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                logger.info(f"Successfully converted to H.264: {output_path}")
                
                # Get file sizes for logging
                original_size = input_size
                converted_size = os.path.getsize(output_path)
                size_diff = converted_size - original_size
                size_change_pct = (size_diff / original_size) * 100
                
                logger.info(f"Conversion complete - Original: {original_size//1024//1024}MB, "
                           f"Converted: {converted_size//1024//1024}MB, "
                           f"Change: {size_change_pct:+.1f}%")
                
                if LOSSLESS_MODE:
                    logger.info("Lossless conversion completed - no quality loss")
                else:
                    logger.info(f"High-quality conversion completed - CRF {CONVERSION_QUALITY} (visually lossless)")
                
                return output_path
            else:
                logger.error("Conversion appeared successful but output file is empty or missing")
                return None
        else:
            logger.error(f"FFmpeg conversion failed with return code {result.returncode}")
            logger.error(f"FFmpeg stderr: {result.stderr}")
            return None
            
    except subprocess.TimeoutExpired:
        logger.error(f"Conversion timeout ({MAX_CONVERSION_TIME}s) for: {input_path}")
        # Try to clean up partial output file
        if 'output_path' in locals() and os.path.exists(output_path):
            try:
                os.remove(output_path)
                logger.info(f"Cleaned up partial output file: {output_path}")
            except Exception:
                pass
        return None
    except Exception as e:
        logger.error(f"Error converting video {input_path}: {e}")
        return None

def process_video_async(file_path, video_id, s3_upload_callback=None):
    """
    Process video asynchronously in background thread.
    This allows upload response to return immediately.
    """
    def async_processing():
        try:
            logger.info(f"Starting async video processing for {video_id}: {file_path}")
            
            # Process video for web compatibility
            processed_path = process_video_for_web_compatibility_sync(file_path)
            
            if processed_path != file_path:
                logger.info(f"Async processing completed conversion: {processed_path}")
                
                # If we have a callback for S3 re-upload, call it
                if s3_upload_callback:
                    try:
                        new_s3_url = s3_upload_callback(processed_path)
                        if new_s3_url:
                            logger.info(f"Async S3 re-upload completed: {new_s3_url}")
                            
                            # Update database with new S3 URL
                            from app.database import update_db
                            update_db({"_id": video_id}, {"$set": {"s3_url": new_s3_url}})
                            logger.info(f"Database updated with new S3 URL for {video_id}")
                    except Exception as e:
                        logger.error(f"Error in async S3 re-upload: {e}")
                        
            logger.info(f"Async video processing completed for {video_id}")
            
        except Exception as e:
            logger.error(f"Error in async video processing for {video_id}: {e}")
    
    # Start background thread
    thread = threading.Thread(target=async_processing)
    thread.daemon = True
    thread.start()
    logger.info(f"Started async video processing thread for {video_id}")

def process_video_for_web_compatibility_sync(file_path):
    """
    Synchronous version of video processing (for async use).
    """
    try:
        # Check if video conversion is enabled
        if not ENABLE_VIDEO_CONVERSION:
            logger.info("Video conversion is disabled (ENABLE_VIDEO_CONVERSION=false)")
            return file_path
        
        # Validate input
        if not file_path or not os.path.exists(file_path):
            logger.error(f"Invalid file path for web compatibility processing: {file_path}")
            return file_path
            
        # Check file size (warn if very large)
        file_size = os.path.getsize(file_path)
        if file_size > 5 * 1024 * 1024 * 1024:  # 5GB
            logger.warning(f"Large file detected ({file_size//1024//1024//1024}GB) - conversion may take a long time")
        
        # Detect the video codec
        codec = detect_video_codec(file_path)
        logger.info(f"Video codec detection result: {codec}")
        
        # Check if conversion is needed
        if codec in ['hevc', 'h265']:
            logger.info(f"H.265/HEVC codec detected in {file_path}, converting to H.264...")
            
            # Convert to H.264
            converted_path = convert_h265_to_h264(file_path)
            
            if converted_path and os.path.exists(converted_path):
                # Verify the converted file is actually H.264
                new_codec = detect_video_codec(converted_path)
                if new_codec in ['h264', 'avc']:
                    logger.info(f"Video successfully converted to H.264: {converted_path}")
                    # Schedule deletion of the original H.265 file
                    schedule_delete(file_path, delay=300)  # Delete original after 5 minutes
                    return converted_path
                else:
                    logger.error(f"Conversion verification failed - codec is still: {new_codec}")
                    # Clean up failed conversion
                    if os.path.exists(converted_path):
                        try:
                            os.remove(converted_path)
                            logger.info(f"Cleaned up failed conversion file: {converted_path}")
                        except Exception as e:
                            logger.warning(f"Could not clean up failed conversion: {e}")
                    return file_path
            else:
                logger.error("H.265 to H.264 conversion failed, using original file")
                return file_path
                
        elif codec in ['h264', 'avc']:
            logger.info(f"Video already uses H.264 codec, no conversion needed: {file_path}")
            return file_path
        elif codec == 'unknown':
            logger.warning(f"Could not detect codec for {file_path}, assuming it's compatible")
            return file_path
        else:
            logger.warning(f"Unsupported or unknown codec '{codec}' for {file_path}, attempting to use as-is")
            return file_path
            
    except Exception as e:
        logger.error(f"Error processing video for web compatibility: {e}")
        return file_path  # Return original file if processing fails

def process_video_for_web_compatibility(file_path):
    """
    Process uploaded video to ensure web browser compatibility.
    Converts H.265/HEVC to H.264 if needed.
    Returns the path to the processed (web-compatible) video.
    
    If ASYNC_PROCESSING is enabled, this will return immediately for fast uploads,
    with conversion happening in background.
    """
    start_time = time.time()
    try:
        logger.info(f"🔄 Starting video compatibility processing: {file_path}")
        
        # Check if video conversion is enabled
        if not ENABLE_VIDEO_CONVERSION:
            elapsed = time.time() - start_time
            logger.info(f"🚫 Video conversion disabled - skipping (took {elapsed:.3f}s)")
            return file_path
        
        # Validate input
        if not file_path or not os.path.exists(file_path):
            elapsed = time.time() - start_time
            logger.error(f"❌ Invalid file path after {elapsed:.3f}s: {file_path}")
            return file_path
        
        # Get file size for optimization decisions
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)
        
        # For small files (< 10MB) with async processing enabled, skip codec detection
        # and return immediately for faster uploads
        if ASYNC_PROCESSING and file_size_mb < 10:
            elapsed = time.time() - start_time
            logger.info(f"⚡ Small file ({file_size_mb:.1f}MB) with async processing - skipping codec detection (took {elapsed:.3f}s)")
            return file_path
        
        # Quick codec detection only if we need it
        if not ASYNC_PROCESSING or file_size_mb >= 10:
            codec_start = time.time()
            codec = detect_video_codec(file_path)
            codec_elapsed = time.time() - codec_start
            logger.info(f"🔍 Codec detection completed in {codec_elapsed:.3f}s - result: {codec}")
            
            # If async processing is enabled and we detect H.265, return original file
            # and let async processing handle conversion
            if ASYNC_PROCESSING and codec in ['hevc', 'h265']:
                elapsed = time.time() - start_time
                logger.info(f"🎯 H.265 detected with async processing - returning original file for fast upload (took {elapsed:.3f}s)")
                return file_path
            
            # For non-async or non-H.265 files, use synchronous processing
            sync_start = time.time()
            result = process_video_for_web_compatibility_sync(file_path)
            sync_elapsed = time.time() - sync_start
            total_elapsed = time.time() - start_time
            logger.info(f"🔄 Synchronous processing completed in {sync_elapsed:.3f}s (total: {total_elapsed:.3f}s)")
            return result
        
        # Default case - return original file
        elapsed = time.time() - start_time
        logger.info(f"✅ Video processing completed - using original file (took {elapsed:.3f}s)")
        return file_path
            
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"❌ Error in video compatibility processing after {elapsed:.3f}s: {e}")
        return file_path  # Return original file if processing fails

def schedule_delete(file_path, delay):
    """Schedule a file for deletion after a delay using a background thread."""
    def delete_after_delay(path, wait_time):
        try:
            logger.info(f"Scheduled deletion of file: {path} in {wait_time} seconds")
            time.sleep(wait_time)
            
            # Handle both files and directories
            if os.path.exists(path):
                if os.path.isdir(path):
                    shutil.rmtree(path)
                    logger.info(f"Directory deleted: {path}")
                else:
                    os.remove(path)
                    logger.info(f"File deleted: {path}")
            else:
                logger.warning(f"Path not found for deletion: {path}")
        except Exception as e:
            logger.error(f"Error deleting path {path}: {e}")
    
    # Start a background thread that won't block the main process
    delete_thread = threading.Thread(target=delete_after_delay, args=(file_path, delay))
    delete_thread.daemon = True  # Daemon thread will be killed when main thread exits
    delete_thread.start()
    logger.info(f"Started background thread for path deletion: {file_path}")

# Log configuration on module load
logger.info("=" * 60)
logger.info("🚀 VIDEO PROCESSING CONFIGURATION")
logger.info("=" * 60)
logger.info(f"  📹 ENABLE_VIDEO_CONVERSION: {ENABLE_VIDEO_CONVERSION}")
logger.info(f"  ⚡ ASYNC_PROCESSING: {ASYNC_PROCESSING}")
logger.info(f"  🏃 FAST_UPLOAD_MODE: {FAST_UPLOAD_MODE}")
logger.info(f"  💎 LOSSLESS_MODE: {LOSSLESS_MODE}")
logger.info(f"  🎯 CONVERSION_QUALITY (CRF): {CONVERSION_QUALITY}")
logger.info(f"  ⚙️ CONVERSION_PRESET: {CONVERSION_PRESET}")
logger.info(f"  ⏰ MAX_CONVERSION_TIME: {MAX_CONVERSION_TIME}s")

if FAST_UPLOAD_MODE:
    logger.info("🔥 FAST_UPLOAD_MODE ENABLED - Ultra-fast uploads (minimal metadata)")
elif ASYNC_PROCESSING:
    logger.info("⚡ ASYNC_PROCESSING ENABLED - Fast uploads with background processing")
else:
    logger.info("🐌 SYNCHRONOUS MODE - Full processing during upload")

if LOSSLESS_MODE:
    logger.info("🎯 TRUE LOSSLESS MODE ENABLED - Zero quality loss guaranteed")
else:
    logger.info(f"🎯 HIGH QUALITY MODE - CRF {CONVERSION_QUALITY} (visually lossless)")
    
logger.info("=" * 60)