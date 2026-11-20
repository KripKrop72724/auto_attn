FROM node:22-alpine AS build
WORKDIR /app
RUN corepack enable && corepack prepare pnpm@11.7.0 --activate
COPY apps/add_frontend/package.json apps/add_frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/add_frontend ./
RUN pnpm run build

# The old screens, served under /classic/ for one week after the revamp release.
# Remove this stage, its COPY below and the /classic/ blocks in nginx.conf
# together with apps/add_frontend_classic (docs/add-classic-screens.md).
FROM node:22-alpine AS build-classic
WORKDIR /classic
RUN corepack enable && corepack prepare pnpm@11.7.0 --activate
COPY apps/add_frontend_classic/package.json apps/add_frontend_classic/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY apps/add_frontend_classic ./
RUN pnpm run build

FROM nginx:1.27-alpine
COPY apps/add_frontend/security-headers.conf /etc/nginx/security-headers.conf
COPY apps/add_frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
COPY --from=build-classic /classic/dist /usr/share/nginx/html/classic
EXPOSE 80
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD wget -qO- http://127.0.0.1/health/ui >/dev/null || exit 1
