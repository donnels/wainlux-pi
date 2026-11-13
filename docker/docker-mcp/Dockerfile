FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/mcp_server.py app/test_mcp_server.py ./
RUN python -m unittest -v test_mcp_server
ENV MCP_HOST=0.0.0.0 MCP_PORT=8082
EXPOSE 8082
CMD ["python", "mcp_server.py"]
