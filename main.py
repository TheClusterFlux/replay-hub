from app import app, logger

if __name__ == '__main__':
    logger.info("Starting Flask app on 0.0.0.0:8080")
    app.run(host='0.0.0.0', port=8080)