FROM public.ecr.aws/lambda/python:3.12

COPY lambda/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt

ARG TAILSCALE_VERSION=1.78.1
RUN dnf install -y tar gzip && \
    curl -fsSL https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_amd64.tgz \
      | tar -xz -C /tmp && \
    mv /tmp/tailscale_${TAILSCALE_VERSION}_amd64/tailscale* /usr/local/bin/ && \
    rm -rf /tmp/tailscale_* && \
    dnf clean all

COPY curator/ ${LAMBDA_TASK_ROOT}/curator/
COPY news_curator.py ${LAMBDA_TASK_ROOT}/
COPY job_curator.py ${LAMBDA_TASK_ROOT}/
COPY feed_prompt.py ${LAMBDA_TASK_ROOT}/
COPY lambda/handler.py ${LAMBDA_TASK_ROOT}/
COPY lambda/job_handler.py ${LAMBDA_TASK_ROOT}/

# Default CMD = news handler so the existing scrape-news-curator Lambda keeps
# working unchanged. The scrape-job-curator function overrides this via
# `--image-config "Command=['job_handler.lambda_handler']"` at deploy time.
CMD ["handler.lambda_handler"]
