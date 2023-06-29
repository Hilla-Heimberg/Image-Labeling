import boto3
from botocore.exceptions import ClientError
import json
from models.processed_image import ProcessedImage, STATUS_CREATED
import os
from typing import Dict, Any
import uuid

IMAGES_BUCKET_NAME = os.environ["IMAGES_BUCKET_NAME"]
IDENTIFY_CELEBRITIES_VALUES = ["false", "true"]

s3_client = boto3.client("s3")

def handler(event: Dict[str, Any], _: Any) -> Dict[str, Any]:

    try:
        # Check that the request is a POST request
        if event["requestContext"]["http"]["method"] != "POST":
            return {"statusCode": 405}

        # Generate a unique job ID
        job_id = str(uuid.uuid4())

        file_name = json.loads(event["body"])["fileName"]

        # Bonus part - should identify celebrities in the picture or not
        if "identifyCelebrities" in json.loads(event["body"]):
            identify_celebrities = json.loads(event["body"])["identifyCelebrities"]
            if (identify_celebrities not in IDENTIFY_CELEBRITIES_VALUES):
                raise TypeError
        else:
            identify_celebrities = "false"

        # Generate presigned URL with the job id in its metadata
        presigned_url = s3_client.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": IMAGES_BUCKET_NAME,
                "Key": file_name,
                "ContentType": "image/jpeg",
                "Metadata": {"job_id": job_id}
            }
        )

        output = {
            "statusCode": 201,
            "body": json.dumps({"url": presigned_url, "jobId": job_id})
        }

        # Add entry to DB
        img_entry = ProcessedImage()
        img_entry.image_job_id = job_id
        img_entry.identify_celebrities = identify_celebrities
        img_entry.status = STATUS_CREATED
        img_entry.save()

        return output

    except (KeyError, TypeError, AttributeError):
        return {"statusCode": 400}

    except ClientError:
        return {"statusCode": 500}
