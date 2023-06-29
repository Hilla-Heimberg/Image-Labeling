import boto3
from botocore.exceptions import ClientError
from models.processed_image import ProcessedImage, STATUS_PROCESSING, \
    STATUS_FAILURE, STATUS_SUCCESS
import os
from PIL import Image, ImageDraw, ImageFont
from typing import Dict, Any

PROCESSED_IMAGES_BUCKET_NAME = os.environ["PROCESSED_IMAGES_BUCKET_NAME"]

__ARIAL_FONT_FILE__ = os.path.join("fonts", "arial.ttf")
MIN_CONFIDENCE_LEVEL = 90.0
FONT_SIZE = 15

s3_client = boto3.client("s3")


def handler(event: Dict[str, Any], _: Any):
    try:
        # Extract the bucket, key and job ID from the uploaded image meta data
        bucket, key, job_id = get_meta_data(event)

        should_identify_celebrities = \
            ProcessedImage.get(job_id).identify_celebrities

        # Update status in the DB
        ProcessedImage(
            image_job_id = job_id,
            status = STATUS_PROCESSING
        ).save()

        image = {"S3Object": {"Bucket": bucket, "Name": key}}
        boxes_to_draw = {}

        if should_identify_celebrities == "true":
            boxes_to_draw = find_celebrities_in_image(image)

        else:
            boxes_to_draw = find_objects_in_image(image)

        if not boxes_to_draw:
            ProcessedImage(
                image_job_id=job_id,
                status=STATUS_FAILURE
            ).save()
            return

        # Mark the items found on the image
        s3_client.download_file(bucket, key, '/tmp/image.jpg')
        draw_boxes(boxes_to_draw)

        # Upload the marked image to S3
        s3_client.upload_file("/tmp/marked_image.jpg", PROCESSED_IMAGES_BUCKET_NAME, key)

        # Store the processed image in the Processed Images Bucket
        # and update the S3 path in the images DynamoDB table.
        ProcessedImage(
            image_job_id = job_id,
            status = STATUS_SUCCESS,
            processed_image_bucket = PROCESSED_IMAGES_BUCKET_NAME,
            processed_image_key = key
        ).save()
        return

    except (KeyError, TypeError, AttributeError, OSError):
        ProcessedImage(
            image_job_id=job_id,
            status=STATUS_FAILURE
        ).save()

    except ClientError:
        ProcessedImage(
            image_job_id=job_id,
            status=STATUS_FAILURE
        ).save()


def get_meta_data(event):
    bucket = event["Records"][0]["s3"]["bucket"]["name"]
    key = event["Records"][0]["s3"]["object"]["key"]
    metadata = s3_client.head_object(Bucket=bucket, Key=key)['Metadata']
    job_id = metadata["job_id"]

    return bucket, key, job_id


def find_objects_in_image(image):
    # Make a request to AWS Rekognition using the detect_labels function
    # of the boto3 rekognition client
    rekognition = boto3.client("rekognition")
    rekognition_response = rekognition.detect_labels(
        Image = image,
        MinConfidence=MIN_CONFIDENCE_LEVEL
    )
    labels = rekognition_response["Labels"]

    # Filter the list:
    # 1. Have non-empty instances
    # 2. Filter labels with the same BoundingBox dimensions
    objects_to_draw = {}

    for label in labels:
        name = label["Name"]
        num_parents = len(label["Parents"])

        for instance in label["Instances"]:
            box = instance["BoundingBox"]
            tuple_box = (box["Width"], box["Height"], box["Left"], box["Top"])

            if tuple_box not in objects_to_draw \
                    or num_parents > objects_to_draw[tuple_box]["num_parents"]:
                objects_to_draw[tuple_box] = {"name": name,
                                              "num_parents": num_parents}

    return objects_to_draw


def find_celebrities_in_image(image):
    rekognition = boto3.client("rekognition")
    rekognition_response = rekognition.recognize_celebrities(
        Image=image
    )
    faces = rekognition_response["CelebrityFaces"]

    faces_to_draw = {}
    for face in faces:
        bounding_box = face["Face"]["BoundingBox"]
        tuple_box = (
            bounding_box["Width"],
            bounding_box["Height"],
            bounding_box["Left"],
            bounding_box["Top"]
        )
        faces_to_draw[tuple_box] = {"name": face["Name"]}

    return faces_to_draw

def draw_boxes(boxes_to_draw):

    with Image.open('/tmp/image.jpg') as image:
        # Normalize the bounding box coordinates
        width, height = image.size
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype(__ARIAL_FONT_FILE__, FONT_SIZE)

        for tuple_box, box_details in boxes_to_draw.items():
            top_left = \
                (float(width * tuple_box[2]), float(height * tuple_box[3]))
            bottom_right = (
                float(width * (tuple_box[2] + tuple_box[0])),
                float(height * (tuple_box[3] + tuple_box[1]))
            )

            draw.rectangle((top_left, bottom_right), outline='gold', width=4)
            draw.text(top_left, box_details["name"], fill='white', font=font,
                      stroke_fill='black', stroke_width=3)

        image.save("/tmp/marked_image.jpg")