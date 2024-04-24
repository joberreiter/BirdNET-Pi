import logging
import os
import os.path
import re
import signal
import sys
import threading
from dataclasses import dataclass, field
from queue import Queue, PriorityQueue
from subprocess import CalledProcessError
from typing import Any

import inotify.adapters
from inotify.constants import IN_CLOSE_WRITE

from server import load_global_model, run_analysis
from utils.helpers import get_settings, ParseFileName, get_wav_files, write_settings, ANALYZING_NOW
from utils.reporting import extract_detection, summary, write_to_file, write_to_db, apprise, bird_weather, heartbeat, \
    update_json_file

shutdown = False

log = logging.getLogger(__name__)


def sig_handler(sig_num, curr_stack_frame):
    global shutdown
    log.info('Caught shutdown signal %d', sig_num)
    shutdown = True


@dataclass(order=True)
class PrioritizedItem:
    priority: int
    item: Any = field(compare=False)


def main():
    write_settings()
    load_global_model()
    conf = get_settings()
    i = inotify.adapters.Inotify()
    i.add_watch(os.path.join(conf['RECS_DIR'], 'StreamData'), mask=IN_CLOSE_WRITE)

    backlog = get_wav_files()

    notify_queue1 = PriorityQueue()
    notify_thread1 = threading.Thread(target=handle_notify_queue, args=(notify_queue1, apprise))
    notify_thread1.start()
    notify_queue2 = PriorityQueue()
    notify_thread2 = threading.Thread(target=handle_notify_queue, args=(notify_queue2, bird_weather))
    notify_thread2.start()
    report_queue = Queue()
    reporting_thread = threading.Thread(target=handle_reporting_queue, args=(report_queue, notify_queue1, notify_queue2))
    reporting_thread.start()

    log.info('backlog is %d', len(backlog))
    for file_name in backlog:
        process_file(file_name, report_queue)
        if shutdown:
            break
    log.info('backlog done')

    empty_count = 0
    for event in i.event_gen():
        if shutdown:
            break

        if event is None:
            if empty_count > (conf.getint('RECORDING_LENGTH') * 2):
                log.error('no more notifications: restarting...')
                break
            empty_count += 1
            continue

        (_, type_names, path, file_name) = event
        if re.search('.wav$', file_name) is None:
            continue
        log.debug("PATH=[%s] FILENAME=[%s] EVENT_TYPES=%s", path, file_name, type_names)

        file_path = os.path.join(path, file_name)
        if file_path in backlog:
            # if we're very lucky, the first event could be for the file in the backlog that finished
            # while running get_wav_files()
            backlog = []
            continue

        process_file(file_path, report_queue)
        empty_count = 0

    # we're all done
    report_queue.put(None)
    reporting_thread.join()
    notify_thread1.join()
    notify_thread2.join()
    report_queue.join()


def process_file(file_name, report_queue):
    try:
        if os.path.getsize(file_name) == 0:
            os.remove(file_name)
            return
        log.info('Analyzing %s', file_name)
        with open(ANALYZING_NOW, 'w') as analyzing:
            analyzing.write(file_name)
        file = ParseFileName(file_name)
        detections = run_analysis(file)
        # we join() to make sure te reporting queue does not get behind
        # we only join report_queue, because we do not care if the external report_queue gets behind
        if not report_queue.empty():
            log.warning('reporting queue not yet empty')
        report_queue.join()
        report_queue.put((file, detections))
    except BaseException as e:
        stderr = e.stderr.decode('utf-8') if isinstance(e, CalledProcessError) else ""
        log.exception(f'Unexpected error: {stderr}', exc_info=e)


def handle_reporting_queue(queue, notify_queue1, notify_queue2):
    while True:
        msg = queue.get()
        # check for signal that we are done
        if msg is None:
            break

        file, detections = msg
        try:
            update_json_file(file, detections)
            for detection in detections:
                detection.file_name_extr = extract_detection(file, detection)
                log.info('%s;%s', summary(file, detection), os.path.basename(detection.file_name_extr))
                write_to_file(file, detection)
                write_to_db(file, detection)
            heartbeat()
            if detections:
                notify_queue1.put(PrioritizedItem(10, (file, detections)))
                notify_queue2.put(PrioritizedItem(10, (file, detections)))
            os.remove(file.file_name)
        except BaseException as e:
            stderr = e.stderr.decode('utf-8') if isinstance(e, CalledProcessError) else ""
            log.exception(f'Unexpected error: {stderr}', exc_info=e)

        queue.task_done()

    notify_queue1.put(PrioritizedItem(0, None))
    notify_queue2.put(PrioritizedItem(0, None))
    # mark the 'None' signal as processed
    queue.task_done()
    log.info('handle_reporting_queue done')


def handle_notify_queue(queue, fn):
    while True:
        msg = queue.get().item
        # check for signal that we are done
        if msg is None:
            break

        if queue.qsize() > 200:
            # drop detection, we are too far behind
            log.warning(f'dropping detection from notify_queue {queue.qsize()=}')
            continue

        file, detections = msg
        try:
            fn(file, detections)
        except BaseException as e:
            stderr = e.stderr.decode('utf-8') if isinstance(e, CalledProcessError) else ""
            log.exception(f'Unexpected error: {stderr}', exc_info=e)

    log.info('handle_notify_queue done')


def setup_logging():
    logger = logging.getLogger()
    formatter = logging.Formatter("[%(name)s][%(levelname)s] %(message)s")
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    global log
    log = logging.getLogger('birdnet_analysis')


if __name__ == '__main__':
    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    setup_logging()

    main()
