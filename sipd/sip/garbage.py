# Copyright 2018 (c) Herbert Shin  https://github.com/initbar/sipd
#
# This source code is licensed under the MIT license.

import logging
import threading
import time

from collections import deque
from src.rtp.server import SynchronousRTPRouter

# from multiprocessing import Queue
try:
    from Queue import Queue
except ImportError:
    from queue import Queue

logger = logging.getLogger()


class CallContainer(object):
    """ call information container.
    """

    def __init__(self):
        """
        @history<deque> -- record of managed Call-ID by garbage collector.
        @metadata<dict> -- CallMetadata objects index by Call-ID in history.
        @count<int> -- general statistics of total received calls.
        """
        self.history = deque(maxlen=(0xffff - 6000) // 2)
        self.metadata = {}
        self.count = 0  # only increment.

    def increment_count(self):
        self.count += 1


class CallMetadata(object):
    """ call metadata container.
    """

    def __init__(self, expiration):
        self.expiration = expiration
        # TODO: add more.


class AsynchronousGarbageCollector(object):
    """ asynchronous garbage collector implementation.
    """

    def __init__(self, settings={}):
        """
        @settings<dict> -- `config.json`
        """
        self.settings = settings
        self.loop_interval = float(settings["gc"]["loop_interval"])
        self.call_lifetime = float(settings["gc"]["call_lifetime"])

        # call information and metadata.
        self.calls = CallContainer()
        self.rtp = None

        # instead of directly manipulating garbage using multiple threads,
        # demultiplex tasks into a thread-safe queue and consume later.
        self.__tasks = Queue()

        self.is_ready = False  # recyclable state.
        self.initialize_garbage_collector()
        logger.debug("<gc>: successfully initialized garbage collector.")

    def initialize_garbage_collector(self):
        """ create a garbage collector thread.
        """

        def create_thread():
            while True:
                time.sleep(self.loop_interval)
                self.consume_tasks()

        thread = threading.Thread(name="garbage-collector", target=create_thread)
        self.__thread = thread
        self.__thread.daemon = True
        self.__thread.start()
        self.is_ready = True

    def queue_task(self, function):
        """ demultiplex a new future garbage collector task.
        """
        if function:
            self.__tasks.put(item=function)
            logger.debug("<gc>: queued task %s", function)
            logger.debug("<gc>: queue size %s", self.__tasks.qsize())

    def consume_tasks(self):
        """ consume demultiplexed garbage collector tasks.
        """
        if not self.is_ready:
            return
        self.is_ready = False  # garbage collector is busy.

        if self.rtp is None:
            self.rtp = SynchronousRTPRouter(self.settings)

        # consume deferred tasks.
        while not self.__tasks.empty():
            try:
                task = self.__tasks.get()
                task()  # deferred execution.
                logger.debug("<gc>: executed deferred task: %s", task)
            except TypeError:
                logger.error("<gc>: expected task: received %s", task)

        now = int(time.time())
        try:  # remove calls from management.
            for _ in list(self.calls.history):
                # since call queue is FIFO, the oldest call is placed on top
                # (left) and the youngest call is placed on the bottom (right).
                call_id = self.calls.history.popleft()
                # if there is no metadata aligned with Call-ID or the current
                # Call-ID has already expired, then force the RTP handler to
                # relieve ports allocated for Call-ID.
                metadata = self.calls.metadata.get(call_id)
                if not metadata:
                    continue
                if now > metadata.expiration:
                    self.revoke(call_id=call_id, expired=True)
                # since the oldest call is yet to expire, that means remaining
                # calls also don't need to be checked.
                else:
                    self.calls.history.appendleft(call_id)
                    break
        except AttributeError:
            self.rtp = None  # unset to re-initialize at next iteration.
        finally:
            self.is_ready = True  # garbage collector is available.

    def register(self, call_id):
        """ register Call-ID and its' metadata.
        """
        if call_id is None or call_id in self.calls.history:
            return
        metadata = CallMetadata(expiration=time.time() + self.call_lifetime)
        self.calls.history.append(call_id)
        self.calls.metadata[call_id] = metadata
        self.calls.increment_count()
        logger.info("<gc>: new call registered: %s", call_id)
        logger.debug("<gc>: total unique calls: %s", self.calls.count)

    def revoke(self, call_id, expired=False):
        """ force remove Call-ID and its' metadata.
        """
        if call_id is None:
            return
        if self.calls.metadata.get(call_id):
            del self.calls.metadata[call_id]
        self.rtp.send_stop_signal(call_id=call_id)
        if expired:
            logger.debug("<gc>: call removed (expired): %s", call_id)
        else:
            logger.debug("<gc>: call removed (signal): %s", call_id)
