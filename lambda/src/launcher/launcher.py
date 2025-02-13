from contextlib import closing
import json
import logging
import re

import boto3
import jmespath

from lambda_logs import JSONFormatter, custom_lambda_logs

logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.handlers[0].setFormatter(JSONFormatter())

EXTENDED_JOB_DATA_FILE_NAME = "_JOB_DATA_"


def read_s3_object(bucket: str, key: str, version: str) -> dict:
    s3 = boto3.client("s3")

    # raises "ClientError: An error occurred (NoSuchVersion)...The specified version does not exist." if file doesn't exist
    # raises "ClientError: An error occurred (InvalidArgument)...Invalid version id specified" if version doesn't exist
    response = s3.get_object(Bucket=bucket, Key=key, VersionId=version)

    with closing(response["Body"]) as fp:
        # this will raise JSONDecodeError for folder creation events (also empty
        # files & malformed JSON)
        ret = json.load(fp)
    return ret


JOB_FINDER = re.compile(r"\${!?job.(.+?)}")

def substitute_job_data(target: str, job_data: dict) -> str:
    try:
        # this is a hacky way to raise a KeyError when jmespath.search fails -------vvvvvvvvvvvvvv
        ret = JOB_FINDER.sub(lambda m: str(jmespath.search(m.group(1), job_data) or {}[m.group(0)]), target)
    except KeyError:
        raise RuntimeError(f"unrecognized job data field in '{target}'")
    return ret


def copy_job_data_to_repo(src_bucket: str, src_key: str, src_version: str, dst_bucket: str, dst_prefix: str) -> None:
    filename = src_key.rsplit("/", 1)[-1]
    dst_key = f"{dst_prefix}/{filename}"
    s3 = boto3.client("s3")
    s3.copy_object(CopySource={"Bucket": src_bucket, "Key": src_key, "VersionId": src_version},
                   Bucket=dst_bucket, Key=dst_key)


def write_extended_job_data_object(raw_job_data: dict, dst_bucket: str, dst_prefix: str) -> None:
    job_data = {
        "job": raw_job_data,
        "scatter": {},
        "parent": {},
    }
    dst_key = f"{dst_prefix}/{EXTENDED_JOB_DATA_FILE_NAME}"
    s3 = boto3.client("s3")
    s3.put_object(Bucket=dst_bucket, Key=dst_key,
                  Body=json.dumps(job_data).encode("utf-8"))


def write_execution_record(event: dict, dst_bucket: str, dst_prefix: str) -> None:
    dst_key = f"{dst_prefix}/execution_info/{event['logging']['sfn_execution_id']}"
    s3 = boto3.client("s3")
    s3.put_object(Bucket=dst_bucket, Key=dst_key,
                  Body=json.dumps(event, indent=4).encode("utf-8"))


def handle_s3_launch(event: dict) -> dict:
    src_bucket = event["input_obj"]["job_file"]["bucket"]
    src_key = event["input_obj"]["job_file"]["key"]
    src_version = event["input_obj"]["job_file"]["version"]

    id_prefix = event["logging"]["sfn_execution_id"].split("-", 1)[0]

    # if bucket versioning is suspended,version will be an empty string
    job_data = read_s3_object(src_bucket, src_key, src_version)

    repo = substitute_job_data(event["repo_template"], job_data)
    repo_bucket, repo_prefix = repo.split("/", 3)[2:]

    copy_job_data_to_repo(src_bucket, src_key, src_version, repo_bucket, repo_prefix)
    write_extended_job_data_object(job_data, repo_bucket, repo_prefix)
    write_execution_record(event, repo_bucket, repo_prefix)

    ret = {
        "id_prefix": id_prefix,
        "index": event["input_obj"]["index"],
        "job_file": {
            "bucket": src_bucket,
            "key": src_key,
            "version": src_version,
            "s3_request_id": event["input_obj"]["job_file"]["s3_request_id"],
        },
        "prev_outputs": {},
        "repo": repo,
    }

    return ret


def lambda_handler(event: dict, context: object) -> dict:
    # event = {
    #   repo_template: ...
    #   input_obj: {}
    #   logging: {
    #     branch: ...
    #     job_file_bucket: ...
    #     job_file_key: ...
    #     job_file_version: ...
    #     job_files3_request_id: ...
    #     sfn_execution_id: ...
    #     step_name: ...
    #     workflow_name: ...
    #   }
    # }
    with custom_lambda_logs(**event["logging"]):
        logger.info(f"event: {str(event)}")

        if "AWS_STEP_FUNCTIONS_STARTED_BY_EXECUTION_ID" in event["input_obj"]:
            # this is a subpipe execution...nothing to do but pass along the input object
            logger.info("subpipe launch detected")
            ret = event["input_obj"]

        else:
            logger.info(f"s3 launch detected")
            ret = handle_s3_launch(event)

        logger.info(f"return: {str(ret)}")
        return ret
