# Replay Hub

Replay Hub is a Flask-based API service designed to interact with a MongoDB database. It provides endpoints to save and fetch data, making it suitable for applications requiring persistent storage and retrieval of structured data.

## Key Features

- **Flask API**:
  - `/save`: Accepts JSON data via a POST request and saves it to MongoDB.
  - `/fetch`: Retrieves all data from MongoDB via a GET request.
- **MongoDB Integration**:
  - Supports both local and Kubernetes-based MongoDB deployments.
  - Automatically detects the environment (local or cluster) using the `IS_LOCAL` environment variable and adjusts the connection settings accordingly.
- **Kubernetes Compatibility**:
  - Designed to run in a Kubernetes cluster with MongoDB as a service.
  - Includes support for Kubernetes secrets to securely manage MongoDB credentials.
- **Local Testing**:
  - Supports local testing by port-forwarding the MongoDB service from the Kubernetes cluster to the local machine.

## How to Use

### 1. Running Locally
To test the service locally:
1. Port-forward the MongoDB service from your Kubernetes cluster:
   ```bash
   kubectl port-forward svc/mongodb 27017:27017
   ```
2. Set the `IS_LOCAL` environment variable to `true`:
   - On Windows Command Prompt:
     ```cmd
     set IS_LOCAL=true
     python main.py
     ```
   - On Windows PowerShell:
     ```powershell
     $env:IS_LOCAL="true"
     python main.py
     ```
3. Start the Flask app:
   ```bash
   python main.py
   ```
4. Test the API using tools like Postman or `curl`:
   - Save data:
     ```bash
     curl -X POST -H "Content-Type: application/json" -d '{"key": "value"}' http://localhost:8080/save
     ```
   - Fetch data:
     ```bash
     curl http://localhost:8080/fetch
     ```

### 2. Running in Kubernetes
1. Deploy the service and MongoDB to your Kubernetes cluster using the provided YAML files or your own configurations.
2. Ensure MongoDB credentials are stored as Kubernetes secrets and injected into the environment variables.
3. Access the service via the cluster's ingress or service endpoint.

## API Endpoints

### `/upload` - Upload a Video File

This endpoint allows users to upload a video file, extract metadata (e.g., duration, resolution, FPS, and thumbnail), and save the file either locally or simulate saving it to S3 for testing purposes. If saved locally, the file is scheduled for deletion after 10 minutes.

#### **Request**
- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Parameters**:
  - `file` (required): The video file to upload.
  - `s3` (optional): A flag (`true` or `false`) indicating whether to save the file to S3. Defaults to `false`. If not provided or set to `false`, the file is saved locally and scheduled for deletion after 10 minutes.
  - Additional metadata fields can be passed as form-data (e.g., `title`, `description`).

#### **Response**
- **Status Code**: `201 Created` (on success)
- **Response Body**:
  ```json
  {
      "message": "File uploaded successfully",
      "file_path": "./uploads/<filename>.mp4",  // Only if saved locally
      "s3_url": "https://mock-s3-bucket/<filename>.mp4",  // Only if saved to S3
      "metadata": {
          "duration": 120.5,
          "resolution": "1920x1080",
          "fps": 30,
          "thumbnail": "./uploads/<filename>_thumbnail.jpg",
          "title": "My Video",
          "description": "Test video upload",
          "internal_name": "<uuid>",
          "thumbnail_id": "<thumbnail_id>",
          "s3_url": "https://mock-s3-bucket/<filename>.mp4"  // Only if saved to S3
      },
      "metadata_id": "<metadata_id>"
  }
  ```

#### **Examples**

1. **Save Locally**:
   ```bash
   curl -X POST -F "file=@path/to/video.mp4" -F "title=My Video" -F "description=Test video upload" http://localhost:8080/upload
   ```

   **Response**:
   ```json
   {
       "message": "File uploaded successfully",
       "file_path": "./uploads/<uuid>.mp4",
       "metadata": {
           "duration": 120.5,
           "resolution": "1920x1080",
           "fps": 30,
           "thumbnail": "./uploads/<uuid>_thumbnail.jpg",
           "title": "My Video",
           "description": "Test video upload",
           "internal_name": "<uuid>",
           "thumbnail_id": "<thumbnail_id>"
       },
       "metadata_id": "<metadata_id>"
   }
   ```

2. **Save to S3**:
   ```bash
   curl -X POST -F "file=@path/to/video.mp4" -F "s3=true" -F "title=My Video" -F "description=Test video upload" http://localhost:8080/upload
   ```

   **Response**:
   ```json
   {
       "message": "File uploaded successfully",
       "s3_url": "https://s3-bucket/<uuid>.mp4",
       "metadata": {
           "duration": 120.5,
           "resolution": "1920x1080",
           "fps": 30,
           "thumbnail": "./uploads/<uuid>_thumbnail.jpg",
           "title": "My Video",
           "description": "Test video upload",
           "internal_name": "<uuid>",
           "thumbnail_id": "<thumbnail_id>",
           "s3_url": "https://mock-s3-bucket/<uuid>.mp4"
       },
       "metadata_id": "<metadata_id>"
   }
   ```
