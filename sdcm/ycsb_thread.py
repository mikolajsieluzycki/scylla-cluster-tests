# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright (c) 2020 ScyllaDB

import os
import re
import time
import uuid
import tempfile
import logging
from textwrap import dedent

from sdcm.prometheus import nemesis_metrics_obj
from sdcm.sct_events.loaders import YcsbStressEvent
from sdcm.remote import FailuresWatcher
from sdcm.utils import alternator
from sdcm.utils.common import FileFollowerThread
from sdcm.utils.docker_remote import RemoteDocker
from sdcm.utils.common import generate_random_string
from sdcm.stress_thread import format_stress_cmd_error, DockerBasedStressThread

LOGGER = logging.getLogger(__name__)


class YcsbStatsPublisher(FileFollowerThread):
    METRICS = {}
    collectible_ops = ['read', 'insert', 'update', 'read-failed', 'update-failed', 'verify']

    def __init__(self, loader_node, loader_idx, ycsb_log_filename):
        super().__init__()
        self.loader_node = loader_node
        self.loader_idx = loader_idx
        self.ycsb_log_filename = ycsb_log_filename
        self.uuid = generate_random_string(10)
        for operation in self.collectible_ops:
            gauge_name = self.gauge_name(operation)
            if gauge_name not in self.METRICS:
                metrics = nemesis_metrics_obj()
                self.METRICS[gauge_name] = metrics.create_gauge(gauge_name,
                                                                'Gauge for ycsb metrics',
                                                                ['instance', 'loader_idx', 'uuid', 'type'])

    @staticmethod
    def gauge_name(operation):
        return 'collectd_ycsb_%s_gauge' % operation.replace('-', '_')

    def set_metric(self, operation, name, value):
        metric = self.METRICS[self.gauge_name(operation)]
        metric.labels(self.loader_node.ip_address, self.loader_idx, self.uuid, name).set(value)

    def handle_verify_metric(self, line):
        verify_status_regex = re.compile(r"Return\((?P<status>.*?)\)=(?P<value>\d*)")
        verify_regex = re.compile(r'\[VERIFY:(.*?)\]')
        verify_content = verify_regex.findall(line)[0]

        for status_match in verify_status_regex.finditer(verify_content):
            stat = status_match.groupdict()
            self.set_metric('verify', stat['status'], float(stat['value']))

    def run(self):
        # pylint: disable=too-many-nested-blocks

        # 729.39 current ops/sec;
        # [READ: Count=510, Max=195327, Min=2011, Avg=4598.69, 90=5743, 99=12583, 99.9=194815, 99.99=195327]
        # [CLEANUP: Count=5, Max=3, Min=0, Avg=0.6, 90=3, 99=3, 99.9=3, 99.99=3]
        # [UPDATE: Count=490, Max=190975, Min=2004, Avg=3866.96, 90=4395, 99=6755, 99.9=190975, 99.99=190975]

        regex_dict = {}
        for operation in self.collectible_ops:
            regex_dict[operation] = re.compile(
                fr'\[{operation.upper()}:\sCount=(?P<count>\d*?),'
                fr'.*?Max=(?P<max>\d*?),.*?Min=(?P<min>\d*?),'
                fr'.*?Avg=(?P<avg>.*?),.*?90=(?P<p90>\d*?),'
                fr'.*?99=(?P<p99>\d*?),.*?99.9=(?P<p999>\d*?),'
                fr'.*?99.99=(?P<p9999>\d*?)[\],\s]'
            )

        while not self.stopped():
            exists = os.path.isfile(self.ycsb_log_filename)
            if not exists:
                time.sleep(0.5)
                continue

            for _, line in enumerate(self.follow_file(self.ycsb_log_filename)):
                if self.stopped():
                    break
                try:
                    for operation, regex in regex_dict.items():
                        match = regex.search(line)
                        if match:
                            if operation == 'verify':
                                self.handle_verify_metric(line)

                            for key, value in match.groupdict().items():
                                if not key == 'count':
                                    try:
                                        value = float(value) / 1000.0
                                    except ValueError:
                                        value = float(0)
                                self.set_metric(operation, key, float(value))

                except Exception:  # pylint: disable=broad-except
                    LOGGER.exception("fail to send metric")


