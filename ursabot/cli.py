# Copyright 2019 RStudio, Inc.
# All rights reserved.
#
# Use of this source code is governed by a BSD 2-Clause
# license that can be found in the LICENSE_BSD file.

import io
import logging
import warnings
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

import click
from buildbot.config import ConfigErrors
from buildbot.plugins import util
from buildbot.process.results import Results
from buildbot.process.results import SUCCESS, WARNINGS, FAILURE, EXCEPTION
from buildbot.util.logger import Logger
from dockermap.api import DockerClientWrapper
from twisted.internet import reactor
from twisted.python.log import PythonLoggingObserver

from .builders import DockerBuilder
from .configs import Config, MasterConfig
from .utils import Matching, Filter, ensure_deferred
from .docker import ImageCollection
from .master import TestMaster


logging.basicConfig()
logger = logging.getLogger(__name__)
logger_ = Logger()  # twisted's logger


# TODO(kszucs): try to use asyncio reactor with uvloop instead of the default
#               twisted one


class UrsabotConfigErrors(click.ClickException):

    def __init__(self, wrapped):
        assert isinstance(wrapped, ConfigErrors)
        self.wrapped = wrapped

    def show(self):
        click.echo(click.style('Configuration Errors:', fg='red'), err=True)
        for e in self.wrapped.errors:
            click.echo(click.style(f' - {e}'), err=True)


@click.group()
@click.option('--verbose/--quiet', '-v/-q', default=False, is_flag=True)
@click.option('--config-path', '-c', default='master.cfg',
              help='Configuration file path')
@click.option('--config-variable', '-cv', default='master',
              help='Variable name in the configuration which must be an '
                   'instance of MasterConfig')
@click.pass_context
def ursabot(ctx, verbose, config_path, config_variable):
    """CLI for Ursabot continous integration framework based on Buildbot

    `ursabot` command tries to locate the master.cfg file and it looks for a
    MasterConfig instance in a variable called `master` by default. This
    configuration affects the rest of the CLI commands.
    """

    if verbose:
        logging.getLogger('ursabot').setLevel(logging.INFO)

    stderr, stdout = io.StringIO(), io.StringIO()
    try:
        with redirect_stderr(stderr), redirect_stdout(stdout):
            with warnings.catch_warnings(record=True) as catched_warnings:
                config = Config.load_from(config_path,
                                          variable=config_variable)
    except ConfigErrors as e:
        raise UrsabotConfigErrors(e)
    finally:
        for warning in catched_warnings:
            click.echo(click.style(str(warning.message), fg='red'), err=True)
        if verbose:
            stderr, stdout = stderr.getvalue(), stdout.getvalue()
            if stderr:
                click.echo(click.style(stderr, fg='red'), err=True)
            if stdout:
                click.echo(stdout)

    if not isinstance(config, MasterConfig):
        raise click.UsageError(
            f'The loaded variable `{config_variable}` from `{config_path}` '
            f'has type `{type(config)}` whereas it needs to be an instance of '
            f'MasterConfig'
        )

    ctx.ensure_object(dict)
    ctx.obj['verbose'] = verbose
    ctx.obj['config'] = config
    ctx.obj['config_path'] = Path(config_path)


@ursabot.group()
@click.option('--project', '-p', default=None,
              help='If the master has multiple projects configured, one must '
                   'be selected.')
@click.pass_obj
def project(obj, project):
    """Ursabot's project specific commands

    Retrieves the selected project's configurations, the project's name must be
    explicitly passed if the master is configured with multiple projects.
    """
    config = obj['config']
    project_names = ', '.join(p.name for p in config.projects)

    if project is None:
        if len(config.projects) == 1:
            project = config.projects[0]
        else:
            raise click.UsageError(f'Master config has multiple projects, one '
                                   f'must be selected: {project_names}')
    else:
        try:
            project = config.project(name=project)
        except KeyError:
            raise click.UsageError(f'Invalid project name {project}, possible '
                                   f'values are: {project_names}')

    obj['project'] = project


