#!/usr/bin/env python
# -*- coding: utf-8 -*-

import copy
from functools import wraps
import getpass
import logging
import os
import subprocess
import sys
import textwrap
import time
import traceback

import click
from click.exceptions import ClickException
# pycreds has a find_executable that works in windows
from dockerpycreds.utils import find_executable
import six
import wandb
from wandb import env, util
from wandb import Error
from wandb import wandb_agent
from wandb import wandb_controller
from wandb.apis import InternalApi, PublicApi
from wandb.old.settings import Settings
from wandb.sync import SyncManager
import yaml

# whaaaaat depends on prompt_toolkit < 2, ipython now uses > 2 so we vendored for now
# DANGER this changes the sys.path so we should never do this in a user script
whaaaaat = util.vendor_import("whaaaaat")


logger = logging.getLogger("wandb")

CONTEXT = dict(default_map={})


def cli_unsupported(argument):
    wandb.termerror("Unsupported argument `{}`".format(argument))
    sys.exit(1)


class ClickWandbException(ClickException):
    def format_message(self):
        # log_file = util.get_log_file_path()
        log_file = ""
        orig_type = '{}.{}'.format(self.orig_type.__module__,
                                   self.orig_type.__name__)
        if issubclass(self.orig_type, Error):
            return click.style(str(self.message), fg="red")
        else:
            return ('An Exception was raised, see %s for full traceback.\n'
                    '%s: %s' % (log_file, orig_type, self.message))


