# The Postgres the database tests run: Cloud SQL's major version, plus the pgAudit extension that
# Cloud SQL ships and migration 0020 creates. procps supplies the `kill` binary the auth tests signal
# a backend with; the base image has only the shell builtin.
FROM postgres:16.14-bookworm@sha256:64154d0babcb1741988719e703419af0382b19953706149f9872fbd0f438efa8

RUN apt-get update \
  && apt-get install --yes --no-install-recommends postgresql-16-pgaudit=16.1-2.pgdg12+1 procps=2:4.0.2-3 \
  && rm -rf /var/lib/apt/lists/*
