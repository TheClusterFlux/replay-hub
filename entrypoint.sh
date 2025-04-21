#!/bin/bash
echo "Checking MoviePy installation..."
python -c "import sys; print(sys.path); from moviepy.editor import VideoFileClip; print('MoviePy imported successfully')"
echo "Starting main application..."
python main.py