class YcsbStressThread(DockerBasedStressThread):  # pylint: disable=too-many-instance-attributes

    def copy_template(self, docker):
        if self.params.get('alternator_use_dns_routing'):
            target_address = 'alternator'
        else:
            if hasattr(self.node_list[0], 'parent_cluster'):
                target_address = self.node_list[0].parent_cluster.get_node().cql_ip_address
            else:
                target_address = self.node_list[0].cql_ip_address

        if 'dynamodb' in self.stress_cmd:
            dynamodb_teample = dedent('''
                measurementtype=hdrhistogram
                dynamodb.awsCredentialsFile = /tmp/aws_empty_file
                dynamodb.endpoint = http://{0}:{1}
                dynamodb.connectMax = 200
                requestdistribution = uniform
                dynamodb.consistentReads = true
            '''.format(target_address,
                       self.params.get('alternator_port')))

            dynamodb_primarykey_type = self.params.get('dynamodb_primarykey_type')
            if isinstance(dynamodb_primarykey_type, alternator.enums.YCSBSchemaTypes):
                dynamodb_primarykey_type = dynamodb_primarykey_type.value

            if dynamodb_primarykey_type == alternator.enums.YCSBSchemaTypes.HASH_AND_RANGE.value:
                dynamodb_teample += dedent(f'''
                    dynamodb.primaryKey = {alternator.consts.HASH_KEY_NAME}
                    dynamodb.hashKeyName = {alternator.consts.RANGE_KEY_NAME}
                    dynamodb.primaryKeyType = {alternator.enums.YCSBSchemaTypes.HASH_AND_RANGE.value}
                ''')
            elif dynamodb_primarykey_type == alternator.enums.YCSBSchemaTypes.HASH_SCHEMA.value:
                dynamodb_teample += dedent(f'''
                    dynamodb.primaryKey = {alternator.consts.HASH_KEY_NAME}
                    dynamodb.primaryKeyType = {alternator.enums.YCSBSchemaTypes.HASH_SCHEMA.value}
                ''')

            aws_empty_file = dedent(f""""
                accessKey = {self.params.get('alternator_access_key_id')}
                secretKey = {self.params.get('alternator_secret_access_key')}
            """)

            with tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8') as tmp_file:
                tmp_file.write(dynamodb_teample)
                tmp_file.flush()
                docker.send_files(tmp_file.name, os.path.join('/tmp', 'dynamodb.properties'))

            with tempfile.NamedTemporaryFile(mode='w+', encoding='utf-8') as tmp_file:
                tmp_file.write(aws_empty_file)
                tmp_file.flush()
                docker.send_files(tmp_file.name, os.path.join('/tmp', 'aws_empty_file'))

    def build_stress_cmd(self):
        hosts = ",".join([i.cql_ip_address for i in self.node_list])

        stress_cmd = f'{self.stress_cmd} -s '
        if 'dynamodb' in self.stress_cmd:
            stress_cmd += ' -P /tmp/dynamodb.properties'
        if 'cassandra-cql' in self.stress_cmd:

            stress_cmd += f' -p hosts={hosts} -p cassandra.readconsistencylevel=QUORUM -p cassandra.writeconsistencylevel=QUORUM'
        if "scylla" in self.stress_cmd:
            stress_cmd += f" -p scylla.hosts={hosts}"
        if 'maxexecutiontime' not in stress_cmd:
            stress_cmd += f' -p maxexecutiontime={self.timeout}'

        return stress_cmd

    @staticmethod
    def parse_final_output(result):
        """
        parse ycsb final results to match what we get out of cassandra-stress
        latencies returned in milliseconds

        :param result: output of ycsb command
        :return: dict
        """
        ops_regex = re.compile(r'\[OVERALL\],\sThroughput\(ops/sec\),\s(?P<op_rate>.*)')
        latency_99_regex = re.compile(
            r'\[(READ|INSERT|UPDATE)\],\s99thPercentileLatency\(us\),\s(?P<latency_99th_percentile>.*)')
        latency_mean_regex = re.compile(r'\[(READ|INSERT|UPDATE)\],\sAverageLatency\(us\),\s(?P<latency_mean>.*)')

        output = {'latency 99th percentile': 0,
                  'latency mean': 0,
                  'op rate': 0
                  }
        for line in result.stdout.splitlines():
            match = ops_regex.match(line)
            if match:
                output['op rate'] = match.groupdict()['op_rate']
            match = latency_99_regex.match(line)
            if match:
                output['latency 99th percentile'] += float(match.groups()[1]) / 1000.0
                output['latency 99th percentile'] /= 2
            match = latency_mean_regex.match(line)
            if match:
                output['latency mean'] += float(match.groups()[1]) / 1000.0
                output['latency mean'] /= 2

        # output back to strings
        output = {k: str(v) for k, v in output.items()}
        return output

    def _run_stress(self, loader, loader_idx, cpu_idx):
        dns_options = ""
        cpu_options = ""
        if self.params.get('alternator_use_dns_routing'):
            dns = RemoteDocker(loader, "scylladb/hydra-loaders:alternator-dns-0.2",
                               command_line=f'python3 /dns_server.py {self.db_node_to_query(loader)} '
                                            f'{self.params.get("alternator_port")}',
                               extra_docker_opts=f'--label shell_marker={self.shell_marker}')
            dns_options += f'--dns {dns.internal_ip_address} --dns-option use-vc'

        if self.stress_num > 1:
            cpu_options = f'--cpuset-cpus="{cpu_idx}"'

        docker = RemoteDocker(loader, "scylladb/hydra-loaders:ycsb-jdk8-20211104",
                              extra_docker_opts=f'{dns_options} {cpu_options} --label shell_marker={self.shell_marker}')
        self.copy_template(docker)
        stress_cmd = self.build_stress_cmd()

        if not os.path.exists(loader.logdir):
            os.makedirs(loader.logdir, exist_ok=True)
        log_file_name = os.path.join(loader.logdir, 'ycsb-l%s-c%s-%s.log' %
                                     (loader_idx, cpu_idx, uuid.uuid4()))
        LOGGER.debug('ycsb-stress local log: %s', log_file_name)

        def raise_event_callback(sentinel, line):  # pylint: disable=unused-argument
            if line:
                YcsbStressEvent.error(node=loader, stress_cmd=stress_cmd, errors=[line, ]).publish()

        LOGGER.debug("running: %s", stress_cmd)

        node_cmd = 'cd /YCSB && {}'.format(stress_cmd)

        YcsbStressEvent.start(node=loader, stress_cmd=stress_cmd).publish()

        with YcsbStatsPublisher(loader, loader_idx, ycsb_log_filename=log_file_name):
            try:
                result = docker.run(
                    cmd=node_cmd,
                    timeout=self.timeout + self.shutdown_timeout,
                    log_file=log_file_name,
                    watchers=[
                        FailuresWatcher(
                            r'\sERROR|=UNEXPECTED_STATE|=ERROR',
                            callback=raise_event_callback,
                            raise_exception=False
                        )
                    ]
                )
                return self.parse_final_output(result)

            except Exception as exc:
                errors_str = format_stress_cmd_error(exc)
                YcsbStressEvent.failure(
                    node=loader,
                    stress_cmd=self.stress_cmd,
                    log_file_name=log_file_name,
                    errors=[errors_str, ],
                ).publish()
                raise
            finally:
                YcsbStressEvent.finish(node=loader, stress_cmd=stress_cmd, log_file_name=log_file_name).publish()
