from membase.api.rest_client import RestConnection
from memcached.helper.data_helper import MemcachedClientHelper
from remote.remote_util import RemoteMachineShellConnection
from mc_bin_client import MemcachedClient
from membase.helper.rebalance_helper import RebalanceHelper
import memcacheConstants
import logging
import time
import Queue
from threading import Thread
import traceback


class ClusterOperationHelper(object):
    # the first ip is taken as the master ip

    # Returns True if cluster successfully finished then rebalance
    @staticmethod
    def add_and_rebalance(servers, wait_for_rebalance=True):
        master = servers[0]
        all_nodes_added = True
        rebalanced = True
        log = logging.getLogger("infra")
        rest = RestConnection(master)
        if len(servers) > 1:
            for serverInfo in servers[1:]:
                log.info('adding node : {0}:{1} to the cluster'.format(
                        serverInfo.ip, serverInfo.port))
                otpNode = rest.add_node(master.rest_username, master.rest_password, serverInfo.ip, port=serverInfo.port)
                if otpNode:
                    log.info('added node : {0} to the cluster'.format(otpNode.id))
                else:
                    all_nodes_added = False
                    break
            if all_nodes_added:
                rest.rebalance(otpNodes=[node.id for node in rest.node_statuses()], ejectedNodes=[])
                if wait_for_rebalance:
                    rebalanced &= rest.monitorRebalance()
                else:
                    rebalanced = False
        return all_nodes_added and rebalanced

    @staticmethod
    def add_all_nodes_or_assert(master, all_servers, rest_settings, test_case):
        otpNodes = []
        all_nodes_added = True
        log = logging.getLogger("infra")
        rest = RestConnection(master)
        for serverInfo in all_servers:
            if serverInfo.ip != master.ip:
                log.info('adding node : {0}:{1} to the cluster'.format(
                        serverInfo.ip, serverInfo.port))
                otpNode = rest.add_node(rest_settings.rest_username,
                                        rest_settings.rest_password,
                                        serverInfo.ip)
                if otpNode:
                    log.info('added node : {0} to the cluster'.format(otpNode.id))
                    otpNodes.append(otpNode)
                else:
                    all_nodes_added = False
        if not all_nodes_added:
            if test_case:
                test_case.assertTrue(all_nodes_added,
                                     msg="unable to add all the nodes to the cluster")
            else:
                log.error("unable to add all the nodes to the cluster")
        return otpNodes

    @staticmethod
    def verify_persistence(servers, test, keys_count=400000, timeout_in_seconds=300):
        master = servers[0]
        log = logging.getLogger("infra")
        rest = RestConnection(master)
        log.info("Verifying Persistence")
        buckets = rest.get_buckets()
        for bucket in buckets:
            # Load some data
            l_threads = MemcachedClientHelper.create_threads(
                [master], bucket.name, - 1, keys_count,
                {1024: 0.50, 512: 0.50}, 2, -1, True, True)
            [t.start() for t in l_threads]
            # Do persistence verification
            ready = ClusterOperationHelper.persistence_verification(servers, bucket.name, timeout_in_seconds)
            log.info("Persistence Verification returned ? {0}".format(ready))
            log.info("waiting for persistence threads to finish...")
            for t in l_threads:
                t.aborted = True
            for t in l_threads:
                t.join()
            log.info("persistence thread has finished...")
            test.assertTrue(ready, msg="Cannot verify persistence")

    @staticmethod
    def persistence_verification(servers, bucket, timeout_in_seconds=1260):
        verification_threads = []
        log = logging.getLogger("infra")
        queue = Queue.Queue()
        rest = RestConnection(servers[0])
        nodes = rest.get_nodes()
        nodes_ip = []
        for node in nodes:
            nodes_ip.append(node.ip)
        for i in range(len(servers)):
            if servers[i].ip in nodes_ip:
                log.info("Server {0}:{1} part of cluster".format(
                        servers[i].ip, servers[i].port))
                rest = RestConnection(servers[i])
                t = Thread(target=ClusterOperationHelper.persistence_verification_per_node,
                           name="verification-thread-{0}".format(servers[i]),
                           args=(rest, bucket, queue, timeout_in_seconds))
                verification_threads.append(t)
        for t in verification_threads:
            t.start()
        for t in verification_threads:
            t.join()
            log.info("thread {0} finished".format(t.name))
        while not queue.empty():
            item = queue.get()
            if item is False:
                return False
        return True

    @staticmethod
    def persistence_verification_per_node(rest, bucket, queue=None, timeout=1260):
        stat_key = 'ep_flusher_todo'
        start = time.time()
        stats = []
        log = logging.getLogger("infra")
        # Collect stats data points
        while time.time() - start <= timeout:
            _new_stats = rest.get_bucket_stats(bucket)
            if _new_stats and 'ep_flusher_todo' in _new_stats:
                stats.append(_new_stats[stat_key])
                time.sleep(0.5)
            else:
                log.error("unable to obtain stats for bucket : {0}".format(bucket))
        value_90th = ClusterOperationHelper.percentile(stats, 90)
        average = float(sum(stats)) / len(stats)
        log.info("90th percentile value is {0} and average {1}".format(value_90th, average))
        if value_90th == 0 and average == 0:
            queue.put(False)
            return
        queue.put(True)

    @staticmethod
    def percentile(samples, percentile):
        element_idx = int(len(samples) * (percentile / 100.0))
        samples.sort()
        value = samples[element_idx]
        return value

    @staticmethod
    def start_cluster(servers):
        for server in servers:
            shell = RemoteMachineShellConnection(server)
            if shell.is_couchbase_installed():
                shell.start_couchbase()
            else:
                shell.start_membase()
            shell.disconnect()

    @staticmethod
    def stop_cluster(servers):
        for server in servers:
            shell = RemoteMachineShellConnection(server)
            if shell.is_couchbase_installed():
                shell.stop_couchbase()
            else:
                shell.stop_membase()
            shell.disconnect()

    @staticmethod
    def flushctl_start(servers, username=None, password=None):
        for server in servers:
            c = MemcachedClient(server.ip, 11210)
            if username:
                c.sasl_auth_plain(username, password)
            c.start_persistence()

    @staticmethod
    def flushctl_stop(servers, username=None, password=None):
        for server in servers:
            c = MemcachedClient(server.ip, 11210)
            if username:
                c.sasl_auth_plain(username, password)
            c.stop_persistence()

    @staticmethod
    def flush_os_caches(servers):
        log = logging.getLogger("infra")
        for server in servers:
            try:
                shell = RemoteMachineShellConnection(server)
                shell.flush_os_caches()
                log.info("Clearing os caches on {0}".format(server))
            except:
                pass
            finally:
                shell.disconnect()

    @staticmethod
    def flushctl_set(master, key, val, bucket='default'):
        rest = RestConnection(master)
        servers = rest.get_nodes()
        for server in servers:
            if "kv" in server.services:
                _server = {"ip": server.ip, "port": server.port,
                           "username": master.rest_username,
                           "password": master.rest_password}
                ClusterOperationHelper.flushctl_set_per_node(_server, key, val, bucket)

    @staticmethod
    def flushctl_set_per_node(server, key, val, bucket='default'):
        log = logging.getLogger("infra")
        rest = RestConnection(server)
        node = rest.get_nodes_self()
        mc = MemcachedClientHelper.direct_client(server, bucket)
        log.info("Setting flush param on server {0}, {1} to {2} on {3}"
                 .format(server, key, val, bucket))
        # Workaround for CBQE-249, ideally this should be node.version
        index_path = node.storage[0].get_index_path()
        if index_path is '':
            # Indicates non 2.0 build
            rv = mc.set_flush_param(key, str(val))
        else:
            type = ClusterOperationHelper._get_engine_param_type(key)

            if val == 'true' or val == 'false':
                rv = mc.set_param(key, val, type)
            else:
                rv = mc.set_param(key, str(val), type)

        log.info("Setting flush param on server {0}, {1} to {2}, result: {3}"
                 .format(server, key, val, rv))
        mc.close()

    @staticmethod
    def _get_engine_param_type(key):
        tap_params = ['tap_keepalive', 'tap_throttle_queue_cap', 'tap_throttle_threshold']
        checkpoint_params = ['chk_max_items', 'chk_period', 'inconsistent_slave_chk', 'keep_closed_chks',
                             'max_checkpoints', 'item_num_based_new_chk']
        flush_params = ['bg_fetch_delay', 'couch_response_timeout', 'exp_pager_stime', 'flushall_enabled',
                        'klog_compactor_queue_cap', 'klog_max_log_size', 'klog_max_entry_ratio',
                        'queue_age_cap', 'max_size', 'max_txn_size', 'mem_high_wat', 'mem_low_wat',
                        'min_data_age', 'timing_log', 'alog_sleep_time', 'bfilter_enabled' ]
        if key in tap_params:
            return memcacheConstants.ENGINE_PARAM_TAP
        if key in checkpoint_params:
            return memcacheConstants.ENGINE_PARAM_CHECKPOINT
        if key in flush_params:
            return memcacheConstants.ENGINE_PARAM_FLUSH

    @staticmethod
    def set_expiry_pager_sleep_time(master, bucket, value=30):
        log = logging.getLogger("infra")
        rest = RestConnection(master)
        servers = rest.get_nodes()
        for server in servers:
            # this is not bucket specific so no need to pass in the bucketname
            log.info("connecting to memcached {0}:{1}".format(server.ip, server.memcached))
            mc = MemcachedClientHelper.direct_client(server, bucket)
            log.info("Set exp_pager_stime flush param on server {0}:{1}".format(server.ip, server.port))
            try:
                mc.set_flush_param("exp_pager_stime", str(value))
                log.info("Set exp_pager_stime flush param on server {0}:{1}".format(server.ip, server.port))
            except Exception as ex:
                traceback.print_exc()
                log.error("Unable to set exp_pager_stime flush param on memcached {0}:{1}".format(server.ip, server.memcached))

    @staticmethod
    def get_mb_stats(servers, key):
        log = logging.getLogger("infra")
        for server in servers:
            c = MemcachedClient(server.ip, 11210)
            log.info("Get flush param on server {0}, {1}".format(server, key))
            value = c.stats().get(key, None)
            log.info("Get flush param on server {0}, {1}".format(server, value))
            c.close()

    @staticmethod
    def change_erlang_threads_values(servers, sync_threads=True, num_threads='16:16'):
        """Change the the type of sync erlang threads and its value
           sync_threads=True means sync threads +S with default threads number equal 16:16
           sync_threads=False means async threads: +A 16, for instance

        Default: +S 16:16
        """
        log = logging.getLogger("infra")
        for server in servers:
            sh = RemoteMachineShellConnection(server)
            product = "membase"
            if sh.is_couchbase_installed():
                product = "couchbase"

            sync_type = sync_threads and "S" or "A"

            command = "sed -i 's/+[A,S] .*/+%s %s \\\/g' /opt/%s/bin/%s-server" % \
                 (sync_type, num_threads, product, product)
            o, r = sh.execute_command(command)
            sh.log_command_output(o, r)
            msg = "modified erlang +%s to %s for server %s"
            log.info(msg % (sync_type, num_threads, server.ip))
            sh.disconnect()

    @staticmethod
    def set_erlang_schedulers(servers, value="16:16"):
        """
        Set num of erlang schedulers.
        Also erase async option (+A)
        """
        log = logging.getLogger("infra")
        ClusterOperationHelper.stop_cluster(servers)

        for server in servers:
            sh = RemoteMachineShellConnection(server)
            product = "membase"
            if sh.is_couchbase_installed():
                product = "couchbase"
            command = "sed -i 's/S\+ 128:128/S %s/' /opt/%s/bin/%s-server"\
                      % (value, product, product)
            o, r = sh.execute_command(command)
            sh.log_command_output(o, r)
            log.info("modified erlang +A to %s for server %s"
                     % (value, server.ip))
            sh.disconnect()

        ClusterOperationHelper.start_cluster(servers)

    @staticmethod
    def change_erlang_gc(servers, value=None):
        """Change the frequency of erlang_gc process
           export ERL_FULLSWEEP_AFTER=0 (most aggressive)

        Default: None
        """
        log = logging.getLogger("infra")
        if value is None:
            return
        for server in servers:
            sh = RemoteMachineShellConnection(server)
            product = "membase"
            if sh.is_couchbase_installed():
                product = "couchbase"
            command = "sed -i '/exec erl/i export ERL_FULLSWEEP_AFTER=%s' /opt/%s/bin/%s-server" % \
                      (value, product, product)
            o, r = sh.execute_command(command)
            sh.log_command_output(o, r)
            msg = "modified erlang gc to full_sweep_after %s on %s " % (value, server.ip)
            log.info(msg)
            sh.disconnect()

    @staticmethod
    def begin_rebalance_in(master, servers, timeout=5):
        RebalanceHelper.begin_rebalance_in(master, servers, timeout)

    @staticmethod
    def begin_rebalance_out(master, servers, timeout=5):
        RebalanceHelper.begin_rebalance_out(master, servers, timeout)

    @staticmethod
    def end_rebalance(master):
        RebalanceHelper.end_rebalance(master)

    @staticmethod
    # Returns the otpNode for Orchestrator
    def find_orchestrator(master):
        rest = RestConnection(master)
        command = "node(global:whereis_name(ns_orchestrator))"
        status, content = rest.diag_eval(command)
        # Get rid of single quotes 'ns_1@10.1.3.74'
        content = content.replace("'", '')
        return status, content

    @staticmethod
    def set_vbuckets(master, vbuckets):
        rest = RestConnection(master)
        command = "rpc:eval_everywhere(ns_config, set, [couchbase_num_vbuckets_default, {0}]).".format(vbuckets)
        status, content = rest.diag_eval(command)
        return status, content
