FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    -i ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} \
    -r requirements.txt

COPY . .
RUN pip install --no-cache-dir --timeout 120 --retries 5 \
    -i ${PIP_INDEX_URL} --trusted-host ${PIP_TRUSTED_HOST} \
    -e .

EXPOSE 8000

CMD ["python", "-m", "agent_sentinel.main"]
