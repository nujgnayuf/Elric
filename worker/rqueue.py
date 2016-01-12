# -*- coding: utf-8 -*-
from __future__ import (absolute_import, unicode_literals)

import redis
from core.job import Job
from core.exceptions import AlreadyRunningException, log_exception
from xmlrpclib import Binary
from worker.base import BaseWorker
from jobqueue.rqueue import RedisJobQueue
from executor.pool import ProcessPoolExecutor
from tzlocal import get_localzone
from trigger.tool import create_trigger
from core.rpc import rpc_client_call
from settings import REDIS_HOST, REDIS_PORT
import time


class RQWorker(BaseWorker):
    def __init__(self, name, listen_keys=None, worker_num=2, timezone=None):
        BaseWorker.__init__(self, name)
        self.job_queue = RedisJobQueue(redis.Redis(host=REDIS_HOST, port=REDIS_PORT), self)
        self.listen_keys = []
        if listen_keys:
            self.listen_keys = ['%s:%s' % (self.name, listen_key) for listen_key in listen_keys]
        self.timezone = timezone or get_localzone()
        self.executor = ProcessPoolExecutor(worker_num, self)
        self._stopped = True

    def start(self):
        if self.running:
            raise AlreadyRunningException

        self._stopped = False
        self.log.debug('elric worker running..')
        while self.running:
            try:
                key, serialized_job = self.job_queue.dequeue_any(self.listen_keys)
                job = Job.deserialize(serialized_job)
                self.log.debug('get job id=[%s] func=[%s]from key %s' % (job.id, job.func, key))
                self.executor.execute_job(job)
            except TypeError as e:
                self.log.error(e)
                time.sleep(60)
                continue

    @log_exception
    def submit_job(self, func, job_key, args=None, kwargs=None, trigger=None, job_id=None,
                   replace_exist=False, filter_key='', filter_value='', **trigger_args):
        """
            submit job to master through rpc
            :type func: str or callable obj or unicode
            :type job_key: str or unicode
            :type args: tuple or list
            :type kwargs: dict
            :type trigger: str or unicode
            :type job_id: str or unicode
            :type replace_exist: bool
            :type trigger_args: dict
        """
        job_key = '%s:%s' % (self.name, job_key)
        # use worker's timezone if trigger don't provide specific `timezone` configuration
        trigger_args['timezone'] = self.timezone
        job_in_dict = {
            'id': job_id,
            'func': func,
            'args': args,
            'trigger': create_trigger(trigger, trigger_args) if trigger else None,
            'kwargs': kwargs,
            'filter_key': '%s_%s' % (self.name, filter_key),
            'filter_value': filter_value,
        }
        job = Job(**job_in_dict)
        rpc_client_call('submit_job', Binary(job.serialize()), job_key, job.id, replace_exist)

    @log_exception
    def update_job(self, job_id, job_key, next_run_time, serialized_job):
        """
            send update job request to master through rpc
            :type job_id: str
            :type job_key: str
            :type next_run_time: datetime.datetime
            :type serialized_job: str
        """
        job_key = '%s:%s' % (self.name, job_key)
        rpc_client_call('update_job', job_id, job_key,
                        next_run_time, Binary(serialized_job))

    @log_exception
    def remove_job(self, job_id):
        """
            send remove job request to master through rpc
            :type job_id: str
        """
        rpc_client_call('remove_job', job_id)

    @property
    def running(self):
        return not self._stopped

    def stop(self):
        self.log.debug('Worker is quiting')
        self._stopped = True
        self.executor.shutdown()
