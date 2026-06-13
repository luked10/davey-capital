FROM python:3.12-slim

WORKDIR /app

COPY mcp_server/requirements.txt /app/mcp_server/requirements.txt
RUN pip install --no-cache-dir -r /app/mcp_server/requirements.txt

COPY . /app

ENV DAVEY_ROOT=/app
EXPOSE 8080

CMD ["python3", "mcp_server/server.py"]
