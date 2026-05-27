"""rescore-llm Lambda handler · placeholder during Dockerfile spike.

Real implementation lands in step 7 of the integration plan. This stub
exists so the Docker build's COPY layer has a target file.
"""

def lambda_handler(event, context):
    return {"status": "placeholder", "event": event}
