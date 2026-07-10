FROM node:22-alpine AS build
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@11.7.0 --activate
COPY apps/add_frontend/package.json apps/add_frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/add_frontend ./
RUN pnpm run build

FROM nginx:1.27-alpine
COPY apps/add_frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1/health/live >/dev/null || exit 1
