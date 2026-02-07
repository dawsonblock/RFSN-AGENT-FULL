FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git bash curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY rfsn_swebench/ /app/rfsn_swebench/
COPY requirements-bench.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

ENTRYPOINT ["python", "-m", "rfsn_swebench.cli"]
