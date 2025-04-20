# Use an official Python runtime as a parent image
FROM python:3.9-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

# Run main.py when the container launches
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8080", "main:app"]