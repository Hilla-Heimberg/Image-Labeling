import os
from pynamodb.attributes import UnicodeAttribute
from pynamodb.models import Model

STATUS_CREATED = "created"
STATUS_PROCESSING = "processing"
STATUS_FAILURE = "failure"
STATUS_SUCCESS = "success"

class ProcessedImage(Model):
    """
    A representation of an image and its processing status.
    When the processing is completed, the ProcessedImage model should contain the S3 bucket and key
    for the processed image.
    """
    class Meta:
        region = os.environ["AWS_DEFAULT_REGION"]
        table_name = "huji-lightricks-exercise-6-images"
    image_job_id = UnicodeAttribute(hash_key=True)
    identify_celebrities = UnicodeAttribute(null=True)
    status = UnicodeAttribute()
    processed_image_bucket = UnicodeAttribute(null=True)
    processed_image_key = UnicodeAttribute(null=True)
