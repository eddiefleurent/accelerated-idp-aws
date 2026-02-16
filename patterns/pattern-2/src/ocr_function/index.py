# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
OCR function that processes PDFs and extracts text using AWS Textract.
Uses the idp_common.ocr package for OCR functionality.
"""

import json
import logging
import os
import time

from aws_xray_sdk.core import patch_all, xray_recorder
from idp_common import get_config, ocr
from idp_common.docs_service import create_document_service
from idp_common.models import Document, Status
from idp_common.utils import calculate_lambda_metering, merge_metering_data

patch_all()

# Configuration will be loaded in handler function

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
logging.getLogger("idp_common.bedrock.client").setLevel(
    os.environ.get("BEDROCK_LOG_LEVEL", "INFO")
)

# Initialize settings
region = os.environ["AWS_REGION"]
METRIC_NAMESPACE = os.environ.get("METRIC_NAMESPACE")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", 20))


@xray_recorder.capture("ocr_function")
def handler(event, context):
    """
    Lambda handler for OCR processing.
    """
    start_time = time.time()  # Capture start time for Lambda metering
    logger.info(f"Event: {json.dumps(event)}")

    # Get document from event - handle both compressed and uncompressed
    working_bucket = os.environ.get("WORKING_BUCKET")
    document = Document.load_document(event["document"], working_bucket, logger)

    # Log loaded document for troubleshooting
    logger.info(f"Loaded document - ID: {document.id}, input_key: {document.input_key}")
    logger.info(
        f"Document buckets - input_bucket: {document.input_bucket}, output_bucket: {document.output_bucket}"
    )
    logger.info(f"Document status: {document.status}, num_pages: {document.num_pages}")
    logger.info(
        f"Document pages count: {len(document.pages)}, sections count: {len(document.sections)}"
    )
    logger.info(f"Full document content: {json.dumps(document.to_dict(), default=str)}")

    # Compute effective IDs for annotations (matching config resolution)
    # Guard against use_case_context being null/None in the event
    use_case_context = event.get("use_case_context") or {}
    if not isinstance(use_case_context, dict):
        if use_case_context is not None:
            logger.warning(
                "use_case_context is not a dict (got %s), falling back to empty dict",
                type(use_case_context).__name__,
            )
        use_case_context = {}

    # Only honor use_case_context if it contains both required keys;
    # a partial context would cause mixed lookups in get_config.
    if use_case_context and not (
        use_case_context.get("business_unit_id") and use_case_context.get("use_case_id")
    ):
        logger.warning(
            "use_case_context is partial (got %s), ignoring and falling back to document fields",
            use_case_context,
        )
        use_case_context = {}

    # Normalize to str or None so get_config and X-Ray annotations get consistent types.
    raw_bu = use_case_context.get("business_unit_id") or document.business_unit_id
    raw_uc = use_case_context.get("use_case_id") or document.use_case_id
    effective_business_unit_id = str(raw_bu) if raw_bu is not None else None
    effective_use_case_id = str(raw_uc) if raw_uc is not None else None

    # X-Ray annotations
    xray_recorder.put_annotation("document_id", document.id)
    xray_recorder.put_annotation("processing_stage", "ocr")
    if effective_business_unit_id:
        xray_recorder.put_annotation("business_unit_id", effective_business_unit_id)
    if effective_use_case_id:
        xray_recorder.put_annotation("use_case_id", effective_use_case_id)

    # Intelligent OCR detection: Skip if pages already have OCR data
    pages_with_ocr = 0
    for page in document.pages.values():
        if page.image_uri and page.raw_text_uri:
            pages_with_ocr += 1

    if pages_with_ocr == len(document.pages) and len(document.pages) > 0:
        logger.info(
            f"Skipping OCR processing for document {document.id} - all {len(document.pages)} pages already have OCR data"
        )

        # Ensure document has the expected execution ARN
        document.workflow_execution_arn = event.get("execution_arn")

        # Add Lambda metering for OCR skip execution
        try:
            lambda_metering = calculate_lambda_metering("OCR", context, start_time)
            document.metering = merge_metering_data(document.metering, lambda_metering)
        except Exception as e:
            logger.warning(f"Failed to add Lambda metering for OCR skip: {str(e)}")

        # Update document execution ARN and merged metering for tracking
        if document.status == Status.QUEUED:
            document_service = create_document_service()
            logger.info("Updating document execution ARN and metering for OCR skip")
            document_service.update_document(document)

        # Prepare output with existing document data
        working_bucket = os.environ.get("WORKING_BUCKET")
        response = {
            "document": document.serialize_document(working_bucket, "ocr_skip", logger)
        }

        logger.info(f"OCR skipped - Response: {json.dumps(response, default=str)}")
        return response

    # Normal OCR processing
    # Update document status to OCR and update in AppSync
    document.status = Status.OCR
    document.workflow_execution_arn = event.get("execution_arn")
    document_service = create_document_service()
    logger.info(f"Updating document status to {document.status}")
    document_service.update_document(document)

    t0 = time.time()

    # Load configuration and initialize the OCR service using new simplified pattern
    # Reuse the already-normalized effective IDs for consistent config resolution.
    config = get_config(
        as_model=True,
        business_unit_id=effective_business_unit_id,
        use_case_id=effective_use_case_id,
    )
    backend = config.ocr.backend

    logger.info(f"Initializing OCR with backend: {backend}")
    service = ocr.OcrService(region=region, config=config, backend=backend)

    # Process the document - the service will read the PDF content directly
    document = service.process_document(document)

    # Check if document processing failed
    if document.status == Status.FAILED:
        error_message = f"OCR processing failed for document {document.id}"
        logger.error(error_message)
        # Update status in AppSync before raising exception
        document_service.update_document(document)
        raise Exception(error_message)

    t1 = time.time()
    logger.info(f"Total OCR processing time: {t1 - t0:.2f} seconds")

    # Add Lambda metering for successful OCR execution
    try:
        lambda_metering = calculate_lambda_metering("OCR", context, start_time)
        document.metering = merge_metering_data(document.metering, lambda_metering)
    except Exception as e:
        logger.warning(f"Failed to add Lambda metering for OCR: {str(e)}")

    # Prepare output with automatic compression if needed
    working_bucket = os.environ.get("WORKING_BUCKET")
    response = {"document": document.serialize_document(working_bucket, "ocr", logger)}

    logger.info(f"Response: {json.dumps(response, default=str)}")
    return response