@ursabot.command('desc')
@click.pass_obj
def master_desc(obj):
    """Describe the master configuration"""

    def ul(values):
        return '\n'.join(f' - {v}' for v in values)

    config = obj['config']

    click.echo('Docker images:')
    click.echo(ul(i.fqn for i in config.images))
    click.echo()
    click.echo('Workers:')
    click.echo(ul(config.workers))
    click.echo()
    click.echo('Builders:')
    click.echo(ul(b.name for b in config.builders))
    click.echo()


@project.command('desc')
@click.pass_obj
def project_desc(obj):
    """Describe the project configuration"""
    def ul(values):
        return '\n'.join(f' - {v}' for v in values)

    project = obj['project']
    click.echo(f'Name: {project.name}')
    click.echo(f'Repo: {project.repo}')
    click.echo()
    click.echo('Docker images:')
    click.echo(ul(i.fqn for i in project.images))
    click.echo()
    click.echo('Workers:')
    click.echo(ul(project.workers))
    click.echo()
    click.echo('Builders:')
    click.echo(ul(b.name for b in project.builders))
    click.echo()


@ursabot.command()
@click.pass_obj
def checkconfig(obj):
    """Run sanity checks on the master configuration

    It is a wrapper around `buildbot checkconfig`.
    """
    config = obj['config']
    config_path = obj['config_path']

    try:
        config.as_buildbot(source=config_path.name)
    except ConfigErrors as e:
        raise UrsabotConfigErrors(e)

    click.echo(click.style('Config file is good!', fg='green'))


@ursabot.command()
@click.pass_obj
def upgrade_master(obj):
    """Initialize/upgrade the buildmaster's database

    It is a wrapper around `buildbot upgrade-master`.
    """
    from buildbot.util import in_reactor
    from buildbot.scripts.upgrade_master import upgradeDatabase

    @in_reactor
    @ensure_deferred
    async def run(command_cfg, master_cfg):
        try:
            await upgradeDatabase(command_cfg, master_cfg)
        except Exception as e:
            click.error(e)

    config = obj['config']
    config_path = obj['config_path']
    basedir = config_path.parent.absolute()

    command_cfg = {'basedir': basedir, 'quiet': False}
    master_cfg = config.as_buildbot(config_path.name)

    run(command_cfg, master_cfg)
    click.echo(click.style('Upgrade complete!', fg='green'))


@ursabot.command('start')
@click.option('--no-daemon', '-nd', is_flag=True, default=False,
              help="Don't daemonize (stay in foreground)")
@click.option('--start-timeout', is_flag=True, default=None,
              help='The amount of time the script waits for the master to '
                   'start until it declares the operation as failure')
@click.pass_obj
def start_master(obj, no_daemon, start_timeout):
    """Start the buildmaster

    It is a wrapper around `buildbot start`.
    """
    from buildbot.scripts.start import start

    command_cfg = {
        'basedir': obj['config_path'].parent.absolute(),
        'quiet': False,
        'nodaemon': no_daemon,
        'start_timeout': start_timeout
    }
    result = start(command_cfg)  # loads the config through the buildbot.tac
    if result > 0:
        raise click.Abort('Failed to start the Buildbot master!')

    url = obj['config'].url
    click.echo('Buildbot UI is available at: ' + click.style(url, fg='green'))


@ursabot.command('stop')
@click.option('--clean', '-c', is_flag=True, default=False,
              help='Clean shutdown master')
@click.option('--no-wait', is_flag=True, default=False,
              help="Don't wait for complete master shutdown")
@click.pass_obj
def stop_master(obj, clean, no_wait):
    """Stop the buildmaster

    It is a wrapper around `buildbot stop`.
    """
    from buildbot.scripts.stop import stop
    command_cfg = {
        'basedir': obj['config_path'].parent.absolute(),
        'quiet': False,
        'clean': clean,
        'no-wait': no_wait
    }
    result = stop(command_cfg)
    if result > 0:
        raise click.Abort('Failed to stop the Buildbot master!')

    click.echo('Buildbot has been successfully stopped!')


@ursabot.command('restart')
@click.option('--no-daemon', '-nd', is_flag=True, default=False,
              help="Don't daemonize (stay in foreground)")
@click.option('--start-timeout', is_flag=True, default=None,
              help='The amount of time the script waits for the master to '
                   'start until it declares the operation as failure')
@click.option('--clean', '-c', is_flag=True, default=False,
              help='Clean shutdown master')
