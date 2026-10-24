"""Seed stage (design section 8, S1): CMS facility universe, Schedule H Part V, URL seeds.

* :mod:`charitycare.seed.cms` - CMS Hospital General Information (live PDC API, cache, demo CSV)
* :mod:`charitycare.seed.schedule_h` - Part V parser with pinned xpaths and narrative URL recovery
* :mod:`charitycare.seed.irs_batch` - streaming IRS index CSV and ZIP batch readers
* :mod:`charitycare.seed.universe` - CMS ∪ Schedule H ∪ external reconciliation with stable ids
* :mod:`charitycare.seed.pipeline` - persistence to the registry and the coverage report

Run ``python -m charitycare.seed --help``; :data:`CLI_COMMAND` is the ``ccc seed`` entry for
the integrator to add to ``charitycare.cli.COMMANDS``.
"""

from charitycare.seed.cli import CLI_COMMAND

__all__ = ["CLI_COMMAND"]
