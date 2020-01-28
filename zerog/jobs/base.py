#!/usr/bin/env python
# encoding: utf-8
"""
Copyright (c) 2017 MotiveMetrics. All rights reserved.

"""

#
# NOTES:
#   - make it possible to kill a job??
#

from abc import ABC, abstractmethod
import datetime
import random
import time
import uuid

from marshmallow import Schema, fields

import zerog.datastores

from .error import ErrorSchema, make_error
from .event import EventSchema, make_event
from .warning import WarningSchema, make_warning

import logging
log = logging.getLogger(__name__)

DEFAULT_JOB_LIFESPAN = 0
DEFAULT_TTR = 3600 * 24   # long because broken workers are killed

JOB_DOCUMENT_TYPE = "geyser_job"
BASE_JOB_TYPE = "geyser_base"

# result codes
INTERNAL_ERROR = 500
NO_RESULT = -1


class ErrorContinue(Exception):
    pass


class ErrorFinish(Exception):
    pass


class WarningFinish(Exception):
    pass


class BaseJobSchema(Schema):
    """
    Base Job Schema

    """
    documentType = fields.String()
    jobType = fields.String()
    schemaVersion = fields.Float()

    createdAt = fields.DateTime(format="iso")
    updatedAt = fields.DateTime(format="iso")
    cas = fields.Integer()

    uuid = fields.String()
    logId = fields.String()

    queueName = fields.String()
    queueKwargs = fields.Dict()
    queueJobId = fields.Integer()

    events = fields.List(fields.Nested(EventSchema))
    errors = fields.List(fields.Nested(ErrorSchema))
    warnings = fields.List(fields.Nested(WarningSchema))

    completeness = fields.Float()
    resultCode = fields.Integer()


