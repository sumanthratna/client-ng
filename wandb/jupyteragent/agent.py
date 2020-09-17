# -*- coding: utf-8 -*-
"""Agent - Agent object.

Manage wandb jupyter agent.

"""

from __future__ import print_function

import os
import socket
import time

import wandb
from wandb import util
from wandb.apis import InternalApi
from wandb.lib import config_util


class Job(object):
    def __init__(self, command):
        job_type = command.get("type")
        self.type = job_type
        if job_type == "run":
            self.run_id = command.get("run_id")
            self.config = command.get("args")

    def __repr__(self):
        return "Job({},{})".format(self.run_id, self.config)

    def done(self):
        return self.type == "exit"


class Agent(object):
    def __init__(self, sweep_id, project=None, entity=None, function=None, count=None):
        self._sweep_path = sweep_id
        self._sweep_id = None
        self._project = project
        self._entity = entity
        self._function = function
        self._count = count
        # glob_config = os.path.expanduser('~/.config/wandb/settings')
        # loc_config = 'wandb/settings'
        # files = (glob_config, loc_config)
        self._api = InternalApi()
        self._agent_id = None

    def register(self):
        agent = self._api.register_agent(socket.gethostname(), sweep_id=self._sweep_id)
        self._agent_id = agent["id"]

    def check_queue(self):
        run_status = dict()
        commands = self._api.agent_heartbeat(self._agent_id, {}, run_status)
        if not commands:
            return
        command = commands[0]
        job = Job(command)
        return job

    def run_job(self, job):
        run_id = job.run_id

        config_file = os.path.join(
            "wandb", "sweep-" + self._sweep_id, "config-" + run_id + ".yaml"
        )
        config_util.save_config_file_from_dict(config_file, job.config)
        os.environ[wandb.env.RUN_ID] = run_id
        os.environ[wandb.env.CONFIG_PATHS] = config_file
        os.environ[wandb.env.SWEEP_ID] = self._sweep_id
        wandb.setup(_reset=True)

        print(
            "wandb: Agent Starting Run: {} with config:\n".format(run_id)
            + "\n".join(
                ["\t{}: {}".format(k, v["value"]) for k, v in job.config.items()]
            )
        )
        try:
            self._function()
            if wandb.run:
                wandb.join()
        except KeyboardInterrupt as e:
            print("Keyboard interrupt", e)
            return True
        except Exception as e:
            print("Problem", e)
            return True

    def setup(self):
        parts = dict(entity=self._entity, project=self._project, name=self._sweep_path)
        err = util.parse_sweep_id(parts)
        if err:
            wandb.termerror(err)
            return
        entity = parts.get("entity") or self._entity
        project = parts.get("project") or self._project
        sweep_id = parts.get("name") or self._sweep_id

        if entity:
            wandb.env.set_entity(entity)
        if project:
            wandb.env.set_project(project)
        if sweep_id:
            self._sweep_id = sweep_id
        self.register()

    def loop(self):
        self.setup()
        count = 0
        while True:
            job = self.check_queue()
            if not job:
                time.sleep(20)
                continue
            if job.done():
                break
            count += 1
            stop = self.run_job(job)
            if stop:
                break
            if self._count and count >= self._count:
                break
            time.sleep(5)


def agent(sweep_id, function=None, entity=None, project=None, count=None):
    """Generic agent entrypoint, used for CLI or jupyter.

    Args:
        sweep_id (dict): Sweep ID generated by CLI or sweep API
        function (func, optional): A function to call instead of the "program"
        entity (str, optional): W&B Entity
        project (str, optional): W&B Project
        count (int, optional): the number of trials to run.
    """
    if function is None:
        raise Exception("function paramter is required")
    if not callable(function):
        raise Exception("function paramter must be callable")
    agent = Agent(
        sweep_id, function=function, entity=entity, project=project, count=count,
    )
    agent.loop()