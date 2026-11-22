FROM python:3.12-slim
ENV TZ=Asia/Tokyo PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends baresip tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn[standard]>=0.34,<1" "httpx>=0.28,<1" "mcp>=1.10,<2" "jpholiday>=0.1,<2"
WORKDIR /app
COPY app/ /app/
VOLUME /data
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "45000"]
