# ** Client - Next Generation**
<div align="center">
  <img src="https://i.imgur.com/dKqSdPi.png" width="600" /><br><br>
</div>

Try the latest version of W&B for machine learning experiment tracking. Install the new library with:

```
pip install wandb --upgrade --pre
```

Why should I switch to the new CLI?
- Better support for Windows and Jupyter notebooks
- Preview the upcoming release and share feedback

## Support

This package is considered experimental but it is *Actively Supported*.  Issues will be responded to within 1
working day.

File issues [in this repository](https://github.com/wandb/client-ng/issues). Please specify the version when reporting issues: `wandb --version`

Alternatively you can discuss problems in the [Public Slack Forum](http://bit.ly/wandb-forum) in channel `#wandb-cling`.

## Compatibility status

- Only online mode is supported.  Only use this on a machine with a stable internet connection.
- No `tensorboard_sync` option, no `from wandb import magic`
- No wandb.init() context manager
- Only supported commandline options: login, sweep, agent, controller
- No code saving support for jupyter
- No resume support

All these compatibility issues will be addressed in upcoming releases.

## Known issues

| Problem | Bug | Estimated fix date |
| - | - | - |
| Console logs have extra linefeed | | |
| python2 occasionally throws spurious exception at end of script execution | | |

# Weights and Biases [![ci](https://circleci.com/gh/wandb/client-ng.svg?style=svg)](https://circleci.com/gh/wandb/client-ng) [![pypi](https://img.shields.io/pypi/v/wandb.svg)](https://pypi.python.org/pypi/wandb) [![Coverage Status](https://coveralls.io/repos/github/wandb/client-ng/badge.svg)](https://coveralls.io/github/wandb/client-ng)

Use W&B to organize and analyze machine learning experiments. It's framework-agnostic and lighter than TensorBoard. Each time you run a script instrumented with `wandb`, we save your hyperparameters and output metrics. Visualize models over the course of training, and compare versions of your models easily. We also automatically track the state of your code, system metrics, and configuration parameters.

[Sign up for a free account →](https://wandb.com)

## Features

-   Store hyper-parameters used in a training run
-   Search, compare, and visualize training runs
-   Analyze system usage metrics alongside runs
-   Collaborate with team members
-   Replicate historic results
-   Run parameter sweeps
-   Keep records of experiments available forever

[Documentation →](https://docs.wandb.com)

## Quickstart

```shell
pip install --pre wandb
```

In your training script:

```python
import wandb
# Your custom arguments defined here
args = ...

wandb.init(config=args, project="my-project")
wandb.config["more"] = "custom"

def training_loop():
    while True:
        # Do some machine learning
        epoch, loss, val_loss = ...
        # Framework agnostic / custom metrics
        wandb.log({"epoch": epoch, "loss": loss, "val_loss": val_loss})
```

If you're already using Tensorboard or [TensorboardX](https://github.com/lanpa/tensorboardX), you can integrate with one line:

```python
wandb.init(sync_tensorboard=True)
```

## Running your script

Run `wandb login` from your terminal to signup or authenticate your machine (we store your api key in ~/.netrc). You can also set the `WANDB_API_KEY` environment variable with a key from your [settings](https://app.wandb.ai/settings).

Run your script with `python my_script.py` and all metadata will be synced to the cloud. You will see a url in your terminal logs when your script starts and finishes. Data is staged locally in a directory named _wandb_ relative to your script. If you want to test your script without syncing to the cloud you can set the environment variable `WANDB_MODE=dryrun`.

If you are using [docker](https://docker.com) to run your code, we provide a wrapper command `wandb docker` that mounts your current directory, sets environment variables, and ensures the wandb library is installed. Training your models in docker gives you the ability to restore the exact code and environment with the `wandb restore` command.

## Web Interface

[Sign up for a free account →](https://wandb.com)
[![Watch the video](https://i.imgur.com/PW0Ejlc.png)](https://youtu.be/EeqhOSvNX-A)
[Introduction video →](https://youtu.be/EeqhOSvNX-A)

## Detailed Usage

Framework specific and detailed usage can be found in our [documentation](http://docs.wandb.com/).

## Testing

To run the tests we use `pytest tests`. If you want a simple mock of the wandb backend and cloud storage you can use the mock_server fixture, see tests/test_cli.py for examples.

We use [circleci](https://circleci.com) and [appveyor](https://appveyor.com) for CI.

# Academic Researchers
If you'd like a free academic account for your research group, [reach out to us →](https://www.wandb.com/academic)

We make it easy to cite W&B in your published paper. [Learn more →](https://www.wandb.com/academic)
[![](https://i.imgur.com/loKLiez.png)](https://www.wandb.com/academic)