class BaseJob(ABC):
    DOCUMENT_TYPE = JOB_DOCUMENT_TYPE   # very important for key value ds
    JOB_TYPE = BASE_JOB_TYPE
    LIFESPAN = DEFAULT_JOB_LIFESPAN
    SCHEMA_VERSION = 1.0
    SCHEMA = BaseJobSchema
    QUEUE_NAME = ""

    def __init__(self, datastore, queue, keepalive=None, **kwargs):
        self.datastore = datastore
        self.queue = queue
        self.keepalive = keepalive

        now = datetime.datetime.utcnow()

        self.documentType = kwargs.get('documentType', self.DOCUMENT_TYPE)
        self.jobType = kwargs.get('jobType', self.JOB_TYPE)
        self.schemaVersion = kwargs.get('schemaVersion', self.SCHEMA_VERSION)

        self.createdAt = kwargs.get('createdAt', now)
        self.updatedAt = kwargs.get('updatedAt', now)
        self.cas = kwargs.get('cas', 0)

        self.uuid = kwargs.get('uuid') or str(uuid.uuid4())
        self.logId = kwargs.get(
            'logId',
            "%s_%s" % (self.JOB_TYPE, self.uuid)
        )

        self.queueKwargs = kwargs.get('queueKwargs', {})
        self.queueJobId = kwargs.get('queueJobId', 0)

        self.events = kwargs.get('events', [])
        self.errors = kwargs.get('errors', [])
        self.warnings = kwargs.get('warnings', [])

        self.completeness = kwargs.get('completeness', 0)
        self.resultCode = kwargs.get('resultCode', NO_RESULT)

        self.tickcount = 0

    def dump(self):
        return self.SCHEMA().dump(self)

    def dumps(self, **kwargs):
        return self.SCHEMA().dumps(self, **kwargs)

    def __str__(self):
        return self.dumps(indent=4)

    def key(self):
        return make_key(self.uuid)

    def save(self):
        """
        Saves job instance to the datastore. Current implementation uses
        Couchbase for the datastore.
        """
        self.updatedAt = datetime.datetime.utcnow()
        _, self.cas = self.datastore.set_with_cas(
            self.key(),
            self.dump(),
            cas=self.cas,
            ttl=self.LIFESPAN
        )

    def reload(self):
        """
        Reload job data by repulling from the datastore.

        This may be necessary if another python instance has updated this job
        in the datastore and the cas loaded here is out of date.
        """
        data, cas = self.datastore.read_with_cas(self.key())
        if data:
            data['cas'] = cas
            loaded = self.SCHEMA().load(data)
            self.__init__(**loaded)

    def lock(self, ttl=1):
        _, self.cas = self.datastore.lock(self.key(), ttl=ttl)

    def unlock(self):
        self.datastore.unlock(self.key(), self.cas)

    def record_change(self, func, *args, **kwargs):
        """
        Use func to update this job instance and save the updated instance to
        the datastore.

        In case of an error, reload the job from the datastore and retry.

        NOTE: How much is couchbase dependent?
        """
        for _ in range(10):
            try:
                func(*args, **kwargs)
                self.save()
                return True

            except zerog.datastore.KeyExistsError:
                log.info("%s: datastore collision - reloading." % self.logId)

            except zerog.datastore.TemporaryFailError:
                log.info("%s: locked - reloading." % self.logId)

            time.sleep(random.random() / 10)
            self.reload()

        log.error("%s: save failed - too many tries" % self.logId)
        return False

    def update_attrs(self, **kwargs):
        """
        Updates a job's attributes. Saves the update to the datastore.
        """
        def do_update_attrs():
            for attr, value in kwargs.items():
                setattr(self, attr, value)

        self.record_change(do_update_attrs)

    def record_event(self, msg):
        """
        Makes and records an event associated with this job.
        """
        event = make_event(msg)

        def do_record_event():
            self.events.append(event)

        self.record_change(do_record_event)

    def record_warning(self, msg):
        """
        Makes and records a warning associated with this job.
        """
        warning = make_warning(msg)

        def do_record_warning():
            self.warnings.append(warning)

        self.record_change(do_record_warning)

    def record_error(self, errorCode, msg):
        """
        Makes and records an error associated with this job.
        """
        error = make_error(errorCode, msg)

        def do_record_error():
            self.errors.append(error)

        self.record_change(do_record_error)

    def record_result(self, resultCode, resultString=""):
        """
        Record the result of a job.
        """
        self.update_attrs(
            resultCode=resultCode,
            resultString=resultString,
            completeness=1
        )

    def keep_alive(self):
        if self.keepalive and callable(self.keepalive):
            self.keepalive()

    def job_log_info(self, msg):
        log.info(msg)
        self.record_event(msg)

    def job_log_warning(self, msg):
        log.warning(msg)
        self.record_warning(msg)

    def job_log_error(self, errorCode, msg):
        log.error(msg)
        self.record_error(errorCode, msg)

    def raise_warning_finish(self, resultCode, msg):
        self.job_log_warning(msg)
        self.record_result(resultCode)
        raise WarningFinish

    def raise_error_continue(self, errorCode, msg):
        self.job_log_error(errorCode, msg)
        raise ErrorContinue

    def raise_error_finish(self, errorCode, msg):
        self.job_log_error(errorCode, msg)
        self.record_result(errorCode)
        raise ErrorFinish

    def set_completeness(self, completeness):
        """
        Sets the absolute value of the job's completeness. Clamps
        value to a range of 0.0 to 1.0
        """
        self.keep_alive()
        setval = clamp(completeness, 0.0, 1.0)

        if completeness < 0 or completeness > 1:
            log.warning(
                "completeness %d out of range. Clamping to %d" %
                (completeness, setval)
            )

        self.update_attrs(completeness=setval)

    def add_to_completeness(self, delta):
        """
        Increment the job's completeness. Adds any unrecorded ticks.
        Resulting completeness will be clamped to a range of 0.0 to 1.0.
        """
        self.set_completeness(self.completeness + delta + self.tickcount)

    def set_tick_value(self, tickval):
        """
        Sets the amount the job's completeness will be incremented
        by a call to the tick method
        """
        self.tickval = tickval

    def tick(self):
        """
        Accumulates the job's tickcount. Adds tickcount to completeness
        when it is >= 0.01
        """
        self.tickcount += self.tickval

        if self.tickcount >= 0.01:
            self.add_to_completeness(0)

    def enqueue(self, **kwargs):
        """
        Add a job to its queue. The queue is defined by the subclass of
        BaseJob.
        """

        # if the job has not been added to the database yet, cas is 0
        if self.cas == 0:
            self.save()

        # thought: does this ever enqueue the job but then fail on the update?
        # what happens then?
        kwargs['ttr'] = kwargs.get('ttr', DEFAULT_TTR)
        queueJobId = self.queue.put(self.uuid, **kwargs)

        self.update_attrs(queueKwargs=kwargs, queueJobId=queueJobId)

    def progress(self):
        """
        Returns a job's completeness, result, events, and errors.

        Override this method to add additional return values. Use super
        to call this method and get the base return values.
        """
        return dict(
            completeness=self.completeness,
            result=self.resultCode,
            events=[e.dump() for e in self.events],
            errors=[e.dump() for e in self.errors],
            warnings=[w.dump() for w in self.warnings]
        )

    @abstractmethod
    def run(self):
        # override this method to execute the job. It is called by
        # the base worker once the job has been successfully loaded
        #
        # Must return:
        #
        #   resultCode: resultCode for the job. Return -1 if job needs
        #               to be requeued for further processing. Otherwise
        #               use HTTP resultCodes (200s for success, etc.)
        #
        pass


def make_key(uuid):
    """
    Makes a unique datastore key for a job.

    Args:
        uuid: uuid of the job

    Returns:
        datastore key
    """
    return "%s_%s" % (BaseJob.DOCUMENT_TYPE, uuid)


def clamp(value, minval, maxval):
    return (max(min(maxval, value), minval))