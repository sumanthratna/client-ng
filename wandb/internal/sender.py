# -*- coding: utf-8 -*-
"""
sender.
"""

from __future__ import print_function

import collections
from datetime import datetime
import json
import logging
import os
import time

import six
from wandb.filesync.dir_watcher import DirWatcher
from wandb.interface import interface
from wandb.lib.config import save_config_file_from_dict
from wandb.lib.dict import dict_from_proto_list
from wandb.lib.filenames import (
    CONFIG_FNAME,
    EVENTS_FNAME,
    HISTORY_FNAME,
    OUTPUT_FNAME,
    SUMMARY_FNAME,
)
from wandb.proto import wandb_internal_pb2  # type: ignore
from wandb.util import sentry_set_scope


# from wandb.stuff import io_wrap

from . import artifacts
from . import file_stream
from . import internal_api
from . import tb_watcher
from .file_pusher import FilePusher
from .git_repo import GitRepo


logger = logging.getLogger(__name__)


DeferState = collections.namedtuple(  # type: ignore[attr-defined]
    "DeferState", "begin flush_tb flush_dir flush_fp flush_fs end"
)._make(range(6))


def _config_dict_from_proto_list(obj_list):
    d = dict()
    for item in obj_list:
        d[item.key] = dict(desc=None, value=json.loads(item.value_json))
    return d


