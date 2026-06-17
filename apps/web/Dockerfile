# Placeholder web surface: nginx serving one static page, listening on Cloud
# Run's $PORT. The stock nginx entrypoint renders templates under
# /etc/nginx/templates with envsubst before starting. Replaced by the Next.js
# build later — the image name (themis/web) and Cloud Run contract stay.
FROM nginx:1.27-alpine

ENV PORT=8080

COPY default.conf.template /etc/nginx/templates/default.conf.template
COPY index.html /usr/share/nginx/html/index.html

EXPOSE 8080
