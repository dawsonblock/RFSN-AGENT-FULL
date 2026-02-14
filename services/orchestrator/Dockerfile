FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
# copy only needed runtime paths from repo root
COPY services/orchestrator /app/services/orchestrator
COPY services/hardening_guard /app/services/hardening_guard
COPY services/replay_verifier /app/services/replay_verifier
COPY policies /app/policies
COPY rfsn_kernel /app/rfsn_kernel
EXPOSE 8000
CMD ["uvicorn","services.orchestrator.app:app","--host","0.0.0.0","--port","8000"]
