-- 0020_pgaudit.sql -- creates the pgAudit extension, through which the instance logs every statement
-- (infrastructure.md, Audit logging). Cloud SQL allows it only on an instance running with
-- `cloudsql.enable_pgaudit`, which the deploy's `pulumi up` sets before migrations run, and only for a
-- member of cloudsqlsuperuser, which the migrator is.
CREATE EXTENSION IF NOT EXISTS pgaudit;
