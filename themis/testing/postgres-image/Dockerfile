# The Postgres the database tests run: Cloud SQL's major version, plus the pgAudit extension that
# Cloud SQL ships and migration 0020 creates. procps supplies the `kill` binary the auth tests signal
# a backend with.
FROM postgres:16.14-trixie@sha256:95206741a5b214807675e14165369d05b93a9cf692223b616d07cca227e74b0b

RUN apt-get update \
  && apt-get install --yes --no-install-recommends postgresql-16-pgaudit=16.1-2.pgdg13+1 procps=2:4.0.4-9 \
  && rm -rf /var/lib/apt/lists/*
