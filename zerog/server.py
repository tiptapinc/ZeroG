#!/usr/bin/env python
# encoding: utf-8
"""
Copyright (c) 2020 MotiveMetrics. All rights reserved.

"""
import multiprocessing
import signal
import sys
import tornado.web
import tornado.ioloop

import zerog.registry
import zerog.workers

import logging
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

HANDLERS = []
POLL_INTERVAL = 2
POLL_JITTER = 0.1


class Server(tornado.web.Application):
    def __init__(
        self, datastore, jobQueue, ctrlQueue, jobClasses, handlers=[], **kwargs
    ):
        """
        Initialize a ZeroG Server, which is a subclass of Tornado's
        Application class

        Args:
            datastore: Datastore object for persisting jobs.

            jobQueue: Queue for sharing jobs with workers

            ctrlQueue: Queue for sharing control information among servers

            jobClasses: List of job classes (derived from BaseClass) that
                        this ZeroG instance will support. Additional job
                        classes can be added later with the add_to_registry
                        method

            *args: passed to parent __init__ method

            **kwargs: passed to parent __init__method
        """
        signal.signal(signal.SIGTERM, self.sig_handler)
        signal.signal(signal.SIGINT, self.sig_handler)

        log.debug("initializing ZeroG server")
        argsStr = (
            "\n  datastore: %s\n  jobQueue: %s\n  ctrlQueue: %s" %
            (datastore, jobQueue, ctrlQueue)
        )
        argsStr += "\n  jobClasses:"
        for jc in jobClasses:
            argsStr += "\n    %s" % jc
        log.debug(argsStr)

        self.datastore = datastore
        self.jobQueue = jobQueue
        self.ctrlQueue = ctrlQueue

        self.registry = zerog.registry.JobRegistry(datastore, jobQueue)
        self.registry.add_classes(jobClasses)

        self.make_worker()

        handlers += HANDLERS
        log.info("initializing Tornado parent, handlers:%s" % handlers)
        super(Server, self).__init__(handlers, **kwargs)

    def sig_handler(self, sig, frame):
        """
        Got a SIGINT or SIGTERM.
            - For now: suicide
            - To do: gracefully shutdown server
        """
        log.info("signal. sig: %s, frame: %s" % (sig, frame))
        sys.exit()

    def make_worker(self):
        log.debug("creating ZeroG worker")

        self.parentConn, self.childConn = multiprocessing.Pipe()
        self.worker = zerog.workers.BaseWorker(
            self.datastore, self.jobQueue, self.registry, self.childConn
        )

        log.debug("starting worker.run process")

        self.proc = multiprocessing.Process(target=self.worker.run)
        self.proc.daemon = True
        self.proc.start()

        log.debug("setting callback for worker_poll")

        tornado.ioloop.IOLoop.instance().call_later(
            0, self.worker_poll
        )

    def worker_poll(self):
        if self.parentConn.poll() is True:
            msg = self.parentConn.recv()
            log.debug("message from worker: %s" % msg)

        tornado.ioloop.IOLoop.instance().call_later(
            POLL_INTERVAL, self.worker_poll
        )
