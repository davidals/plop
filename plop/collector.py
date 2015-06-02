from __future__ import with_statement
import collections
import os
import signal
import sys
import thread
import time
import argparse
import syslog

from plop import platform


class Collector(object):
    MODES = {
        'prof': (platform.ITIMER_PROF, signal.SIGPROF),
        'virtual': (platform.ITIMER_VIRTUAL, signal.SIGVTALRM),
        'real': (platform.ITIMER_REAL, signal.SIGALRM),
    }

    def __init__(self, interval=0.01, mode='virtual'):
        self.interval = interval
        self.mode = mode
        assert mode in Collector.MODES
        timer, sig = Collector.MODES[self.mode]
        signal.signal(sig, self.handler)
        signal.siginterrupt(sig, False)
        self.reset()

    def reset(self):
        self.stacks = list()
        self.samples_remaining = 0
        self.stopping = False
        self.stopped = False

        self.samples_taken = 0
        self.sample_time = 0

    def start(self, duration=30.0):
        self.stopping = False
        self.stopped = False
        self.samples_remaining = int(duration / self.interval)
        timer, sig = Collector.MODES[self.mode]
        platform.setitimer(timer, self.interval, self.interval)

    def stop(self):
        self.stopping = True
        self.wait()

    def wait(self):
        while not self.stopped:
            pass  # need busy wait; ITIMER_PROF doesn't proceed while sleeping

    def handler(self, sig, current_frame):
        start = time.time()
        self.samples_remaining -= 1
        if self.samples_remaining <= 0 or self.stopping:
            platform.setitimer(Collector.MODES[self.mode][0], 0, 0)
            self.stopped = True
            return
        current_tid = thread.get_ident()
        for tid, frame in sys._current_frames().items():
            if tid == current_tid:
                frame = current_frame
            frames = []
            while frame is not None:
                code = frame.f_code
                frames.append((code.co_filename, code.co_firstlineno, code.co_name))
                frame = frame.f_back
            self.stacks.append(frames)
        end = time.time()
        self.samples_taken += 1
        self.sample_time += (end - start)


class CollectorFormatter(object):
    """
    Abstract class for output formats
    """
    def format(self, collector):
        raise Exception("not implemented")

    def store(self, collector, filename):
        with open(filename, "wb") as f:
            f.write(self.format(collector))


class PlopFormatter(CollectorFormatter):
    """
    Formats stack frames for plop.viewer
    """
    def __init__(self, max_stacks=50):
        self.max_stacks = 50

    def format(self, collector):
        # defaultdict instead of counter for pre-2.7 compatibility
        stack_counts = collections.defaultdict(int)
        for frames in collector.stacks:
            stack_counts[tuple(frames)] += 1
        stack_counts = dict(sorted(stack_counts.iteritems(),
                                key=lambda kv: -kv[1])[:self.max_stacks])
        return repr(stack_counts)


class FlamegraphFormatter(CollectorFormatter):
    """
    Creates Flamegraph files
    """
    def format(self, collector):
        output = ""
        previous = None
        previous_count = 1
        for stack in collector.stacks:
            current = self.format_flame(stack)
            if current == previous:
                previous_count += 1
            else:
                output += "%s %d\n" % (previous, previous_count)
                previous_count = 1
                previous = current
        output += "%s %d\n" % (previous, previous_count)
        return output

    def format_flame(self, stack):
        funcs = map("{0[2]} ({0[0]}:{0[1]})".format, reversed(stack))
        return ";".join(funcs)


def print_msg(msg, to_syslog, log_level=syslog.LOG_INFO):
    if to_syslog:
        syslog.syslog(log_level, msg)
    else:
        print msg


def main():
    # TODO: more options, refactor this into somewhere shared
    # between tornado.autoreload and auto2to3
    parser = argparse.ArgumentParser(description="Plop: Python Low-Overhead Profiler",
                                     prog="python -m plop.collector",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--format", "-f", help="Output format",
                        choices=["plop", "flamegraph"], default="plop")
    parser.add_argument("--module", "-m", help="Execute target as a module",
                        action="store_const", const=True, default=False)
    parser.add_argument("--mode", help="Interval timer mode to use, see `man 2 setitimer`",
                        choices=["prof", "real", "virtual"], default="prof")
    parser.add_argument("--interval", help="Timer interval in seconds", default=0.01, type=float)
    parser.add_argument("--duration", help="Profiling duration in seconds", default=3600,
                        type=int)
    parser.add_argument("--max-stacks", help=("Number of most frequent stacks to store."
                                              " Ignored for Flamegraph output."), type=int, default=50)
    parser.add_argument("--output-folder", help=("Folder where the profiles will be saved."),
                        type=str, default="profiles")
    parser.add_argument("--syslog", help=("Should this output to syslog, or stdout."),
                        type=bool, default="False")

    parser.add_argument("target", help="Module or script to run")
    parser.add_argument("arguments", nargs=argparse.REMAINDER,
                        help="Pass-through arguments for the profiled application")
    args = parser.parse_args()
    sys.argv = [args.target] + args.arguments

    if args.syslog:
        syslog.openlog()

    if args.format == "flamegraph":
        extension = "flame"
        formatter = FlamegraphFormatter()
    elif args.format == "plop":
        extension = "plop"
        formatter = PlopFormatter(max_stacks=args.max_stacks)
    else:
        msg = "Unhandled output format: %s" % args.format
        if args.syslog:
            print_msg(msg, True, syslog.LOG_ERR)
        else:
            sys.stderr.write(msg)
            sys.stderr.flush()
        sys.exit(1)


    if not os.path.exists(args.output_folder):
        msg = "Creating output folder: %s" % args.output_folder
        print_msg(msg, args.syslog)

        os.mkdir(args.output_folder)
    filename = '%s/%s-%s.%s' % (args.output_folder,
                                os.path.basename(args.target),
                                time.strftime('%Y%m%d-%H%M-%S'),
                                extension)

    collector = Collector(mode=args.mode, interval=args.interval)
    collector.start(duration=args.duration)
    exit_code = 0
    try:
        if args.module:
            import runpy
            runpy.run_module(args.target, run_name="__main__", alter_sys=True)
        else:
            with open(args.target) as f:
                global __file__
                __file__ = args.target
                # Use globals as our "locals" dictionary so that
                # something that tries to import __main__ (e.g. the unittest
                # module) will see the right things.
                exec f.read() in globals(), globals()
    except SystemExit, e:
        exit_code = e.code
    collector.stop()
    if collector.samples_taken:
        formatter.store(collector, filename)
        msg = "profile output saved to %s" % filename
        print_msg(msg, args.syslog)
        overhead = float(collector.sample_time) / collector.samples_taken
        print_msg(msg, args.syslog)
        msg = "overhead was %s per sample (%s%%)" % (
            overhead, overhead / collector.interval)
        print_msg(msg, args.syslog)
    else:
        msg = "no samples collected; program was too fast"
        print_msg(msg, args.syslog)

    sys.exit(exit_code)


if __name__ == '__main__':
    main()
