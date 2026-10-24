"""Public read API, ``/v1/screen``, review console and navigator page (FastAPI).

``create_app(registry)`` builds the ASGI app; ``python -m charitycare.api serve``
runs it with uvicorn. See :mod:`charitycare.api.app` for the endpoints.
"""

from charitycare.api.app import create_app
from charitycare.api.settings import ApiSettings

__all__ = ["ApiSettings", "create_app"]
