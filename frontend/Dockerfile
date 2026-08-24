FROM node:24-alpine

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install

COPY . .
EXPOSE 5173

# 开发环境由 Vite 反向代理 /api 到 FastAPI 服务。
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
