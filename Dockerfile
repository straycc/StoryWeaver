FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY database ./database
COPY backend ./backend
ENV PYTHONPATH=/app/backend/src
EXPOSE 8000

# 单实例 JobSupervisor 不允许以多个 Uvicorn worker 运行。
# Schema 脚本是幂等的，仅负责空库初始化，不承担结构迁移。
CMD ["sh", "-c", "python -c \"import os, psycopg; sql=open('database/schema.sql', encoding='utf-8').read(); url=os.environ['STORYWEAVER_DATABASE_URL'].replace('postgresql+psycopg://', 'postgresql://', 1); connection=psycopg.connect(url); connection.execute(sql); connection.commit(); connection.close()\" && python -m storyweaver.api.server"]
