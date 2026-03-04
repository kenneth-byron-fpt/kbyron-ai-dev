FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY webhook/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy integrations package
COPY integrations/ ./integrations/

# Copy agent system prompts
COPY webhook/agents/ ./agents/

# Copy webhook server
COPY webhook/main.py .

RUN mkdir -p logs

ENV AGENT_DIR=/app/agents

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
