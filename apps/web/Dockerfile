# Build from the repo root: docker build -f apps/web/Dockerfile .
# (the app imports the design system from ../../design, so the context must be the repo root)
FROM node:22-alpine AS build
WORKDIR /app
COPY design design
COPY apps/web/package.json apps/web/package-lock.json apps/web/
RUN cd apps/web && npm ci
COPY apps/web apps/web
# Same-origin by default: the browser talks to this container, nginx proxies to chat and gateway.
RUN cd apps/web && npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine
# The official image renders /etc/nginx/templates/*.template with the environment at start-up.
COPY apps/web/nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/apps/web/dist /usr/share/nginx/html
ENV HUDDLE_CHAT_UPSTREAM=chat:8001 HUDDLE_GATEWAY_UPSTREAM=gateway:8000
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/nginx-health || exit 1
