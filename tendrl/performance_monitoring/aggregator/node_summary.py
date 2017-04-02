import datetime
from etcd import EtcdConnectionFailed
from etcd import EtcdKeyNotFound
import gevent
import math
import multiprocessing
import re
import time

from tendrl.commons.event import Event
from tendrl.commons.message import ExceptionMessage

from tendrl.performance_monitoring.exceptions \
    import TendrlPerformanceMonitoringException
from tendrl.performance_monitoring.objects.node_summary \
    import NodeSummary


class NodeSummarise(multiprocessing.Process):
    def __init__(self):
        super(NodeSummarise, self).__init__()
        self._complete = multiprocessing.Event()

    ''' Get latest stats of resource as in param resource'''
    def get_latest_stat(self, node, resource):
        try:
            node_name = NS.central_store_thread.get_node_name_from_id(
                node
            )
            stats = NS.time_series_db_manager.get_plugin().get_metric_stats(
                node_name,
                resource,
                'latest'
            )
            if stats == "[]":
                raise TendrlPerformanceMonitoringException(
                    'Stats not yet available in time series db'
                )
            stat = re.search('Current:(.+?)Max', stats)
            if not stat:
                raise TendrlPerformanceMonitoringException(
                    'Failed to get latest stat of %s of node %s for summary'
                    'Error: Current utilization not found' % (
                        resource,
                        node
                    )
                )
            stat = re.search('Current:(.+?)Max', stats).group(1)
            if math.isnan(float(stat)):
                raise TendrlPerformanceMonitoringException(
                    'Received nan for utilization %s of %s' % (
                        resource,
                        node
                    )
                )
            return float(stat)
        except TendrlPerformanceMonitoringException as ex:
            Event(
                ExceptionMessage(
                    priority="debug",
                    publisher=NS.publisher_id,
                    payload={"message": 'Failed to get latest stat of %s of '
                                        'node %s for node summary.'
                                        % (resource, node),
                             "exception": ex
                             }
                )
            )
            raise ex

    ''' Get latest stats of resources matching wild cards in param resource'''
    def get_latest_stats(self, node, resource):
        try:
            node_name = NS.central_store_thread.get_node_name_from_id(node)
            stats = NS.time_series_db_manager.get_plugin().get_metric_stats(
                node_name,
                resource,
                'latest'
            )
            if stats == "[]":
                raise TendrlPerformanceMonitoringException(
                    'Stats not yet available in time series db'
                )
            return re.findall('Current:(.+?)Max', stats)
        except TendrlPerformanceMonitoringException as ex:
            Event(
                ExceptionMessage(
                    priority="debug",
                    publisher=NS.publisher_id,
                    payload={"message": 'Failed to get latest stats of %s of '
                                        'node %s for node summary.'
                                        % (resource, node),
                             "exception": ex
                             }
                )
            )
            raise ex

    def get_net_host_cpu_utilization(self, node):
        try:
            percent_user = self.get_latest_stat(node, 'cpu.percent-user')
            percent_system = self.get_latest_stat(node, 'cpu.percent-system')
            return {
                'percent_used': str(percent_user + percent_system),
                'updated_at': datetime.datetime.now().isoformat()
            }
        except TendrlPerformanceMonitoringException:
            # Exception already handled
            return None

    def get_net_host_memory_utilization(self, node):
        try:
            used = self.get_latest_stat(node, 'memory.memory-used')
            total = self.get_latest_stat(node, 'aggregation-memory-sum.memory')
            percent_used = self.get_latest_stat(node, 'memory.percent-used')
            return {
                'used': str(used),
                'percent_used': str(percent_used),
                'total': str(total),
                'updated_at': datetime.datetime.now().isoformat()
            }
        except TendrlPerformanceMonitoringException:
            # Exception already handled
            return None

    def get_net_storage_utilization(self, node):
        try:
            used_stats = self.get_latest_stats(node, 'df-*.df_complex-used')
            used = 0.0
            for stat in used_stats:
                if not math.isnan(float(stat)):
                    used = used + float(stat)
            free_stats = self.get_latest_stats(node, 'df-*.df_complex-free')
            free = 0.0
            for stat in free_stats:
                if not math.isnan(float(stat)):
                    free = free + float(stat)
            if free + used == 0:
                return None
            percent_used = float(used * 100) / float(free + used)
            return {
                'used': str(used),
                'total': str(used + free),
                'percent_used': str(percent_used),
                'updated_at': datetime.datetime.now().isoformat()
            }
        except TendrlPerformanceMonitoringException:
            # Exception already handled
            return None

    def get_alert_count(self, node):
        try:
            alert_ids = NS.central_store_thread.get_node_alert_ids(node)
            return len(alert_ids)
        except TendrlPerformanceMonitoringException:
            return 0

    def calculate_host_summary(self, node):
        gevent.sleep(1)
        cpu_usage = self.get_net_host_cpu_utilization(node)
        memory_usage = self.get_net_host_memory_utilization(node)
        storage_usage = self.get_net_storage_utilization(node)
        alert_count = self.get_alert_count(node)
        old_summary = NodeSummary(
            node_id=node,
            cpu_usage={
                'percent_used': '',
                'updated_at': ''
            },
            memory_usage={
                'percent_used': '',
                'updated_at': '',
                'used': '',
                'total': ''
            },
            storage_usage={
                'percent_used': '',
                'total': '',
                'used': '',
                'updated_at': ''
            },
            alert_count=alert_count
        )
        try:
            NS.etcd_orm.client.read(old_summary.value)
            old_summary = old_summary.load()
        except EtcdKeyNotFound:
            pass
        except (EtcdConnectionFailed, Exception) as ex:
            Event(
                ExceptionMessage(
                    priority="debug",
                    publisher=NS.publisher_id,
                    payload={"message": 'Failed to fetch previously computed '
                                        'summary from etcd.',
                             "exception": ex
                             }
                )
            )
            return
        if cpu_usage is None:
            cpu_usage = old_summary.cpu_usage
        if memory_usage is None:
            memory_usage = old_summary.memory_usage
        if storage_usage is None:
            storage_usage = old_summary.storage_usage
        summary = NodeSummary(
            node,
            cpu_usage,
            memory_usage,
            storage_usage,
            alert_count
        )
        try:
            summary.save()
        except Exception as ex:
            Event(
                ExceptionMessage(
                    priority="error",
                    publisher=NS.publisher_id,
                    payload={"message": 'Exception caught while trying to '
                                        'save %s' % str(summary.__dict__),
                             "exception": ex
                             }
                )
            )

    def calculate_host_summaries(self):
        nodes = NS.central_store_thread.get_node_ids()
        for node in nodes:
            gevent.spawn(self.calculate_host_summary, node)

    def run(self):
        while not self._complete.is_set():
            self.calculate_host_summaries()
            time.sleep(60)

    def stop(self):
        self._complete.set()
