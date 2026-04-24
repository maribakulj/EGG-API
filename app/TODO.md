# Deferred TODOs (explicitly out of current scope)

Items historically listed here as "deferred" have since landed:
OpenSearch adapter, admin UI, persistent API key store, Redis rate
limiting and cache, cursor pagination, OAI-PMH provider + importers,
IIIF manifest redirect. Keep this file as the ground-truth list of
what is *still* deliberately deferred.

- TODO: Add Solr adapter. (Elasticsearch + OpenSearch cover the
  common GLAM cluster topologies; Solr support would require a
  separate query DSL translator.)
- TODO: First-class IIIF proxy. The current ``/v1/manifest/{id}``
  endpoint is a stable 302 redirect to the upstream manifest URL
  held in ``links.iiif_manifest``. A full proxy that rewrites image
  service URLs, caches manifests and signs access tokens is a
  larger piece of work.
