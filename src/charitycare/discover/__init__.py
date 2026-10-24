"""Discovery stage (design section 8, S2-S3): find, fetch, hash, classify and map policy documents.

* :mod:`~charitycare.discover.fetcher` - polite fetcher (robots.txt, 1 req / 5 s per host, allowlist, block detection)
* :mod:`~charitycare.discover.agent` - discovery agent (LLM tools, budget <= 25) with a heuristic fallback
* :mod:`~charitycare.discover.sitemap`, :mod:`~charitycare.discover.wayback` - sitemap and Wayback CDX lookups
* :mod:`~charitycare.discover.classify` - Haiku-class document classifier with a keyword fallback
* :mod:`~charitycare.discover.docstore` - sha256-addressed versioned document store with text layers
* :mod:`~charitycare.discover.policy` - PolicyVersion and facility -> policy mapping (section 5.4)
* :mod:`~charitycare.discover.records` - records-request queue; :mod:`~charitycare.discover.linkhealth` - link health
* :mod:`~charitycare.discover.pipeline` - orchestration; :data:`CLI_COMMAND` is the ``ccc discover`` entry
"""

from charitycare.discover.cli import CLI_COMMAND

__all__ = ["CLI_COMMAND"]