class SendManager(object):
    def __init__(
        self, settings, process_q, notify_q, resp_q, run_meta=None, system_stats=None
    ):
        self._settings = settings
        self._resp_q = resp_q
        self._run_meta = run_meta
        self._system_stats = system_stats

        self._fs = None
        self._pusher = None
        self._dir_watcher = None
        self._tb_watcher = None

        # State updated by login
        self._entity = None
        self._flags = None

        # State updated by wandb.init
        self._run = None
        self._project = None

        # State updated by resuming
        self._offsets = {
            "step": 0,
            "history": 0,
            "events": 0,
            "output": 0,
            "runtime": 0,
        }

        # State added when run_exit needs results
        self._exit_sync_uuid = None

        # State added when run_exit is complete
        self._exit_result = None

        self._api = internal_api.Api(default_settings=settings)
        self._api_settings = dict()

        # TODO(jhr): do something better, why do we need to send full lines?
        self._partial_output = dict()

        self._interface = interface.BackendSender(
            process_queue=process_q, notify_queue=notify_q,
        )

        self._defer_state = None
        self._exit_code = 0

        # keep track of config and summary from key/val updates
        # self._consolidated_config = dict()
        self._consolidated_summary = dict()

    def send(self, record):
        record_type = record.WhichOneof("record_type")
        if record_type is None:
            print("unknown record")
            return
        handler = getattr(self, "handle_" + record_type, None)
        if handler is None:
            print("unknown handle", record_type)
            return
        handler(record)

    def send_request(self, record):
        request_type = record.request.WhichOneof("request_type")
        if request_type is None:
            print("unknown request")
            return
        handler = getattr(self, "handle_request_" + request_type, None)
        if handler is None:
            print("unknown request handle", request_type)
            return
        handler(record)

    def _flatten(self, dictionary):
        if type(dictionary) == dict:
            for k, v in list(dictionary.items()):
                if type(v) == dict:
                    self._flatten(v)
                    dictionary.pop(k)
                    for k2, v2 in v.items():
                        dictionary[k + "." + k2] = v2

    def handle_request_status(self, data):
        if not data.control.req_resp:
            return

        result = wandb_internal_pb2.Result(uuid=data.uuid)
        status_resp = result.response.status_response
        if data.request.status.check_stop_req:
            status_resp.run_should_stop = False
            if self._entity and self._project and self._run.run_id:
                try:
                    status_resp.run_should_stop = self._api.check_stop_requested(
                        self._project, self._entity, self._run.run_id
                    )
                except Exception as e:
                    logger.warning("Failed to check stop requested status: %s", e)
        self._resp_q.put(result)

    def handle_tbrecord(self, data):
        logger.info("handling tbrecord: %s", data)
        if self._tb_watcher:
            tbrecord = data.tbrecord
            self._tb_watcher.add(tbrecord.log_dir, tbrecord.save)

    def handle_request(self, rec):
        self.send_request(rec)

    def handle_request_login(self, data):
        # TODO: do something with api_key or anonymous?
        # TODO: return an error if we aren't logged in?
        self._api.reauth()
        viewer = self._api.viewer()
        self._flags = json.loads(viewer.get("flags", "{}"))
        self._entity = viewer.get("entity")
        if data.control.req_resp:
            result = wandb_internal_pb2.Result(uuid=data.uuid)
            result.response.login_response.active_entity = self._entity
            self._resp_q.put(result)

    def handle_exit(self, data):
        exit = data.exit
        self._exit_code = exit.exit_code

        logger.info("handling exit code: %s", exit.exit_code)

        # Pass the responsibility to respond to handle_request_defer()
        if data.control.req_resp:
            self._exit_sync_uuid = data.uuid

        # We need to give the request queue a chance to empty between states
        # so use handle_request_defer as a state machine.
        self._defer_state = DeferState.begin
        logger.info("send defer: {}".format(self._defer_state))
        self._interface.send_defer()

    def handle_request_defer(self, data):
        logger.info("handle defer: {}".format(self._defer_state))

        done = False
        state = self._defer_state
        if state == DeferState.begin:
            pass
        elif state == DeferState.flush_tb:
            # shutdown tensorboard workers so we get all metrics flushed
            if self._tb_watcher:
                self._tb_watcher.finish()
                self._tb_watcher = None
        elif state == DeferState.flush_dir:
            if self._dir_watcher:
                self._dir_watcher.finish()
                self._dir_watcher = None
        elif state == DeferState.flush_fp:
            if self._pusher:
                self._pusher.finish()
        elif state == DeferState.flush_fs:
            if self._fs:
                # TODO(jhr): now is a good time to output pending output lines
                self._fs.finish(self._exit_code)
                self._fs = None
        elif state == DeferState.end:
            done = True
        else:
            raise AssertionError("unknown state")

        if not done:
            self._defer_state += 1
            logger.info("send defer: {}".format(self._defer_state))
            self._interface.send_defer()
            return

        exit_result = wandb_internal_pb2.RunExitResult()

        # This path is not the prefered method to return exit results
        # as it could take a long time to flush the file pusher buffers
        if self._exit_sync_uuid:
            if self._pusher:
                # NOTE: This will block until finished
                self._pusher.print_status()
                self._pusher.join()
                self._pusher = None
            resp = wandb_internal_pb2.Result(
                exit_result=exit_result, uuid=self._exit_sync_uuid
            )
            self._resp_q.put(resp)

        # mark exit done in case we are polling on exit
        self._exit_result = exit_result

    def handle_request_poll_exit(self, data):
        if not data.control.req_resp:
            return

        result = wandb_internal_pb2.Result(uuid=data.uuid)

        alive = False
        if self._pusher:
            alive, status = self._pusher.get_status()
            file_counts = self._pusher.file_counts_by_category()
            resp = result.response.poll_exit_response
            resp.pusher_stats.uploaded_bytes = status["uploaded_bytes"]
            resp.pusher_stats.total_bytes = status["total_bytes"]
            resp.pusher_stats.deduped_bytes = status["deduped_bytes"]
            resp.file_counts.wandb_count = file_counts["wandb"]
            resp.file_counts.media_count = file_counts["media"]
            resp.file_counts.artifact_count = file_counts["artifact"]
            resp.file_counts.other_count = file_counts["other"]

        if self._exit_result and not alive:
            # pusher join should not block as it was reported as not alive
            self._pusher.join()
            result.response.poll_exit_response.exit_result.CopyFrom(self._exit_result)
            result.response.poll_exit_response.done = True
        self._resp_q.put(result)

    def _maybe_setup_resume(self, run):
        """This maybe queries the backend for a run and fails if the settings are
        incompatible."""
        error = None
        if self._settings.resume:
            # TODO: This causes a race, we need to make the upsert atomically
            # only create or update depending on the resume config
            # we use the runs entity if set, otherwise fallback to users entity
            entity = run.entity or self._entity
            logger.info(
                "checking resume status for %s/%s/%s", entity, run.project, run.run_id
            )
            resume_status = self._api.run_resume_status(
                entity=entity, project_name=run.project, name=run.run_id
            )
            logger.info("resume status %s", resume_status)
            if resume_status is None:
                if self._settings.resume == "must":
                    error = wandb_internal_pb2.ErrorInfo()
                    error.code = wandb_internal_pb2.ErrorInfo.ErrorCode.INVALID
                    error.message = (
                        "resume='must' but run (%s) doesn't exist" % run.run_id
                    )
            else:
                if self._settings.resume == "never":
                    error = wandb_internal_pb2.ErrorInfo()
                    error.code = wandb_internal_pb2.ErrorInfo.ErrorCode.INVALID
                    error.message = "resume='never' but run (%s) exists" % run.run_id
                elif self._settings.resume in ("allow", "auto"):
                    history = {}
                    events = {}
                    try:
                        history = json.loads(
                            json.loads(resume_status["historyTail"])[-1]
                        )
                        events = json.loads(json.loads(resume_status["eventsTail"])[-1])
                    except (IndexError, ValueError) as e:
                        logger.error("unable to load resume tails", exc_info=e)
                    # TODO: Do we need to restore config / summary?
                    # System metrics runtime is usually greater than history
                    events_rt = events.get("_runtime", 0)
                    history_rt = history.get("_runtime", 0)
                    self._offsets["runtime"] = max(events_rt, history_rt)
                    self._offsets["step"] = history.get("_step", -1) + 1
                    self._offsets["history"] = resume_status["historyLineCount"]
                    self._offsets["events"] = resume_status["eventsLineCount"]
                    self._offsets["output"] = resume_status["logLineCount"]
                    logger.info("configured resuming with: %s" % self._offsets)
        return error

    def handle_run(self, data):
        run = data.run
        run_tags = run.tags[:]
        error = None
        is_wandb_init = self._run is None

        # build config dict
        config_dict = None
        if run.HasField("config"):
            config_dict = _config_dict_from_proto_list(run.config.update)
            config_path = os.path.join(self._settings.files_dir, CONFIG_FNAME)
            save_config_file_from_dict(config_path, config_dict)

        repo = GitRepo(remote=self._settings.git_remote)

        if is_wandb_init:
            # Only check resume status on `wandb.init`
            error = self._maybe_setup_resume(run)

        if error is not None:
            if data.control.req_resp:
                resp = wandb_internal_pb2.Result(uuid=data.uuid)
                resp.run_result.run.CopyFrom(run)
                resp.run_result.error.CopyFrom(error)
                self._resp_q.put(resp)
            else:
                logger.error("Got error in async mode: %s", error.message)
            return

        # TODO: we don't check inserted currently, ultimately we should make
        # the upsert know the resume state and fail transactionally
        ups, inserted = self._api.upsert_run(
            name=run.run_id,
            entity=run.entity or None,
            project=run.project or None,
            group=run.run_group or None,
            job_type=run.job_type or None,
            display_name=run.display_name or None,
            notes=run.notes or None,
            tags=run_tags or None,
            config=config_dict or None,
            sweep_name=run.sweep_id or None,
            host=run.host or None,
            program_path=self._settings.program or None,
            repo=repo.remote_url,
            commit=repo.last_commit,
        )

        # We subtract the previous runs runtime when resuming
        start_time = run.start_time.ToSeconds() - self._offsets["runtime"]
        self._run = run
        self._run.starting_step = self._offsets["step"]
        self._run.start_time.FromSeconds(start_time)
        storage_id = ups.get("id")
        if storage_id:
            self._run.storage_id = storage_id
        display_name = ups.get("displayName")
        if display_name:
            self._run.display_name = display_name
        project = ups.get("project")
        if project:
            project_name = project.get("name")
            if project_name:
                self._run.project = project_name
                self._project = project_name
            entity = project.get("entity")
            if entity:
                entity_name = entity.get("name")
                if entity_name:
                    self._run.entity = entity_name
                    self._entity = entity_name

        if data.control.req_resp:
            resp = wandb_internal_pb2.Result(uuid=data.uuid)
            resp.run_result.run.CopyFrom(self._run)
            self._resp_q.put(resp)

        if self._entity is not None:
            self._api_settings["entity"] = self._entity
        if self._project is not None:
            self._api_settings["project"] = self._project

        # Only spin up our threads on the first run message
        if is_wandb_init:
            self._fs = file_stream.FileStreamApi(
                self._api, run.run_id, start_time, settings=self._api_settings
            )
            # Ensure the streaming polices have the proper offsets
            self._fs.set_file_policy(
                "wandb-summary.json", file_stream.SummaryFilePolicy()
            )
            self._fs.set_file_policy(
                "wandb-history.jsonl",
                file_stream.JsonlFilePolicy(start_chunk_id=self._offsets["history"]),
            )
            self._fs.set_file_policy(
                "wandb-events.jsonl",
                file_stream.JsonlFilePolicy(start_chunk_id=self._offsets["events"]),
            )
            self._fs.set_file_policy(
                "output.log",
                file_stream.CRDedupeFilePolicy(start_chunk_id=self._offsets["output"]),
            )
            self._fs.start()
            self._pusher = FilePusher(self._api)
            self._dir_watcher = DirWatcher(self._settings, self._api, self._pusher)
            self._tb_watcher = tb_watcher.TBWatcher(self._settings, sender=self)
            if self._run_meta:
                self._run_meta.write()
            sentry_set_scope("internal", run.entity, run.project)
            logger.info(
                "run started: %s with start time %s", self._run.run_id, start_time
            )
        else:
            logger.info("updated run: %s", self._run.run_id)

    def _save_history(self, history_dict):
        if self._fs:
            # print("\n\nABOUT TO SAVE:\n", history_dict, "\n\n")
            self._fs.push(HISTORY_FNAME, json.dumps(history_dict))
            # print("got", x)
        # save history into summary
        self._consolidated_summary.update(history_dict)
        self._save_summary(self._consolidated_summary)

    def handle_history(self, data):
        history = data.history
        history_dict = dict_from_proto_list(history.item)
        self._save_history(history_dict)

    def _save_summary(self, summary_dict):
        json_summary = json.dumps(summary_dict)
        if self._fs:
            self._fs.push(SUMMARY_FNAME, json_summary)
        summary_path = os.path.join(self._settings.files_dir, SUMMARY_FNAME)
        with open(summary_path, "w") as f:
            f.write(json_summary)
            self._save_file(SUMMARY_FNAME)

    def handle_summary(self, data):
        summary = data.summary

        for item in summary.update:
            if len(item.nested_key) > 0:
                # we use either key or nested_key -- not both
                assert item.key == ""
                key = tuple(item.nested_key)
            else:
                # no counter-assertion here, because technically
                # summary[""] is valid
                key = (item.key,)

            target = self._consolidated_summary

            # recurse down the dictionary structure:
            for prop in key[:-1]:
                target = target[prop]

            # use the last element of the key to write the leaf:
            target[key[-1]] = json.loads(item.value_json)

        for item in summary.remove:
            if len(item.nested_key) > 0:
                # we use either key or nested_key -- not both
                assert item.key == ""
                key = tuple(item.nested_key)
            else:
                # no counter-assertion here, because technically
                # summary[""] is valid
                key = (item.key,)

            target = self._consolidated_summary

            # recurse down the dictionary structure:
            for prop in key[:-1]:
                target = target[prop]

            # use the last element of the key to erase the leaf:
            del target[key[-1]]

        self._save_summary(self._consolidated_summary)

    def handle_stats(self, data):
        stats = data.stats
        if stats.stats_type != wandb_internal_pb2.StatsRecord.StatsType.SYSTEM:
            return
        if not self._fs:
            return
        now = stats.timestamp.seconds
        d = dict()
        for item in stats.item:
            d[item.key] = json.loads(item.value_json)
        row = dict(system=d)
        self._flatten(row)
        row["_wandb"] = True
        row["_timestamp"] = now
        row["_runtime"] = int(now - self._run.start_time.ToSeconds())
        self._fs.push(EVENTS_FNAME, json.dumps(row))
        # TODO(jhr): check fs.push results?

    def handle_output(self, data):
        if not self._fs:
            return
        out = data.output
        prepend = ""
        stream = "stdout"
        if out.output_type == wandb_internal_pb2.OutputRecord.OutputType.STDERR:
            stream = "stderr"
            prepend = "ERROR "
        line = out.line
        if not line.endswith("\n"):
            self._partial_output.setdefault(stream, "")
            self._partial_output[stream] += line
            # TODO(jhr): how do we make sure this gets flushed?
            # we might need this for other stuff like telemetry
        else:
            # TODO(jhr): use time from timestamp proto
            # TODO(jhr): do we need to make sure we write full lines?
            # seems to be some issues with line breaks
            cur_time = time.time()
            timestamp = datetime.utcfromtimestamp(cur_time).isoformat() + " "
            prev_str = self._partial_output.get(stream, "")
            line = u"{}{}{}{}".format(prepend, timestamp, prev_str, line)
            self._fs.push(OUTPUT_FNAME, line)
            self._partial_output[stream] = ""

    def handle_config(self, data):
        cfg = data.config
        config_dict = _config_dict_from_proto_list(cfg.update)
        self._api.upsert_run(
            name=self._run.run_id, config=config_dict, **self._api_settings
        )
        config_path = os.path.join(self._settings.files_dir, "config.yaml")
        save_config_file_from_dict(config_path, config_dict)
        # TODO(jhr): check result of upsert_run?

    def _save_file(self, fname, policy="end"):
        logger.info("saving file %s with policy %s", fname, policy)
        self._dir_watcher.update_policy(fname, policy)

    def handle_files(self, data):
        files = data.files
        for k in files.files:
            # TODO(jhr): fix paths with directories
            self._save_file(k.path, interface.file_enum_to_policy(k.policy))

    def handle_artifact(self, data):
        artifact = data.artifact
        saver = artifacts.ArtifactSaver(
            api=self._api,
            digest=artifact.digest,
            manifest_json=artifacts._manifest_json_from_proto(artifact.manifest),
            file_pusher=self._pusher,
            is_user_created=artifact.user_created,
        )

        saver.save(
            type=artifact.type,
            name=artifact.name,
            metadata=artifact.metadata,
            description=artifact.description,
            aliases=artifact.aliases,
            use_after_commit=artifact.use_after_commit,
        )

    def handle_request_get_summary(self, data):
        result = wandb_internal_pb2.Result(uuid=data.uuid)
        for key, value in six.iteritems(self._consolidated_summary):
            item = wandb_internal_pb2.SummaryItem()
            item.key = key
            item.value_json = json.dumps(value)
            result.response.get_summary_response.item.append(item)
        self._resp_q.put(result)

    def handle_request_resume(self, data):
        if self._system_stats is not None:
            logger.info("starting system metrics thread")
            self._system_stats.start()

    def handle_request_pause(self, data):
        if self._system_stats is not None:
            logger.info("stopping system metrics thread")
            self._system_stats.shutdown()

    def finish(self):
        logger.info("shutting down sender")
        if self._tb_watcher:
            self._tb_watcher.finish()
        if self._dir_watcher:
            self._dir_watcher.finish()
        if self._pusher:
            self._pusher.finish()
            self._pusher.join()
        if self._fs:
            self._fs.finish(self._exit_code)
