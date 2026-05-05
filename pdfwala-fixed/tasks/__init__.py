"""
tasks package — explicit submodule imports so that Celery's @task decorators
register on every worker import.

FIX: Previously this file was empty. Celery's `autodiscover_tasks(["tasks"])`
looks for `tasks.tasks` (which doesn't exist), so tasks were never registered
and the workers crashed with `ModuleNotFoundError: No module named 'tasks'`.
Importing the modules here guarantees registration whenever `tasks` is loaded.
"""

from . import pdf_tasks    # noqa: F401
from . import office_tasks  # noqa: F401
from . import image_tasks   # noqa: F401
