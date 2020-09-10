# -*- coding: utf-8 -*-
"""InternalRun - Internal-only run object.

Semi-stubbed run for internal process use.

"""
import wandb
from wandb import data_types

if wandb.PY3:  # type: ignore[has-type]
    from wandb.sdk import wandb_run
else:
    from wandb.sdk_py27 import wandb_run


class InternalRun(wandb_run.Run):
    def __init__(self, run_obj, settings):
        super(InternalRun, self).__init__(settings=settings)
        self._run_obj = run_obj

        # TODO: This undoes what's done in the constructor of wandb_run.Run.
        # We really want a common interface for wandb_run.Run and InternalRun.
        data_types._datatypes_set_callback(None)

    def _set_backend(self, backend):
        # This type of run object can't have a backend
        # or do any writes.
        pass
