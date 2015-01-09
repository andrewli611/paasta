#!/usr/bin/env python
"""Contains methods used by the paasta client to upload a docker
image to a registry.
"""

import shlex
import subprocess
import sys

from paasta_tools.paasta_cli.utils import validate_service_name


def add_subparser(subparsers):
    list_parser = subparsers.add_parser(
        'push-to-registry',
        description='Uploads a docker image to a registry',
        help='Uploads a docker image to a registry')

    list_parser.add_argument('-s', '--service',
                             help='Name of service for which you wish to upload a docker image. Leading "services-", as included in a Jenkins job name, will be stripped.',
                             required=True,
                             )
    list_parser.add_argument('-c', '--commit',
                             help='Git sha after which to name the remote image',
                             required=True,
                             )

    list_parser.set_defaults(command=paasta_push_to_registry)


def build_command(upstream_job_name, upstream_git_commit):
    cmd = 'docker push docker-paasta.yelpcorp.com:443/%s:paasta-%s' % (
        upstream_job_name,
        upstream_git_commit,
    )
    return shlex.split(cmd)


def paasta_push_to_registry(args):
    """Upload a docker image to a registry"""
    service_name = args.service
    if service_name and service_name.startswith('services-'):
        service_name = service_name.split('services-', 1)[1]
    validate_service_name(service_name)

    cmd = build_command(service_name, args.commit)
    print 'INFO: Executing command "%s"' % cmd
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    except subprocess.CalledProcessError as exc:
        print 'ERROR: Failed to promote image. Output:\n%sReturn code was: %d' % (exc.output, exc.returncode)
        sys.exit(exc.returncode)
