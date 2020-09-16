#
# -*- coding: utf-8 -*-
"""
login.
"""

from __future__ import print_function

import logging

import click
import wandb
from wandb.errors.error import UsageError
from wandb.internal.internal_api import Api
from wandb.lib import apikey

logger = logging.getLogger("wandb")

if wandb.TYPE_CHECKING:  # type: ignore
    from typing import Dict  # noqa: F401 pylint: disable=unused-import


def _validate_anonymous_setting(anon_str):
    return anon_str in ["must", "allow", "never"]


def login(anonymous=None, key=None, relogin=None, host=None, force=None):
    configured = _login(
        anonymous=anonymous, key=key, relogin=relogin, host=host, force=force
    )
    return True if configured else False


def _login(
    anonymous=None,
    key=None,
    relogin=None,
    force=None,
    host=None,
    _backend=None,
    _disable_warning=None,
    _settings=None,
):
    """Log in to W&B.

    Args:
        settings (dict, optional): Override settings.
        relogin (bool, optional): If true, will re-prompt for API key.
        host (string, optional): The host to connect to
        anonymous (string, optional): Can be "must", "allow", or "never".
            If set to "must" we'll always login anonymously, if set to
            "allow" we'll only create an anonymous user if the user
            isn't already logged in.
    Returns:
        bool: if key is configured

    Raises:
        UsageError - if api_key can not configured and no tty
    """
    if wandb.run is not None:
        if not _disable_warning:
            wandb.termwarn("Calling wandb.login() after wandb.init() is a no-op.")
        return True

    settings_dict: Dict = {}
    api = Api()

    if anonymous is not None:
        # TODO: Move this check into wandb_settings probably.
        if not _validate_anonymous_setting(anonymous):
            wandb.termwarn(
                "Invalid value passed for argument `anonymous` to "
                "wandb.login(). Can be 'must', 'allow', or 'never'."
            )
            return False
        settings_dict.update({"anonymous": anonymous})

    if host is not None:
        settings_dict.update({"base_url": host})

    if key:
        settings_dict.update({"api_key": key})

    # Note: This won't actually do anything if called from a codepath where
    # wandb.setup was previously called. If wandb.setup is called further up,
    # you must make sure the anonymous setting (and any other settings) are
    # already properly set up there.
    wl = wandb.setup(settings=wandb.Settings(**settings_dict))
    wl_settings = wl.settings()
    if _settings:
        wl_settings._apply_settings(settings=_settings)
    settings = wl_settings

    if settings._offline:
        return False

    active_entity = None
    logged_in = is_logged_in(settings=settings)
    if logged_in:
        # TODO: do we want to move all login logic to the backend?
        if _backend:
            res = _backend.interface.communicate_login(key, anonymous)
            active_entity = res.active_entity
        else:
            active_entity = wl._get_entity()
    if active_entity and not relogin:
        login_state_str = "Currently logged in as:"
        login_info_str = "(use `wandb login --relogin` to force relogin)"
        wandb.termlog(
            "{} {} {}".format(
                login_state_str, click.style(active_entity, fg="yellow"), login_info_str
            ),
            repeat=False,
        )
        return True

    jupyter = settings._jupyter or False
    if key:
        if jupyter:
            wandb.termwarn(
                (
                    "If you're specifying your api key in code, ensure this "
                    "code is not shared publically.\nConsider setting the "
                    "WANDB_API_KEY environment variable, or running "
                    "`wandb login` from the command line."
                )
            )
        apikey.write_key(settings, key)
    else:
        key = apikey.prompt_api_key(
            settings, api=api, no_offline=force, no_create=force
        )
        if key is False:
            raise UsageError("api_key not configured (no-tty).  Run wandb login")

    if _backend and not logged_in:
        # TODO: calling this twice is gross, this deserves a refactor
        # Make sure our backend picks up the new creds
        _ = _backend.interface.communicate_login(key, anonymous)
    return key or False


def is_logged_in(settings=None):
    wl = wandb.setup()
    wl_settings = wl.settings()
    wl_settings._apply_settings(settings=settings)
    return apikey.api_key(settings=wl_settings) is not None
