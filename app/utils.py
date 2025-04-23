import os
import time
import uuid
from moviepy.editor import VideoFileClip
from app import logger

def extract_video_metadata(file_path):
    """Extract metadata from a video file."""
    try:
        logger.info(f"Extracting metadata from video: {file_path}")
        clip = VideoFileClip(file_path)

        metadata = {
            "duration": clip.duration,
            "resolution": f"{clip.size[0]}x{clip.size[1]}",
            "fps": clip.fps
        }

        # Generate a thumbnail
        thumbnail_path = file_path + "_thumbnail.jpg"
        clip.save_frame(thumbnail_path, t=0)
        metadata["thumbnail"] = thumbnail_path

        logger.info(f"Extracted metadata: {metadata}")
        return metadata
    except Exception as e:
        logger.error(f"Error extracting metadata: {e}")
        raise
    finally:
        clip.close()
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.info(f"Deleted local video file: {file_path}")
            except Exception as e:
                logger.error(f"Failed to delete local video file: {file_path}. Error: {e}")

def schedule_delete(file_path, delay):
    """Schedule a file for deletion after a delay."""
    try:
        logger.info(f"Scheduling deletion of file: {file_path} in {delay} seconds")
        time.sleep(delay)
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info(f"File deleted: {file_path}")
        else:
            logger.warning(f"File not found for deletion: {file_path}")
    except Exception as e:
        logger.error(f"Error deleting file: {e}")