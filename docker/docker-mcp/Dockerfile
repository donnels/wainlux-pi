FROM --platform=linux/amd64 docker.io/library/python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY docker/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY docker/app/mcp_server.py ./
ENV MCP_HOST=0.0.0.0 MCP_PORT=8082
EXPOSE 8082
CMD ["python", "mcp_server.py"]
