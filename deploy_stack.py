import contextlib
import os
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from os.path import abspath, join, relpath
from pathlib import Path
from typing import Dict, List, Any, Union, Optional, Tuple, Literal
import argparse
import sys
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

from botocore.exceptions import WaiterError, ClientError

AWS_REGION = "us-west-2"
BASE_DIRECTORY = abspath(join(abspath(__file__), ".."))
DEPLOY_PACKAGE_DIR = join(BASE_DIRECTORY, "deploy_package")
BYTES_IN_MB = 1024**2

USED_AWS_SERVICES = sorted(
    [
        "dynamodb",
        "lambda",
        "rekognition",
        "s3",
    ]
)


def deploy_cloudformation_template(
    stack_name: str,
    template_file_path: str,
    pair_name: str,
    now_ms: int,
) -> None:
    cloudformation_client = boto3.client("cloudformation", region_name=AWS_REGION)
    print(f"Deploying template [{stack_name}]")

    should_create = False
    try:
        cloudformation_client.describe_stacks(StackName=stack_name)
    except ClientError:
        should_create = True

    stack_parameters: Dict[str, Any] = {"PairName": pair_name, "CodePackageDate":str(now_ms)}
    formatted_parameters: List[Dict[str, Union[str, bool]]] = [
        {"ParameterKey": key, "ParameterValue": value} for key, value in stack_parameters.items()
    ]

    if should_create:
        _create_stack(cloudformation_client, stack_name, template_file_path, formatted_parameters)
    else:
        _update_stack(cloudformation_client, stack_name, template_file_path, formatted_parameters)