@click.option('--no-wait', is_flag=True, default=False,
              help="Don't wait for complete master shutdown")
@click.pass_obj
def restart_master(obj, no_daemon, start_timeout, clean, no_wait):
    """Restart the buildmaster

    It is a wrapper around `buildbot restart`.
    """
    from buildbot.scripts.restart import restart
    command_cfg = {
        'basedir': obj['config_path'].parent.absolute(),
        'quiet': False,
        'nodaemon': no_daemon,
        'start_timeout': start_timeout,
        'clean': clean,
        'no-wait': no_wait
    }
    result = restart(command_cfg)
    if result > 0:
        raise click.Abort('Failed to restart the Buildbot master!')

    url = obj['config'].url
    click.echo('Buildbot UI is available at: ' + click.style(url, fg='green'))


@ursabot.group()
@click.option('--docker-host', '-dh', default=None,
              help='Docker host url in form: tcp://127.0.0.1:2375')
@click.option('--docker-username', '-du', default=None,
              help='Username to authenticate dockerhub with')
@click.option('--docker-password', '-dp', default=None,
              help='Password to authenticate dockerhub with')
@click.option('--arch', '-a', default='*',
              help='Filter images by architecture')
@click.option('--system', '-s', default='*',
              help='Filter images by operating system')
@click.option('--distro', '-d', default='*',
              help='Filter images by the distribution of the operating system')
@click.option('--tag', '-t', default='*',
              help='Filter images by operating system')
@click.option('--variant', '-v', default='*',
              help='Filter images by variant')
@click.option('--no-variant', default=False, is_flag=True,
              help='Filter images by having empty/no variant set')
@click.option('--name', '-n', default='*', help='Filter images by name')
@click.pass_obj
def docker(obj, docker_host, docker_username, docker_password, name, tag,
           variant, no_variant, arch, system, distro):
    """Subcommand to build docker images for the docker builders

    It loads the docker images defined in the master's configuration.
    MasterConfig aggregates the available docker images from the passed
    projects.
    """
    config = obj['config']
    if obj['verbose']:
        logging.getLogger('dockermap').setLevel(logging.INFO)

    client = DockerClientWrapper(docker_host)
    if docker_username is not None:
        client.login(username=docker_username, password=docker_password)

    if no_variant:
        variant = None

    image_filter = Filter(
        name=Matching(name),
        tag=Matching(tag),
        variant=Matching(variant),
        platform=Filter(
            arch=Matching(arch),
            system=Matching(system),
            distro=Matching(distro)
        )
    )
    filtered = ImageCollection(i for i in config.images if image_filter(i))

    obj['client'] = client
    obj['images'] = filtered


@docker.command('list')
@click.pass_obj
def docker_list_images(obj):
    """List the defined docker images"""
    images = obj['images']
    for image in images:
        click.echo(image)


# TODO(kszucs): option to push to another organization
@docker.command('build')
@click.option('--push/--no-push', '-p', default=False,
              help='Push the built images')
@click.option('--no-cache/--cache', default=False,
              help='Do not use cache when building the images')
@click.pass_obj
def docker_image_build(obj, push, no_cache):
    """Build and optionally push docker images"""
    client = obj['client']
    images = obj['images']

    images.build(client=client, nocache=no_cache)
    if push:
        images.push(client=client)


@docker.command('write-dockerfiles')
@click.option('--directory', '-d', default='images',
              help='Path to the directory where the images should be written')
@click.pass_obj
def docker_write_dockerfiles(obj, directory):
    """Write the corresponding Dockerfile for the images"""
    images = obj['images']
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    for image in images:
        image.save_dockerfile(directory)


def _handle_stdio_log(newlines):
    # 'o': 'stdout',
    # 'e': 'stderr',
    # 'h': 'header'
    for l in newlines:
        if l.startswith('h'):
            click.echo(click.style(l[1:], fg='blue'))
        elif l.startswith('e'):
            click.echo(click.style(l[1:], fg='red'))
        elif l.startswith('o'):
            click.echo(l[1:])
        else:
            click.echo(l)


