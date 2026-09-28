FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY skills ./skills
ENV PYTHONPATH=/app/backend/src
EXPOSE 8000

# 单实例 JobSupervisor 不允许以多个 Uvicorn worker 运行。
CMD ["python", "-m", "storyweaver.api.server"]