def _create_stack(
    cloudformation_client: boto3.client,
    stack_name: str,
    template_file_path: str,
    formatted_parameters: List[Dict[str, Union[str, bool]]],
) -> None:
    template = _fetch_template(template_file_path)

    cloudformation_client.create_stack(
        StackName=stack_name,
        TemplateBody=template,
        Parameters=formatted_parameters,
        Capabilities=["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
    )

    status = _wait_on_stack_update(cloudformation_client, stack_name, "stack_create_complete")

    if not status or status not in ["CREATE_COMPLETE", "UPDATE_COMPLETE"]:
        raise ValueError(f"Failed to create stack {stack_name} {status}")

    print("Stack was created")


def _update_stack(
    cloudformation_client: boto3.client,
    stack_name: str,
    template_file_path: str,
    formatted_parameters: List[Dict[str, Union[str, bool]]],
) -> None:
    print(f"Creating Change Sets [{stack_name}]")

    _delete_change_sets(cloudformation_client, stack_name)

    template = _fetch_template(template_file_path)

    if not (
        change_set_name := _create_change_set(
            cloudformation_client=cloudformation_client,
            stack_name=stack_name,
            template=template,
            formatted_parameters=formatted_parameters,
        )
    ):
        return

    print(f"Executing Change Set {change_set_name}")
    _exec_change_set(cloudformation_client, change_set_name, stack_name)


def _delete_change_sets(cloudformation_client: boto3.client, stack_name: str) -> None:
    kwargs = {"StackName": stack_name}
    response = cloudformation_client.list_change_sets(**kwargs)

    change_sets = response["Summaries"]
    for change_set in change_sets:
        change_set_name = change_set["ChangeSetName"]
        cloudformation_client.delete_change_set(StackName=stack_name, ChangeSetName=change_set_name)
        print(f"Deleted change set {change_set_name}")


def _fetch_template(template_file_path: str) -> str:
    with open(template_file_path, "r") as template_file:
        return template_file.read()


def _create_change_set(
    cloudformation_client: boto3.client,
    stack_name: str,
    template: str,
    formatted_parameters: List[Dict[str, Union[str, bool]]],
) -> Optional[str]:
    current_time_ms = int(time.time() * 1000)
    change_set_name = f"{stack_name}-{current_time_ms}"

    cloudformation_client.create_change_set(
        StackName=stack_name,
        TemplateBody=template,
        Parameters=formatted_parameters,
        Capabilities=["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
        ChangeSetName=change_set_name,
    )

    status, status_reason = _wait_for_change_set(cloudformation_client, stack_name, change_set_name)
    if status == "FAILED":
        print("No change detected")
        return None

    return change_set_name


def _wait_for_change_set(
    cloudformation_client: boto3.client, stack_name: str, change_set_name: str
) -> Tuple[str, Optional[str]]:
    waiter = cloudformation_client.get_waiter("change_set_create_complete")
    try:
        waiter.wait(
            ChangeSetName=change_set_name,
            StackName=stack_name,
            WaiterConfig={"Delay": 10, "MaxAttempts": 50},
        )
    except WaiterError as error:
        failed_status = error.last_response["Status"]
        reason = error.last_response["StatusReason"]
        return failed_status, reason

    return _get_change_set_status(cloudformation_client, stack_name, change_set_name)


def _get_change_set_status(
    cloudformation_client: boto3.client, stack_name: str, change_set_name: str
) -> Tuple[str, Optional[str]]:
    result = cloudformation_client.describe_change_set(
        StackName=stack_name, ChangeSetName=change_set_name
    )
    stack_status = result["Status"]
    reason = result.get("StatusReason")

    return stack_status, reason


def _exec_change_set(
    cloudformation_client: boto3.client, change_set_name: str, stack_name: str
) -> None:
    cloudformation_client.execute_change_set(
        StackName=stack_name,
        ChangeSetName=change_set_name,
    )

    status = _wait_on_stack_update(cloudformation_client, stack_name, "stack_update_complete")

    if not status or status not in ["CREATE_COMPLETE", "UPDATE_COMPLETE"]:
        raise ValueError(f"Failed to update stack {stack_name} {status}")

    print("Stack was updated")


def _wait_on_stack_update(
    cloudformation_client: boto3.client,
    stack_name: str,
    waiter_name: Literal["stack_create_complete", "stack_update_complete"],
) -> Optional[str]:
    waiter = cloudformation_client.get_waiter(waiter_name)
    try:
        waiter.wait(
            StackName=stack_name,
            WaiterConfig={"Delay": 10, "MaxAttempts": 50},
        )
    except WaiterError as error:
        raise ValueError(f"Failed to update stack {stack_name}") from error

    return _get_stack_status(stack_name)


def _get_stack_status(stack_name_to_check: str) -> Optional[str]:
    cloudformation = boto3.resource("cloudformation", region_name=AWS_REGION)
    stack = cloudformation.Stack(stack_name_to_check)
    stack_status = stack.stack_status
    return stack_status

def deploy_code_package(pair_name: str, now_ms: int) -> None:
    bucket_name = f"huji-lightricks-exercise-6-code-{pair_name}-bucket"
    key = f"{pair_name}-{now_ms}-code-package.zip"
    config = Config(connect_timeout=10, read_timeout=10, retries={"total_max_attempts": 20})
    s3_client = boto3.client("s3", region_name=AWS_REGION, config=config)

    print("Creating code package")
    _create_code_package()

    print("Removing unused Botocore service packages")
    _remove_unused_botocore_services()

    print("Zipping and uploading code package")
    _zip_and_upload(s3_client, bucket_name, key, now_ms)

    print("Cleanup")
    _cleanup()


def _create_code_package() -> None:
    try:
        shutil.rmtree(DEPLOY_PACKAGE_DIR)
    except FileNotFoundError:
        pass

    print("Copying code into deploy package")
    Path(join(DEPLOY_PACKAGE_DIR, "lambdas")).mkdir(parents=True, exist_ok=True)
    _copy_directory(join(BASE_DIRECTORY, "lambdas"), join(DEPLOY_PACKAGE_DIR, "lambdas"))

    Path(join(DEPLOY_PACKAGE_DIR, "models")).mkdir(parents=True, exist_ok=True)
    _copy_directory(join(BASE_DIRECTORY, "models"), join(DEPLOY_PACKAGE_DIR, "models"))

    Path(join(DEPLOY_PACKAGE_DIR, "fonts")).mkdir(parents=True, exist_ok=True)
    _copy_directory(join(BASE_DIRECTORY, "fonts"), join(DEPLOY_PACKAGE_DIR, "fonts"))

    print("Installing Dependencies")

    subprocess.check_call(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--requirement",
            join(BASE_DIRECTORY, "requirements.txt"),
            "--target",
            DEPLOY_PACKAGE_DIR,
        ]
    )


def _remove_unused_botocore_services() -> None:
    botocore_services_directory = join(DEPLOY_PACKAGE_DIR, "botocore", "data")
    dirs_to_remove = [
        directory
        for directory in os.listdir(botocore_services_directory)
        if (
            os.path.isdir(join(botocore_services_directory, directory))
            and directory not in USED_AWS_SERVICES
        )
    ]

    print(f"Removing unused services from botocore {dirs_to_remove}")

    for directory in dirs_to_remove:
        shutil.rmtree(join(botocore_services_directory, directory))


def _copy_directory(source_directory_path: str, target_directory_path: str) -> None:
    for item in os.listdir(source_directory_path):
        source_sub = os.path.join(source_directory_path, item)
        dest_sub = os.path.join(target_directory_path, item)
        if os.path.isdir(source_sub):
            shutil.copytree(src=source_sub, dst=dest_sub, symlinks=False, ignore=None)
        else:
            shutil.copy2(source_sub, dest_sub)


def _zip_and_upload(
    boto_s3_client: boto3.client, s3_bucket_name: str, key: str, now_ms: int
) -> None:
    zip_file_path = join(BASE_DIRECTORY, "deploy_package.zip")
    _zip_directory(zip_file_path, DEPLOY_PACKAGE_DIR)
    print(f"ZIP file size: {int(os.path.getsize(zip_file_path) / BYTES_IN_MB)} MB")

    config = TransferConfig(
        multipart_threshold=BYTES_IN_MB,
        max_concurrency=20,
        multipart_chunksize=int(BYTES_IN_MB / 3),
    )
    boto_s3_client.upload_file(
        Filename=zip_file_path,
        Bucket=s3_bucket_name,
        Key=key,
        Config=config,
    )


def _zip_directory(output_file_path: str, base_directory_path: str) -> None:
    with open(output_file_path, "wb") as raw_output_file:
        with zipfile.ZipFile(
            file=raw_output_file, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as zip_file:
            with contextlib.closing(zip_file) as raw_zip_file:
                for directory_path, _, filenames in os.walk(base_directory_path, followlinks=True):
                    for filename in filenames:
                        full_path = join(directory_path, filename)
                        relative_path = relpath(full_path, base_directory_path)
                        raw_zip_file.write(full_path, relative_path)


def _cleanup() -> None:
    os.remove(join(BASE_DIRECTORY, "deploy_package.zip"))
    shutil.rmtree(DEPLOY_PACKAGE_DIR)


def get_lambdas_urls(stack_name_to_check: str) -> Tuple[Optional[str], Optional[str]]:
    cloudformation = boto3.resource("cloudformation", region_name=AWS_REGION)
    stack = cloudformation.Stack(stack_name_to_check)
    image_uploading_url = None
    image_getter_url = None
    for output in stack.outputs:
        if output["OutputKey"] == "ImageGetterLambdaURL":
            image_getter_url = output["OutputValue"]
        if output["OutputKey"] == "ImageUploadingLambdaURL":
            image_uploading_url = output["OutputValue"]

    return image_uploading_url, image_getter_url

def main() -> None:
    description = "Script for deploying exercise 6 resources"
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--pair-name",
        required=True,
        help="The name of the pair submitting the exercise",
        type=str,
        metavar="PAIR",
    )

    args = parser.parse_args()

    now_ms = int(datetime.now().timestamp() * 1000)
    try:
        deploy_cloudformation_template(
            pair_name=args.pair_name,
            now_ms=now_ms,
            stack_name="huji-lightricks-exercise-6-buckets",
            template_file_path="./cloudformation/code_bucket.yaml",
        )

        deploy_code_package(pair_name=args.pair_name, now_ms=now_ms)

        deploy_cloudformation_template(
            pair_name=args.pair_name,
            now_ms=now_ms,
            stack_name="huji-lightricks-exercise-6-resources",
            template_file_path="./cloudformation/exercise6.yaml",
        )

        image_uploading_url, image_getter_url = get_lambdas_urls("huji-lightricks-exercise-6-resources")
        print(f"Image uploading lambda URL: {image_uploading_url}")
        print(f"Image getter lambda URL: {image_getter_url}")

    except ValueError as error:
        print("FATAL ERROR")
        print(str(error))
        sys.exit(1)


if __name__ == "__main__":
    main()
