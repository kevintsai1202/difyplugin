import logging
import os
import requests
import traceback
from typing import Any, Optional, Dict
from dify_plugin.invocations.file import UploadFileResponse

logger = logging.getLogger(__name__)

class FileUploader:
    """
    A utility class to handle file uploads to Dify API
    """

    def __init__(
        self, session=None, dify_base_url="https://api.dify.ai/v1", dify_api_key=None
    ):
        """
        Initialize the FileUploader

        Args:
            session: The Dify plugin session object (if available)
            dify_base_url: The base URL for Dify API (if session not available)
            dify_api_key: The API key for Dify API (if session not available)
        """
        self.session = session
        self.dify_base_url = dify_base_url
        self.dify_api_key = dify_api_key

    def upload_file_via_session(
        self, filename: str, content: bytes, mimetype: str
    ) -> Optional[Dict[str, Any]]:
        """
        Upload a file using the Dify plugin session

        Args:
            filename: The name of the file
            content: The content of the file as bytes
            mimetype: The MIME type of the file

        Returns:
            Dictionary with file information or None if upload fails
        """
        try:
            logger.debug(
                f"Uploading file via session: {filename}, mimetype: {mimetype}, content size: {len(content)} bytes"
            )

            # Use a simple test file first to verify the API is working
            test_result = self.session.file.upload(
                filename="test.txt", content=b"test content", mimetype="text/plain"
            )
            logger.debug(f"Test upload result: {test_result}")

            # Now try the actual file
            storage_file = self.session.file.upload(
                filename=filename, content=content, mimetype=mimetype
            )

            logger.debug(f"Uploaded file to Dify storage: {storage_file}")

            if storage_file:
                # Convert to app parameter format
                return storage_file.to_app_parameter()
            return None
        except Exception as e:
            logger.debug(f"Error uploading file via session: {e}")
            traceback.print_exc()
            return None

    def upload_file_via_api(
        self, filename: str, content: bytes, mimetype: str
    ) -> Optional[Dict[str, Any]]:
        """
        Upload a file using direct API calls to Dify

        Args:
            filename: The name of the file
            content: The content of the file as bytes
            mimetype: The MIME type of the file

        Returns:
            Dictionary with file information or None if upload fails
        """
        if not self.dify_base_url or not self.dify_api_key:
            logger.debug(
                "Error: dify_base_url and dify_api_key must be provided for direct API upload"
            )
            return None

        try:
            logger.debug(
                f"Uploading file via API: {filename}, mimetype: {mimetype}, content size: {len(content)} bytes"
            )

            # Prepare the file upload endpoint
            upload_url = f"{self.dify_base_url}/files/upload"
            headers = {"Authorization": f"Bearer {self.dify_api_key}"}

            # Create a temporary file to upload
            temp_file_path = f"/tmp/{filename}"
            with open(temp_file_path, "wb") as f:
                f.write(content)

            # Upload the file
            files = {"file": (filename, open(temp_file_path, "rb"), mimetype)}

            response = requests.post(upload_url, headers=headers, files=files)

            # Clean up the temporary file
            os.remove(temp_file_path)

            if response.status_code == 201 or response.status_code == 200:
                result = response.json()
                logger.debug(f"File upload API response: {result}")

                # Format the response to match Dify plugin format
                if "id" in result:
                    return {
                        "id": result["id"],
                        "name": result.get("name", filename),
                        "size": result.get("size", len(content)),
                        "extension": result.get("extension", ""),
                        "mime_type": result.get("mime_type", mimetype),
                        "type": UploadFileResponse.Type.from_mime_type(mimetype).value,
                        "url": result.get("url", ""),
                    }
            else:
                logger.error(
                    f"File upload API error: {response.status_code}, {response.text}")

            return None
        except Exception as e:
            logger.error(f"Error uploading file via API: {e}")
            logger.error(traceback.format_exc())
            return None