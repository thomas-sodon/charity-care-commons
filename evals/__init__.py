"""Charity Care Commons evaluation harness (design section 12).

YAML suites (``evals/suites``), a runner that compiles policies through
``charitycare.compile`` and scores them against gold, per-field-type scorers
with bootstrap 95% CIs, the naive-agent baseline, CI regression gates, the
public eval report, inter-rater agreement and the Open FAP Schema
compatibility check. Run ``python -m evals --help``.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
