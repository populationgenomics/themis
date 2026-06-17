# web

The IAP-fronted web surface, served on Cloud Run as `themis-web`. Today a static
placeholder page that exercises the build → Artifact Registry → Cloud Run →
load balancer → IAP path; replaced by the Next.js frontend later.

CI builds this directory into the `themis/web` image (tagged with the commit
SHA) and `infra`'s Pulumi program points the Cloud Run service at it.