### `/thumbnail/<thumbnail_id>` - Retrieve a Thumbnail Image

This endpoint retrieves a thumbnail image stored in a GridFS database using its unique `thumbnail_id`. The image is returned in `image/jpeg` format.

#### **Request**
- **Method**: `GET`
- **URL Parameters**:
   - `thumbnail_id` (required): The unique identifier of the thumbnail stored in GridFS. This should be a valid MongoDB ObjectId (e.g., `507f1f77bcf86cd799439011`).

#### **Response**
- **Status Code**: `200 OK` (on success)
- **Response Body**:
   - The thumbnail image in `image/jpeg` format.

- **Error (500)**: Returns a JSON object with an error message if the thumbnail cannot be retrieved:
   ```json
   {
         "error": "Failed to retrieve thumbnail",
         "details": "Invalid thumbnail_id or database error"
   }
   ```

#### **Examples**

1. **Retrieve Thumbnail**:
    ```bash
    curl http://localhost:8080/thumbnail/507f1f77bcf86cd799439011 --output thumbnail.jpg
    ```

    **Response**:
    - The thumbnail image is saved as `thumbnail.jpg`.

2. **Error Example**:
    ```bash
    curl http://localhost:8080/thumbnail/invalid_id
    ```

    **Response**:
    ```json
    {
          "error": "Failed to retrieve thumbnail",
          "details": "Invalid thumbnail_id or database error"
    }
    ```


#### **Notes**
- The `thumbnail` is saved to MongoDB's GridFS and its ID is included in the metadata.
- The `thumbnail` is saved to MongoDB's GridFS, a specification for storing and retrieving large files, and its ID is included in the metadata.


### `/metadata` - Retrieve Metadata Records

This endpoint retrieves metadata records stored in the MongoDB database. Users can apply filters using query parameters to narrow down the results.

#### **Request**
- **Method**: `GET`
- **Query Parameters**:
   - Any field present in the metadata schema can be used as a filter. For example:
      - `title`: Filter by the title of the metadata.
      - `description`: Filter by the description of the metadata.
      - Custom fields added to the metadata can also be used as filters.

#### **Response**
- **Status Code**: `200 OK` (on success)
- **Response Body**:
   - A JSON object containing an array of metadata records:
      ```json
      {
            "metadata": [
                  {
                        "title": "My Video",
                        "description": "Test video upload",
                        "duration": 120.5,
                        "resolution": "1920x1080",
                        "fps": 30,
                        "thumbnail": "./uploads/<uuid>_thumbnail.jpg",
                        "internal_name": "<uuid>",
                        "thumbnail_id": "<thumbnail_id>",
                        "s3_url": "https://mock-s3-bucket/<uuid>.mp4"
                  },
                  ...
            ]
      }
      ```

- **Error (500)**: Returns a JSON object with an error message if the metadata cannot be retrieved:
   ```json
   {
         "error": "Error fetching metadata",
         "details": "<error details>"
   }
   ```

#### **Examples**

1. **Retrieve All Metadata**:
    ```bash
    curl http://localhost:8080/metadata
    ```

    **Response**:
    ```json
    {
          "metadata": [
                {
                      "title": "My Video",
                      "description": "Test video upload",
                      "duration": 120.5,
                      "resolution": "1920x1080",
                      "fps": 30,
                      "thumbnail": "./uploads/<uuid>_thumbnail.jpg",
                      "internal_name": "<uuid>",
                      "thumbnail_id": "<thumbnail_id>",
                      "s3_url": "https://mock-s3-bucket/<uuid>.mp4"
                }
          ]
    }
    ```

2. **Retrieve Metadata with Filters**:
    ```bash
    curl "http://localhost:8080/metadata?title=My%20Video"
    ```

    **Response**:
    ```json
    {
          "metadata": [
                {
                      "title": "My Video",
                      "description": "Test video upload",
                      "duration": 120.5,
                      "resolution": "1920x1080",
                      "fps": 30,
                      "thumbnail": "./uploads/<uuid>_thumbnail.jpg",
                      "internal_name": "<uuid>",
                      "thumbnail_id": "<thumbnail_id>",
                      "s3_url": "https://mock-s3-bucket/<uuid>.mp4"
                }
          ]
    }
    ```

3. **Error Example**:
    ```bash
    curl "http://localhost:8080/metadata?invalid_field=value"
    ```

    **Response**:
    ```json
    {
          "error": "Error fetching metadata",
          "details": "Invalid query parameter or database error"
    }
    ```

#### **Notes**
- Query parameters are optional. If no filters are provided, all metadata records are returned.
- The `_id` field from MongoDB is excluded in the response for simplicity.
- Ensure that the query parameters match the metadata schema fields to avoid errors.