def _use_local_sources(builder, sources):
    """Small utility function to inject source volumes"""
    volumes = []
    for src, dst in sources.items():
        src = Path(src).expanduser()
        volumes.append(
            util.Interpolate(
                f'{src}:%(prop:docker_workdir)s/%(prop:builddir)s/{dst}:rw'
            )
        )

    # add the volumes to the builder
    builder.volumes.extend(volumes)

    # remove the source steps from the build factory, setting notReally make
    # the source steps to fake the checkouts
    from buildbot.steps.source import Source
    assert hasattr(Source, 'notReally')
    Source.notReally = True


@project.command('build')
@click.argument('builder_name', nargs=1)
@click.option('--repo', '-r', default=None,
              help="Repository to clone, defaults to the Project's repo.")
@click.option('--branch', '-b', default='master', help='Branch to clone')
@click.option('--commit', '-c', default=None, help='Commit to clone')
@click.option('--pull-request', '-pr', type=int, default=None,
              help='Github pull request to clone, owerwrites the branch '
                   'option')
@click.option('--property', '-p', 'properties', multiple=True,
              help='Arbitrary properties passed to the builds. It must be '
                   'passed in `name=value` form.')
@click.option('--mount-source', '-s', 'sources', multiple=True,
              help='Mount local source directory into the docker worker. '
                   'It must be passed in `source:destination` form.'
                   'Useful for running builders on local repositories. '
                   'If any source mount is defined, then all of the '
                   "builder's source checkouts are faked out, so each "
                   'checkout step must be provided.')
@click.option('--attach-on-failure', '-a', is_flag=True, default=False,
              help='If a build fails and it is executed withing a '
                   'DockerLatentWorker then start an interactive shell '
                   'session in the container. Note that it blocks the event '
                   'loop until the shell is running.')
@click.pass_obj
def project_build(obj, builder_name, repo, branch, commit, pull_request,
                  properties, sources, attach_on_failure):
    """Reproduce the builds locally

    It spins up a a short living, lightweight buildmaster with an inmemory
    sqlite database and triggers the specified builder. The build step logs
    are redirected to the console.
    """
    # force twisted logger to use the cli module's python logger
    observer = PythonLoggingObserver(loggerName=logger.name)
    observer.start()

    config, project = obj['config'], obj['project']

    # check that the triggerable builder exists
    try:
        builder = project.builder(name=builder_name)
    except KeyError:
        available = '\n'.join(f' - {b.name}' for b in project.builders)
        raise click.ClickException(
            f"Project {project.name} doesn't have a builder named "
            f'`{builder_name}`.\n Select one from the following list: \n'
            f'{available}'
        )
    else:
        click.echo(f'Triggering builder: {builder}')

    # convert the sources and properties to a plain mapping
    sources = dict(p.split(':') for p in sources)
    properties = dict(p.split('=') for p in properties)

    # if local source directories are passed add them as docker volumes
    if sources:
        if not isinstance(builder, DockerBuilder):
            raise click.UsageError(
                'Mounting source directories is a feature only available for '
                'docker builders.'
            )
        _use_local_sources(builder, sources)

    # construct the sourcestamp which will trigger the builders
    if pull_request is not None:
        branch = f'refs/pull/{pull_request}/merge'
    sourcestamp = {
        'codebase': '',
        'repository': repo or project.repo,
        'branch': branch,
        'revision': commit,
        'project': project.name
    }

    attach_on = {FAILURE, EXCEPTION} if attach_on_failure else set()
    result = {'complete': False}
    try:
        # configure a lightweight master with in-memory database
        master = TestMaster(config, attach_on=attach_on,
                            log_handler=_handle_stdio_log)
    except ConfigErrors as e:
        raise UrsabotConfigErrors(e)

    @ensure_deferred
    async def run():
        """Start the master and trigger the requested builders"""
        nonlocal result
        try:
            async with master:
                result = await master.build(builder.name, sourcestamp,
                                            properties=properties)
        finally:
            reactor.stop()

    reactor.callWhenRunning(run)
    reactor.run()

    if not result['complete']:
        raise click.ClickException('Build has not completed!')

    # 'results' refers to the final state of the build
    state = result['results']
    if state in (SUCCESS, WARNINGS):
        click.echo(click.style('Build successful!', fg='green'))
    else:
        statestring = Results[state]
        raise click.ClickException(
            f'Build has failed with state {statestring}'
        )
