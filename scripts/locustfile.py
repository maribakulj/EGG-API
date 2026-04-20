"""Locust profile for EGG-API load testing.

Usage (requires ``pip install locust``; not pinned in pyproject because it
is an ops tool, not a runtime dep):

    locust -f scripts/locustfile.py --host https://egg.example.org \\
           -u 50 -r 5 --run-time 2m --headless

Envs:
    EGG_API_KEY   Optional X-API-Key header appended to every request.
    EGG_Q_POOL    Optional comma-separated list of search terms; defaults
                  to a small GLAM-flavored sample.

The profile hits the public read path (livez/search/records) with weights
tuned to approximate a typical collections-catalog workload. Admin endpoints
are deliberately excluded — load-testing against the admin UI is noise.
"""

from __future__ import annotations

import os
import random

from locust import HttpUser, between, task

_SEARCH_TERMS = (os.getenv("EGG_Q_POOL") or "ceramic, manuscript, portrait, viking, baroque").split(
    ","
)
_SEARCH_TERMS = [term.strip() for term in _SEARCH_TERMS if term.strip()]


class PublicReader(HttpUser):
    """Simulates a consumer app hitting the read-only public endpoints."""

    wait_time = between(0.5, 2.0)

    @property
    def auth_headers(self) -> dict[str, str]:
        api_key = os.getenv("EGG_API_KEY")
        return {"x-api-key": api_key} if api_key else {}

    @task(1)
    def livez(self) -> None:
        # Dirt-cheap request; catches loop starvation and ingress latency.
        self.client.get("/v1/livez", name="/v1/livez")

    @task(10)
    def search(self) -> None:
        term = random.choice(_SEARCH_TERMS)  # noqa: S311
        self.client.get(
            f"/v1/search?q={term}&page_size=20",
            name="/v1/search",
            headers=self.auth_headers,
        )

    @task(4)
    def search_with_facets(self) -> None:
        term = random.choice(_SEARCH_TERMS)  # noqa: S311
        self.client.get(
            f"/v1/search?q={term}&facet=type&facet=language",
            name="/v1/search+facets",
            headers=self.auth_headers,
        )

    @task(2)
    def get_record(self) -> None:
        # Fake record id; the FakeAdapter-free deployment will likely 404,
        # but the latency path matters regardless.
        self.client.get(
            f"/v1/records/bench-{random.randint(1, 1000)}",  # noqa: S311
            name="/v1/records/{id}",
            headers=self.auth_headers,
        )

    @task(1)
    def facets_only(self) -> None:
        term = random.choice(_SEARCH_TERMS)  # noqa: S311
        self.client.get(
            f"/v1/facets?q={term}&facet=type",
            name="/v1/facets",
            headers=self.auth_headers,
        )