def display_error(func):
    """Function decorator for catching common errors and re-raising as wandb.Error"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except wandb.Error as e:
            exc_type, exc_value, exc_traceback = sys.exc_info()
            lines = traceback.format_exception(
                exc_type, exc_value, exc_traceback)
            logger.error(''.join(lines))
            click_exc = ClickWandbException(e)
            click_exc.orig_type = exc_type
            six.reraise(ClickWandbException, click_exc, sys.exc_info()[2])
    return wrapper


def _get_cling_api():
    """Get a reference to the internal api with cling settings."""
    # TODO(jhr): make a settings object that is better for non runs.
    wandb.setup(settings=wandb.Settings(_cli_only_mode=True))
    api = InternalApi()
    return api


def prompt_for_project(ctx, entity):
    """Ask the user for a project, creating one if necessary."""
    result = ctx.invoke(projects, entity=entity, display=False)
    api = _get_cling_api()

    try:
        if len(result) == 0:
            project = click.prompt("Enter a name for your first project")
            # description = editor()
            project = api.upsert_project(project, entity=entity)["name"]
        else:
            project_names = [project["name"] for project in result]
            question = {
                'type': 'list',
                'name': 'project_name',
                'message': "Which project should we use?",
                'choices': project_names + ["Create New"]
            }
            result = whaaaaat.prompt([question])
            if result:
                project = result['project_name']
            else:
                project = "Create New"
            # TODO: check with the server if the project exists
            if project == "Create New":
                project = click.prompt(
                    "Enter a name for your new project", value_proc=api.format_project)
                # description = editor()
                project = api.upsert_project(project, entity=entity)["name"]

    except wandb.errors.error.CommError as e:
        raise ClickException(str(e))

    return project


class RunGroup(click.Group):
    @display_error
    def get_command(self, ctx, cmd_name):
        # TODO: check if cmd_name is a file in the current dir and not require `run`?
        rv = click.Group.get_command(self, ctx, cmd_name)
        if rv is not None:
            return rv
        return None


@click.command(cls=RunGroup, invoke_without_command=True)
@click.version_option(version=wandb.__version__)
@click.pass_context
def cli(ctx):
    # wandb.try_to_set_up_global_logging()
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command(context_settings=CONTEXT, help="List projects", hidden=True)
@click.option("--entity", "-e", default=None, envvar=env.ENTITY, help="The entity to scope the listing to.")
@display_error
def projects(entity, display=True):
    api = _get_cling_api()
    projects = api.list_projects(entity=entity)
    if len(projects) == 0:
        message = "No projects found for %s" % entity
    else:
        message = 'Latest projects for "%s"' % entity
    if display:
        click.echo(click.style(message, bold=True))
        for project in projects:
            click.echo("".join(
                (click.style(project['name'], fg="blue", bold=True),
                 " - ",
                 str(project['description'] or "").split("\n")[0])
            ))
    return projects


@cli.command(context_settings=CONTEXT, help="Login to Weights & Biases")
@click.argument("key", nargs=-1)
@click.option("--cloud", is_flag=True, help="Login to the cloud instead of local")
@click.option("--host", default=None, help="Login to a specific instance of W&B")
@click.option("--relogin", default=None, is_flag=True, help="Force relogin if already logged in.")
@click.option("--anonymously", default=False, is_flag=True, help="Log in anonymously")
@display_error
def login(key, host, cloud, relogin, anonymously):
    anon_mode = "must" if anonymously else "never"
    wandb.setup(settings=wandb.Settings(_cli_only_mode=True, anonymous=anon_mode))
    api = _get_cling_api()

    if host == "https://api.wandb.ai" or (host is None and cloud):
        api.clear_setting("base_url", globally=True, persist=True)
        # To avoid writing an empty local settings file, we only clear if it exists
        if os.path.exists(Settings._local_path()):
            api.clear_setting("base_url", persist=True)
    elif host:
        if not host.startswith("http"):
            raise ClickException("host must start with http(s)://")
        api.set_setting("base_url", host.strip("/"), globally=True, persist=True)
    key = key[0] if len(key) > 0 else None

    wandb.login(relogin=relogin, key=key, anonymous=anon_mode)


@cli.command(context_settings=CONTEXT, help="Run a grpc server", name="grpc-server", hidden=True)
@display_error
def grpc_server(project=None, entity=None):
    _ = util.get_module(
        "grpc",
        required="grpc-server requires the grpcio library, run pip install wandb[grpc]",
    )
    from wandb.server.grpc_server import main as grpc_server
    grpc_server()


@cli.command(context_settings=CONTEXT, help="Run a SUPER agent", hidden=True)
@click.option("--project", "-p", default=None, help="The project use.")
@click.option("--entity", "-e", default=None, help="The entity to use.")
@click.argument('agent_spec', nargs=-1)
@display_error
def superagent(project=None, entity=None, agent_spec=None):
    wandb.superagent.run_agent(agent_spec)


@cli.command(context_settings=CONTEXT, help="Configure a directory with Weights & Biases")
@click.option("--project", "-p", help="The project to use.")
@click.option("--entity", "-e", help="The entity to scope the project to.")
# TODO(jhr): Enable these with settings rework
# @click.option("--setting", "-s", help="enable an arbitrary setting.", multiple=True)
# @click.option('--show', is_flag=True, help="Show settings")
@click.option('--reset', is_flag=True, help="Reset settings")
@click.pass_context
@display_error
def init(ctx, project, entity, reset):
    from wandb.old.core import _set_stage_dir, __stage_dir__, wandb_dir
    if __stage_dir__ is None:
        _set_stage_dir('wandb')

    # non interactive init
    if reset or project or entity:
        api = InternalApi()
        if reset:
            api.clear_setting("entity", persist=True)
            api.clear_setting("project", persist=True)
            # TODO(jhr): clear more settings?
        if entity:
            api.set_setting('entity', entity, persist=True)
        if project:
            api.set_setting('project', project, persist=True)
        return

    if os.path.isdir(wandb_dir()) and os.path.exists(os.path.join(wandb_dir(), "settings")):
        click.confirm(click.style(
            "This directory has been configured previously, should we re-configure it?", bold=True), abort=True)
    else:
        click.echo(click.style(
            "Let's setup this directory for W&B!", fg="green", bold=True))
    api = InternalApi()
    if api.api_key is None:
        ctx.invoke(login)

    viewer = api.viewer()

    # Viewer can be `None` in case your API information became invalid, or
    # in testing if you switch hosts.
    if not viewer:
        click.echo(click.style(
            "Your login information seems to be invalid: can you log in again please?", fg="red", bold=True))
        ctx.invoke(login)

    # This shouldn't happen.
    viewer = api.viewer()
    if not viewer:
        click.echo(click.style(
            "We're sorry, there was a problem logging you in. Please send us a note at support@wandb.com and tell us how this happened.", fg="red", bold=True))
        sys.exit(1)

    # At this point we should be logged in successfully.
    if len(viewer["teams"]["edges"]) > 1:
        team_names = [e["node"]["name"] for e in viewer["teams"]["edges"]]
        question = {
            'type': 'list',
            'name': 'team_name',
            'message': "Which team should we use?",
            'choices': team_names
            # TODO(jhr): disabling manual entry for cling
            # 'choices': team_names + ["Manual Entry"]
        }
        result = whaaaaat.prompt([question])
        # result can be empty on click
        if result:
            entity = result['team_name']
        else:
            entity = "Manual Entry"
        if entity == "Manual Entry":
            entity = click.prompt("Enter the name of the team you want to use")
    else:
        entity = viewer.get('entity') or click.prompt("What username or team should we use?")

    # TODO: this error handling sucks and the output isn't pretty
    try:
        project = prompt_for_project(ctx, entity)
    except ClickWandbException:
        raise ClickException('Could not find team: %s' % entity)

    api.set_setting('entity', entity, persist=True)
    api.set_setting('project', project, persist=True)
    api.set_setting('base_url', api.settings().get('base_url'), persist=True)

    util.mkdir_exists_ok(wandb_dir())
    with open(os.path.join(wandb_dir(), '.gitignore'), "w") as file:
        file.write("*\n!settings")

    click.echo(click.style("This directory is configured!  Next, track a run:\n",
               fg="green") + textwrap.dedent("""\
        * In your training script:
            {code1}
            {code2}
        * then `{run}`.
        """).format(
        code1=click.style("import wandb", bold=True),
        code2=click.style("wandb.init(project=\"%s\")" % project, bold=True),
        run=click.style("python <train.py>", bold=True),
    ))


@cli.command(context_settings=CONTEXT,
             help="Upload an offline training directory to W&B")
@click.pass_context
@click.argument("path", nargs=-1, type=click.Path(exists=True))
@click.option("--id", help="The run you want to upload to.")
@click.option("--project", "-p", help="The project you want to upload to.")
@click.option("--entity", "-e", help="The entity to scope to.")
@click.option("--ignore",
              help="A comma seperated list of globs to ignore syncing with wandb.")
@click.option('--all', is_flag=True, default=False, help="Sync all runs")
@display_error
def sync(ctx, path, id, project, entity, ignore, all):
    if ignore:
        ignore = ignore.split(",")
    sm = SyncManager(project=project, entity=entity, run_id=id, ignore=ignore)
    if not path:
        # Show listing of possible paths to sync
        # (if interactive, allow user to pick run to sync)
        sync_items = sm.list()
        if not sync_items:
            wandb.termerror("Nothing to sync")
            return
        if not all:
            wandb.termlog("NOTE: use sync --all to sync all unsynced runs")
            wandb.termlog("Number of runs to be synced: {}".format(len(sync_items)))
            some_runs = 5
            if some_runs < len(sync_items):
                wandb.termlog("Showing {} runs".format(some_runs))
            for item in sync_items[:some_runs]:
                wandb.termlog("  {}".format(item))
            return
        path = sync_items
    if id and len(path) > 1:
        wandb.termerror("id can only be set for a single run")
        sys.exit(1)
    for p in path:
        sm.add(p)
    sm.start()
    while not sm.is_done():
        _ = sm.poll()
        # print(status)


@cli.command(context_settings=CONTEXT, help="Create a sweep")  # noqa: C901
@click.pass_context
@click.option("--project", "-p", default=None, help="The project of the sweep.")
@click.option("--entity", "-e", default=None, help="The entity scope for the project.")
@click.option('--controller', is_flag=True, default=False, help="Run local controller")
@click.option('--verbose', is_flag=True, default=False, help="Display verbose output")
@click.option('--name', default=False, help="Set sweep name")
@click.option('--program', default=False, help="Set sweep program")
@click.option('--settings', default=False, help="Set sweep settings", hidden=True)
@click.option('--update', default=None, help="Update pending sweep")
@click.argument('config_yaml')
@display_error
def sweep(ctx, project, entity, controller, verbose, name, program, settings, update, config_yaml):  # noqa: C901
    def _parse_settings(settings):
        """settings could be json or comma seperated assignments."""
        ret = {}
        # TODO(jhr): merge with magic_impl:_parse_magic
        if settings.find('=') > 0:
            for item in settings.split(","):
                kv = item.split("=")
                if len(kv) != 2:
                    wandb.termwarn("Unable to parse sweep settings key value pair", repeat=False)
                ret.update(dict([kv]))
            return ret
        wandb.termwarn("Unable to parse settings parameter", repeat=False)
        return ret

    api = InternalApi()
    if api.api_key is None:
        wandb.termlog("Login to W&B to use the sweep feature")
        ctx.invoke(login, no_offline=True)

    sweep_obj_id = None
    if update:
        parts = dict(entity=entity, project=project, name=update)
        err = util.parse_sweep_id(parts)
        if err:
            wandb.termerror(err)
            return
        entity = parts.get("entity") or entity
        project = parts.get("project") or project
        sweep_id = parts.get("name") or update
        found = api.sweep(sweep_id, '{}', entity=entity, project=project)
        if not found:
            wandb.termerror('Could not find sweep {}/{}/{}'.format(entity, project, sweep_id))
            return
        sweep_obj_id = found['id']

    wandb.termlog('{} sweep from: {}'.format(
        'Updating' if sweep_obj_id else 'Creating', config_yaml))
    try:
        yaml_file = open(config_yaml)
    except OSError:
        wandb.termerror('Couldn\'t open sweep file: %s' % config_yaml)
        return
    try:
        config = util.load_yaml(yaml_file)
    except yaml.YAMLError as err:
        wandb.termerror('Error in configuration file: %s' % err)
        return
    if config is None:
        wandb.termerror('Configuration file is empty')
        return

    # Set or override parameters
    if name:
        config["name"] = name
    if program:
        config["program"] = program
    if settings:
        settings = _parse_settings(settings)
        if settings:
            config.setdefault("settings", {})
            config["settings"].update(settings)
    if controller:
        config.setdefault("controller", {})
        config["controller"]["type"] = "local"

    is_local = config.get('controller', {}).get('type') == 'local'
    if is_local:
        tuner = wandb_controller.controller()
        err = tuner._validate(config)
        if err:
            wandb.termerror('Error in sweep file: %s' % err)
            return

    env = os.environ
    entity = entity or env.get("WANDB_ENTITY") or config.get('entity')
    project = project or env.get("WANDB_PROJECT") or config.get('project') or util.auto_project_name(
        config.get("program"))
    sweep_id = api.upsert_sweep(config, project=project, entity=entity, obj_id=sweep_obj_id)
    wandb.termlog('{} sweep with ID: {}'.format(
        'Updated' if sweep_obj_id else 'Created',
        click.style(sweep_id, fg="yellow")))
    sweep_url = wandb_controller._get_sweep_url(api, sweep_id)
    if sweep_url:
        wandb.termlog("View sweep at: {}".format(
            click.style(sweep_url, underline=True, fg='blue')))

    # reprobe entity and project if it was autodetected by upsert_sweep
    entity = entity or env.get("WANDB_ENTITY")
    project = project or env.get("WANDB_PROJECT")

    if entity and project:
        sweep_path = "{}/{}/{}".format(entity, project, sweep_id)
    elif project:
        sweep_path = "{}/{}".format(project, sweep_id)
    else:
        sweep_path = sweep_id

    if sweep_path.find(' ') >= 0:
        sweep_path = '"{}"'.format(sweep_path)

    wandb.termlog("Run sweep agent with: {}".format(
        click.style("wandb agent %s" % sweep_path, fg="yellow")))
    if controller:
        wandb.termlog('Starting wandb controller...')
        tuner = wandb_controller.controller(sweep_id)
        tuner.run(verbose=verbose)


@cli.command(context_settings=CONTEXT, help="Run the W&B agent")
@click.pass_context
@click.option("--project", "-p", default=None, help="The project of the sweep.")
@click.option("--entity", "-e", default=None, help="The entity scope for the project.")
@click.option("--count", default=None, type=int, help="The max number of runs for this agent.")
@click.argument('sweep_id')
@display_error
def agent(ctx, project, entity, count, sweep_id):
    api = InternalApi()
    if api.api_key is None:
        wandb.termlog("Login to W&B to use the sweep agent feature")
        ctx.invoke(login, no_offline=True)

    wandb.termlog('Starting wandb agent 🕵️')
    wandb_agent.run_agent(sweep_id, entity=entity, project=project, count=count)

    # you can send local commands like so:
    # agent_api.command({'type': 'run', 'program': 'train.py',
    #                'args': ['--max_epochs=10']})


@cli.command(context_settings=CONTEXT, help="Run the W&B local sweep controller")
@click.option('--verbose', is_flag=True, default=False, help="Display verbose output")
@click.argument('sweep_id')
@display_error
def controller(verbose, sweep_id):
    click.echo('Starting wandb controller...')
    tuner = wandb_controller.controller(sweep_id)
    tuner.run(verbose=verbose)


RUN_CONTEXT = copy.copy(CONTEXT)
RUN_CONTEXT['allow_extra_args'] = True
RUN_CONTEXT['ignore_unknown_options'] = True


@cli.command(context_settings=RUN_CONTEXT, name="docker-run")
@click.pass_context
@click.argument('docker_run_args', nargs=-1)
@click.option('--help', is_flag=True)
def docker_run(ctx, docker_run_args, help):
    """Simple wrapper for `docker run` which sets W&B environment
    Adds WANDB_API_KEY and WANDB_DOCKER to any docker run command.
    This will also set the runtime to nvidia if the nvidia-docker executable is present on the system
    and --runtime wasn't set.
    """
    api = InternalApi()
    args = list(docker_run_args)
    if len(args) > 0 and args[0] == "run":
        args.pop(0)
    if help or len(args) == 0:
        wandb.termlog("This commands adds wandb env variables to your docker run calls")
        subprocess.call(['docker', 'run'] + args + ['--help'])
        exit()
    #  TODO: is this what we want?
    if len([a for a in args if a.startswith("--runtime")]) == 0 and find_executable('nvidia-docker'):
        args = ["--runtime", "nvidia"] + args
    #  TODO: image_from_docker_args uses heuristics to find the docker image arg, there are likely cases
    #  where this won't work
    image = util.image_from_docker_args(args)
    resolved_image = None
    if image:
        resolved_image = wandb.docker.image_id(image)
    if resolved_image:
        args = ['-e', 'WANDB_DOCKER=%s' % resolved_image] + args
    else:
        wandb.termlog("Couldn't detect image argument, running command without the WANDB_DOCKER env variable")
    if api.api_key:
        args = ['-e', 'WANDB_API_KEY=%s' % api.api_key] + args
    else:
        wandb.termlog("Not logged in, run `wandb login` from the host machine to enable result logging")
    subprocess.call(['docker', 'run'] + args)


@cli.command(context_settings=RUN_CONTEXT)
@click.pass_context
@click.argument('docker_run_args', nargs=-1)
@click.argument('docker_image', required=False)
@click.option('--nvidia/--no-nvidia', default=find_executable('nvidia-docker') is not None,
              help='Use the nvidia runtime, defaults to nvidia if nvidia-docker is present')
@click.option('--digest', is_flag=True, default=False, help="Output the image digest and exit")
@click.option('--jupyter/--no-jupyter', default=False, help="Run jupyter lab in the container")
@click.option('--dir', default="/app", help="Which directory to mount the code in the container")
@click.option('--no-dir', is_flag=True, help="Don't mount the current directory")
@click.option('--shell', default="/bin/bash", help="The shell to start the container with")
@click.option('--port', default="8888", help="The host port to bind jupyter on")
@click.option('--cmd', help="The command to run in the container")
@click.option('--no-tty', is_flag=True, default=False, help="Run the command without a tty")
@display_error
def docker(ctx, docker_run_args, docker_image, nvidia, digest, jupyter, dir, no_dir, shell, port, cmd, no_tty):
    """W&B docker lets you run your code in a docker image ensuring wandb is configured. It adds the WANDB_DOCKER and WANDB_API_KEY
    environment variables to your container and mounts the current directory in /app by default.  You can pass additional
    args which will be added to `docker run` before the image name is declared, we'll choose a default image for you if
    one isn't passed:

    wandb docker -v /mnt/dataset:/app/data
    wandb docker gcr.io/kubeflow-images-public/tensorflow-1.12.0-notebook-cpu:v0.4.0 --jupyter
    wandb docker wandb/deepo:keras-gpu --no-tty --cmd "python train.py --epochs=5"

    By default we override the entrypoint to check for the existance of wandb and install it if not present.  If you pass the --jupyter
    flag we will ensure jupyter is installed and start jupyter lab on port 8888.  If we detect nvidia-docker on your system we will use
    the nvidia runtime.  If you just want wandb to set environment variable to an existing docker run command, see the wandb docker-run
    command.
    """
    api = InternalApi()
    if not find_executable('docker'):
        raise ClickException(
            "Docker not installed, install it from https://docker.com")
    args = list(docker_run_args)
    image = docker_image or ""
    # remove run for users used to nvidia-docker
    if len(args) > 0 and args[0] == "run":
        args.pop(0)
    if image == "" and len(args) > 0:
        image = args.pop(0)
    # If the user adds docker args without specifying an image (should be rare)
    if not util.docker_image_regex(image.split("@")[0]):
        if image:
            args = args + [image]
        image = wandb.docker.default_image(gpu=nvidia)
        subprocess.call(["docker", "pull", image])
    _, repo_name, tag = wandb.docker.parse(image)

    resolved_image = wandb.docker.image_id(image)
    if resolved_image is None:
        raise ClickException(
            "Couldn't find image locally or in a registry, try running `docker pull %s`" % image)
    if digest:
        sys.stdout.write(resolved_image)
        exit(0)

    existing = wandb.docker.shell(
        ["ps", "-f", "ancestor=%s" % resolved_image, "-q"])
    if existing:
        if click.confirm("Found running container with the same image, do you want to attach?"):
            subprocess.call(['docker', 'attach', existing.split("\n")[0]])
            exit(0)
    cwd = os.getcwd()
    command = ['docker', 'run', '-e', 'LANG=C.UTF-8', '-e', 'WANDB_DOCKER=%s' % resolved_image, '--ipc=host',
               '-v', wandb.docker.entrypoint + ':/wandb-entrypoint.sh', '--entrypoint', '/wandb-entrypoint.sh']
    if nvidia:
        command.extend(['--runtime', 'nvidia'])
    if not no_dir:
        #  TODO: We should default to the working directory if defined
        command.extend(['-v', cwd + ":" + dir, '-w', dir])
    if api.api_key:
        command.extend(['-e', 'WANDB_API_KEY=%s' % api.api_key])
    else:
        wandb.termlog("Couldn't find WANDB_API_KEY, run `wandb login` to enable streaming metrics")
    if jupyter:
        command.extend(['-e', 'WANDB_ENSURE_JUPYTER=1', '-p', port + ':8888'])
        no_tty = True
        cmd = "jupyter lab --no-browser --ip=0.0.0.0 --allow-root --NotebookApp.token= --notebook-dir %s" % dir
    command.extend(args)
    if no_tty:
        command.extend([image, shell, "-c", cmd])
    else:
        if cmd:
            command.extend(['-e', 'WANDB_COMMAND=%s' % cmd])
        command.extend(['-it', image, shell])
        wandb.termlog("Launching docker container \U0001F6A2")
    subprocess.call(command)


@cli.command(context_settings=RUN_CONTEXT, help="Launch local W&B container (Experimental)")
@click.pass_context
@click.option('--port', '-p', default="8080", help="The host port to bind W&B local on")
@click.option('--env', '-e', default=[], multiple=True, help="Env vars to pass to wandb/local")
@click.option('--daemon/--no-daemon', default=True, help="Run or don't run in daemon mode")
@click.option('--upgrade', is_flag=True, default=False, help="Upgrade to the most recent version")
@click.option('--edge', is_flag=True, default=False, help="Run the bleading edge", hidden=True)
@display_error
def local(ctx, port, env, daemon, upgrade, edge):
    api = InternalApi()
    if not find_executable('docker'):
        raise ClickException(
            "Docker not installed, install it from https://docker.com")
    if wandb.docker.image_id("wandb/local") != wandb.docker.image_id_from_registry("wandb/local"):
        if upgrade:
            subprocess.call(["docker", "pull", "wandb/local"])
        else:
            wandb.termlog("A new version of W&B local is available, upgrade by calling `wandb local --upgrade`")
    running = subprocess.check_output(["docker", "ps", "--filter", "name=wandb-local", "--format", "{{.ID}}"])
    if running != b"":
        if upgrade:
            subprocess.call(["docker", "stop", "wandb-local"])
        else:
            wandb.termerror("A container named wandb-local is already running, run `docker stop wandb-local` if you want to start a new instance")
            exit(1)
    image = "docker.pkg.github.com/wandb/core/local" if edge else "wandb/local"
    username = getpass.getuser()
    env_vars = ['-e', 'LOCAL_USERNAME=%s' % username]
    for e in env:
        env_vars.append('-e')
        env_vars.append(e)
    command = ['docker', 'run', '--rm', '-v', 'wandb:/vol', '-p', port + ':8080', '--name', 'wandb-local'] + env_vars
    host = "http://localhost:%s" % port
    api.set_setting("base_url", host, globally=True, persist=True)
    if daemon:
        command += ["-d"]
    command += [image]

    # DEVNULL is only in py3
    try:
        from subprocess import DEVNULL
    except ImportError:
        DEVNULL = open(os.devnull, 'wb')  # noqa: N806
    code = subprocess.call(command, stdout=DEVNULL)
    if daemon:
        if code != 0:
            wandb.termerror("Failed to launch the W&B local container, see the above error.")
            exit(1)
        else:
            wandb.termlog("W&B local started at http://localhost:%s \U0001F680" % port)
            wandb.termlog("You can stop the server by running `docker stop wandb-local`")
            if not api.api_key:
                # Let the server start before potentially launching a browser
                time.sleep(2)
                ctx.invoke(login, host=host)


@cli.group(help="Commands for interacting with artifacts")
def artifact():
    pass


@artifact.command(context_settings=CONTEXT, help="Upload an artifact to wandb")
@click.argument("path")
@click.option("--name", "-n", help="The name of the artifact to push: project/artifact_name")
@click.option("--description", "-d", help="A description of this artifact")
@click.option("--type", "-t", default="dataset", help="The type of the artifact")
@click.option("--alias", "-a", default=["latest"], multiple=True, help="An alias to apply to this artifact")
@display_error
def put(path, name, description, type, alias):
    if name is None:
        name = os.path.basename(path)
    public_api = PublicApi()
    entity, project, artifact_name = public_api._parse_artifact_path(name)
    if project is None:
        project = click.prompt("Enter the name of the project you want to use")
    # TODO: settings nightmare...
    api = InternalApi()
    api.set_setting("entity", entity)
    api.set_setting("project", project)
    artifact = wandb.Artifact(name=artifact_name, type=type, description=description)
    artifact_path = "{entity}/{project}/{name}:{alias}".format(entity=entity,
                                                               project=project, name=artifact_name, alias=alias[0])
    if os.path.isdir(path):
        wandb.termlog("Uploading directory {path} to: \"{artifact_path}\" ({type})".format(
            path=path, type=type, artifact_path=artifact_path))
        artifact.add_dir(path)
    elif os.path.isfile(path):
        wandb.termlog("Uploading file {path} to: \"{artifact_path}\" ({type})".format(
            path=path, type=type, artifact_path=artifact_path))
        artifact.add_file(path)
    elif "://" in path:
        wandb.termlog("Logging reference artifact from {path} to: \"{artifact_path}\" ({type})".format(
            path=path, type=type, artifact_path=artifact_path))
        artifact.add_reference(path)
    else:
        raise ClickException("Path argument must be a file or directory")

    run = wandb.init(entity=entity, project=project, config={"path": path}, job_type="cli_put")
    # We create the artifact manually to get the current version
    res = api.create_artifact(type, artifact_name, artifact.digest,
                              entity_name=entity, project_name=project, run_name=run.id, description=description,
                              aliases=[{"artifactCollectionName": artifact_name, "alias": a} for a in alias])
    artifact_path = artifact_path.split(":")[0] + ":" + res.get("version", "latest")
    # Re-create the artifact and actually upload any files needed
    run.log_artifact(artifact, aliases=alias)
    wandb.termlog("Artifact uploaded, use this artifact in a run by adding:\n", prefix=False)

    wandb.termlog("    artifact = run.use_artifact(\"{path}\")\n".format(
        path=artifact_path,
    ), prefix=False)


@artifact.command(context_settings=CONTEXT, help="Download an artifact from wandb")
@click.argument("path")
@click.option("--root", help="The directory you want to download the artifact to")
@click.option("--type", help="The type of artifact you are downloading")
@display_error
def get(path, root, type):
    public_api = PublicApi()
    entity, project, artifact_name = public_api._parse_artifact_path(path)
    if project is None:
        project = click.prompt("Enter the name of the project you want to use")

    try:
        artifact_parts = artifact_name.split(":")
        if len(artifact_parts) > 1:
            version = artifact_parts[1]
            artifact_name = artifact_parts[0]
        else:
            version = "latest"
        full_path = "{entity}/{project}/{artifact}:{version}".format(
            entity=entity, project=project,
            artifact=artifact_name, version=version)
        wandb.termlog("Downloading {type} artifact {full_path}".format(
            type=type or "dataset", full_path=full_path))
        artifact = public_api.artifact(full_path, type=type)
        path = artifact.download(root=root)
        wandb.termlog("Artifact downloaded to %s" % path)
    except ValueError:
        raise ClickException("Unable to download artifact")


@artifact.command(context_settings=CONTEXT, help="List all artifacts in a wandb project")
@click.argument("path")
@click.option("--type", "-t", help="The type of artifacts to list")
@display_error
def ls(path, type):
    public_api = PublicApi()
    if type is not None:
        types = [public_api.artifact_type(type, path)]
    else:
        types = public_api.artifact_types(path)

    def human_size(bytes, units=None):
        units = units or ['', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB']
        return str(bytes) + units[0] if bytes < 1024 else human_size(bytes >> 10, units[1:])

    for kind in types:
        for collection in kind.collections():
            versions = public_api.artifact_versions(kind.type, "/".join([kind.entity, kind.project, collection.name]),
                                                    per_page=1)
            latest = next(versions)
            print("{:<15s}{:<15s}{:>15s} {:<20s}".format(kind.type, latest.updated_at, human_size(latest.size),
                                                         latest.name))


@cli.command(context_settings=CONTEXT, help="Pull files from Weights & Biases")
@click.argument("run", envvar=env.RUN_ID)
@click.option("--project", "-p", envvar=env.PROJECT, help="The project you want to download.")
@click.option("--entity", "-e", default="models", envvar=env.ENTITY, help="The entity to scope the listing to.")
@display_error
def pull(run, project, entity):
    api = InternalApi()
    project, run = api.parse_slug(run, project=project)
    urls = api.download_urls(project, run=run, entity=entity)
    if len(urls) == 0:
        raise ClickException("Run has no files")
    click.echo("Downloading: {project}/{run}".format(
        project=click.style(project, bold=True), run=run
    ))

    for name in urls:
        if api.file_current(name, urls[name]['md5']):
            click.echo("File %s is up to date" % name)
        else:
            length, response = api.download_file(urls[name]['url'])
            # TODO: I had to add this because some versions in CI broke click.progressbar
            sys.stdout.write("File %s\r" % name)
            dirname = os.path.dirname(name)
            if dirname != '':
                wandb.util.mkdir_exists_ok(dirname)
            with click.progressbar(length=length, label='File %s' % name,
                                   fill_char=click.style('&', fg='green')) as bar:
                with open(name, "wb") as f:
                    for data in response.iter_content(chunk_size=4096):
                        f.write(data)
                        bar.update(len(data))
