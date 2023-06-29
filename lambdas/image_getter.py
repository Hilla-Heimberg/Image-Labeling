import boto3
from botocore.exceptions import ClientError
from models.processed_image import ProcessedImage, STATUS_CREATED,\
    STATUS_PROCESSING, STATUS_FAILURE, STATUS_SUCCESS
from pynamodb.exceptions import DoesNotExist
from typing import Dict, Any

s3_client = boto3.client("s3")

def handler(event: Dict[str, Any], _: Any) -> Dict[str, Any]:
    try:
        # Check that the request is a GET request
        if event["requestContext"]["http"]["method"] != "GET":
            return {"statusCode": 405}

        # Extract job ID from the event
        job_id = event["queryStringParameters"]["jobID"]

        # Check if the process succeeded
        entry = ProcessedImage.get(job_id)
        status = entry.status
        if status in [STATUS_CREATED, STATUS_PROCESSING, STATUS_FAILURE]:
            return {"status": status}
        elif status != STATUS_SUCCESS:
            return {"statusCode": 400}

        # Generate presigned URL with the job id in its metadata
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod="get_object",
            Params = {
                "Bucket": entry.processed_image_bucket,
                "Key": entry.processed_image_key
            }
        )

        output = {
            "url": presigned_url,
            "status": status
        }

        return output

    except (KeyError, TypeError, AttributeError):
        return {"statusCode": 400}

    except DoesNotExist:
        return {"statusCode": 404}

    except ClientError:
        return {"statusCode": 500}
