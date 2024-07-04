#!/usr/bin/env python
# Copyright 2015-2017 Yelp Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
paasta_oom_logger is supposed to be used as a syslog-ng destination.
It looks for OOM events in the log, adds PaaSTA service and instance names
and send JSON-encoded messages the stream 'tmp_paasta_oom_events'.

syslog-ng.conf:

destination paasta_oom_logger {
  program("exec /usr/bin/paasta_oom_logger" template("${UNIXTIME} ${HOST} ${MESSAGE}\n") );
};

filter f_cgroup_oom {
  match(" killed as a result of limit of ") or match(" invoked oom-killer: ");
};

log {
  source(s_all);
  filter(f_cgroup_oom);
  destination(paasta_oom_logger);
};
"""
import argparse
import json
import re
import sys
from collections import namedtuple
from typing import Dict

import grpc
from containerd.services.containers.v1 import containers_pb2
from containerd.services.containers.v1 import containers_pb2_grpc
from docker.errors import APIError

from paasta_tools.cli.utils import get_instance_config
from paasta_tools.utils import _log
from paasta_tools.utils import DEFAULT_LOGLEVEL
from paasta_tools.utils import get_docker_client
from paasta_tools.utils import load_system_paasta_config


# Sorry to any non-yelpers but this won't
# do much as our metrics and logging libs
# are not open source
try:
    import yelp_meteorite
except ImportError:
    yelp_meteorite = None

try:
    import clog
except ImportError:
    clog = None


LogLine = namedtuple(
    "LogLine",
    [
        "timestamp",
        "hostname",
        "container_id",
        "cluster",
        "service",
        "instance",
        "process_name",
        "mesos_container_id",
        "mem_limit",
    ],
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="paasta_oom_logger")
    parser.add_argument(
        "--containerd",
        action="store_true",
        help="Use containerd to inspect containers, otherwise use docker",
    )
    return parser.parse_args()


def capture_oom_events_from_stdin():
    process_name_regex = re.compile(
        r"^\d+\s[a-zA-Z0-9\-]+\s.*\]\s(.+)\sinvoked\soom-killer:"
    )
    oom_regex_docker = re.compile(
        r"^(\d+)\s([a-zA-Z0-9\-]+)\s.*Task in /docker/(\w{12})\w+ killed as a"
    )
    oom_regex_kubernetes = re.compile(
        r"""
        ^(\d+)\s # timestamp
        ([a-zA-Z0-9\-]+) # hostname
        \s.*Task\sin\s/kubepods/(?:[a-zA-Z]+/)? # start of message; non capturing, optional group for the qos cgroup
        pod[-\w]+/(\w{12}(?:\w{52})?)\w*\s # Match 'pod' followed by alphanumeric and hyphen characters, then capture 12 characters, optionally followed by 52 characters for container id (12 char for docker and 64 char if we're using containerd), and then zero or more word characters
        killed\sas\sa*  # eom
        """,
        re.VERBOSE,
    )
    oom_regex_kubernetes_containerd_systemd_cgroup = re.compile(
        r"""
        ^(\d+)\s # timestamp
        ([a-zA-Z0-9\-]+) # hostname
        \s.*oom-kill:.*task_memcg=/system\.slice/.*nerdctl-(\w{64})\w*\.scope,.*$ # loosely match systemd slice and containerid
        """,
        re.VERBOSE,
    )
    oom_regex_kubernetes_structured = re.compile(
        r"""
        ^(\d+)\s # timestamp
        ([a-zA-Z0-9\-]+) # hostname
        \s.*oom-kill:.*task_memcg=/kubepods/(?:[a-zA-Z]+/)? # start of message; non-capturing, optional group for the qos cgroup
        pod[-\w]+/(\w{12}(?:\w{52})?)\w*,.*$ # containerid
        """,
        re.VERBOSE,
    )
    oom_regex_kubernetes_systemd_cgroup = re.compile(
        r"""
        ^(\d+)\s # timestamp
        ([a-zA-Z0-9\-]+) # hostname
        \s.*oom-kill:.*task_memcg=/kubepods\.slice/[^,]+docker-(\w{12})\w+\.scope,.*$ # loosely match systemd slice and containerid
        """,
        re.VERBOSE,
    )
    event_detail_regexes = [
        oom_regex_docker,
        oom_regex_kubernetes,
        oom_regex_kubernetes_structured,
        oom_regex_kubernetes_systemd_cgroup,
        oom_regex_kubernetes_containerd_systemd_cgroup,
    ]

    process_name = ""
    while True:
        try:
            syslog = sys.stdin.readline()
        except StopIteration:
            break
        if not syslog:
            break
        r = process_name_regex.search(syslog)
        if r:
            process_name = r.group(1)
        for expression in event_detail_regexes:
            r = expression.search(syslog)
            if r:
                yield (int(r.group(1)), r.group(2), r.group(3), process_name)
                process_name = ""
                break


def get_container_env_as_dict(
    is_cri_containerd: bool, container_inspect: dict
) -> Dict[str, str]:
    env_vars = {}
    if is_cri_containerd:
        config = container_inspect.get("process")
        env = config.get("env", [])
    else:
        config = container_inspect.get("Config")
        env = config.get("Env", [])
    if config is not None:
        for i in env:
            name, _, value = i.partition("=")
            env_vars[name] = value
    return env_vars


def log_to_clog(log_line):
    """Send the event to 'tmp_paasta_oom_events'."""
    line = (
        '{"timestamp": %d, "hostname": "%s", "container_id": "%s", "cluster": "%s", '
        '"service": "%s", "instance": "%s", "process_name": "%s", '
        '"mesos_container_id": "%s", "mem_limit": "%s"}'
        % (
            log_line.timestamp,
            log_line.hostname,
            log_line.container_id,
            log_line.cluster,
            log_line.service,
            log_line.instance,
            log_line.process_name,
            log_line.mesos_container_id,
            log_line.mem_limit,
        )
    )
    clog.log_line("tmp_paasta_oom_events", line)


def log_to_paasta(log_line):
    """Add the event to the standard PaaSTA logging backend."""
    line = "oom-killer killed {} on {} (container_id: {}).".format(
        "a %s process" % log_line.process_name
        if log_line.process_name
        else "a process",
        log_line.hostname,
        log_line.container_id,
    )
    _log(
        service=log_line.service,
        instance=log_line.instance,
        component="oom",
        cluster=log_line.cluster,
        level=DEFAULT_LOGLEVEL,
        line=line,
    )


def send_sfx_event(service, instance, cluster):
    if yelp_meteorite:
        service_instance_config = get_instance_config(
            service=service, instance=instance, cluster=cluster
        )
        dimensions = {
            "paasta_cluster": cluster,
            "paasta_instance": instance,
            "paasta_service": service,
            "paasta_pool": service_instance_config.get_pool(),
        }
        yelp_meteorite.events.emit_event(
            "paasta.service.oom_events",
            dimensions=dimensions,
        )
        counter = yelp_meteorite.create_counter(
            "paasta.service.oom_count",
            default_dimensions=dimensions,
        )
        counter.count()


def get_containerd_container(container_id: str) -> containers_pb2.Container:
    with grpc.insecure_channel("unix:///run/containerd/containerd.sock") as channel:
        containersv1 = containers_pb2_grpc.ContainersStub(channel)
        return containersv1.Get(
            containers_pb2.GetContainerRequest(id=container_id),
            metadata=(("containerd-namespace", "k8s.io"),),
        ).container


def main():
    if clog is None:
        print("CLog logger unavailable, exiting.", file=sys.stderr)
        sys.exit(1)
    args = parse_args()
    clog.config.configure(
        scribe_host="169.254.255.254",
        scribe_port=1463,
        monk_disable=False,
        scribe_disable=False,
    )
    cluster = load_system_paasta_config().get_cluster()
    client = get_docker_client()
    for (
        timestamp,
        hostname,
        container_id,
        process_name,
    ) in capture_oom_events_from_stdin():
        if args.containerd:
            # then we're using containerd to inspect containers
            try:
                container_info = get_containerd_container(container_id)
            except grpc.RpcError as e:
                print("An error occurred while getting the container:", e)
                continue
            container_spec_raw = container_info.spec.value.decode("utf-8")
            container_inspect = json.loads(container_spec_raw)
        else:
            # we're using docker to inspect containers
            try:
                container_inspect = client.inspect_container(resource_id=container_id)
            except (APIError):
                continue
        env_vars = get_container_env_as_dict(args.containerd, container_inspect)
        service = env_vars.get("PAASTA_SERVICE", "unknown")
        instance = env_vars.get("PAASTA_INSTANCE", "unknown")
        mesos_container_id = env_vars.get("MESOS_CONTAINER_NAME", "mesos-null")
        mem_limit = env_vars.get("PAASTA_RESOURCE_MEM", "unknown")
        log_line = LogLine(
            timestamp=timestamp,
            hostname=hostname,
            container_id=container_id,
            cluster=cluster,
            service=service,
            instance=instance,
            process_name=process_name,
            mesos_container_id=mesos_container_id,
            mem_limit=mem_limit,
        )
        log_to_clog(log_line)
        log_to_paasta(log_line)
        send_sfx_event(service, instance, cluster)


if __name__ == "__main__":
    main()
