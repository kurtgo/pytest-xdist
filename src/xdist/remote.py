"""
    This module is executed in remote subprocesses and helps to
    control a remote testing session and relay back information.
    It assumes that 'py' is importable and does not have dependencies
    on the rest of the xdist code.  This means that the xdist-plugin
    needs not to be installed in remote environments.
"""
from __future__ import annotations

import contextlib
import sys
import os
import time
from typing import Any
import pickle
import logging
import copy
import traceback

import pytest
from execnet.gateway_base import dumps, DumpError

from _pytest.config import _prepareconfig, Config

try:
    from setproctitle import setproctitle
except ImportError:

    def setproctitle(title):
        pass


class Producer:
    """
    Simplified implementation of the same interface as py.log, for backward compatibility
    since we dropped the dependency on pylib.
    Note: this is defined here because this module can't depend on xdist, so we need
    to have the other way around.
    """

    def __init__(self, name: str, *, enabled: bool = True):
        self.name = name
        self.enabled = enabled

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r}, enabled={self.enabled})"

    def __call__(self, *a: Any, **k: Any) -> None:
        if self.enabled:
            print(f"[{self.name}]", *a, **k, file=sys.stderr)

    def __getattr__(self, name: str) -> "Producer":
        return type(self)(name, enabled=self.enabled)


def worker_title(title):
    try:
        setproctitle(title)
    except Exception:
        # changing the process name is very optional, no errors please
        pass


class DummyNext:
    """ delayed next item collection object
        The idea here is to pass the next item to work on to
        pytest without actually knowing what it is.
        When pytest wants to operate on the next item
        (usually after the previous test is finished),
        we ask the scheduler for another item, instead
        of forcing the scheduler to add two items of
        unknown work size to a worker.

        pytest will call the listchain() function to get the details of
        the next item.

        """
    def __init__(self, parent: WorkerInteractor):
        self.parent = parent
        self.real_next = None
        self.nextitem_index = None

    def __repr__(self):
        return f'DummyNext({self.nextitem_index})'

    def _fetch(self):
        if self.nextitem_index == None:
            self.parent.sendevent("runtest_need_work")
            self.real_next, self.nextitem_index = self.parent.get_more_work()

    def _next_index(self):
        self._fetch()
        return self.nextitem_index

    def listchain(self):
        """ this is a proxy for the next time's listchain """
        self._fetch()

        return self.real_next.listchain() if self.real_next else []

class RemoteMessageHandler(logging.Handler):
    """
    This handler sends events to a queue. Typically, it would be used together
    with a multiprocessing Queue to centralise logging to file in one process
    (in a multi-process application), so as to avoid file write contention
    between processes.

    This code is new in Python 3.2, but this class can be copy pasted into
    user code for use with earlier Python versions.
    """

    def __init__(self, queue):
        """
        Initialise an instance, using the passed queue.
        """
        logging.Handler.__init__(self)
        self.queue = queue

    def emit(self, record):
        """
        Emit a record.

        Writes the LogRecord to the queue, preparing it for pickling first.
        """
        try:
            msg = self.format(record)
            # bpo-35726: make copy of record to avoid affecting other handlers in the chain.
            record = copy.copy(record)
            record.message = msg
            record.msg = msg
            record.args = None
            record.exc_info = None
            record.exc_text = None
            x = pickle.dumps(record)
            self.queue.send_log(x)
        except Exception as e:
            self.handleError(record)

class WorkerInteractor:
    SHUTDOWN_MARK = object()

    def __init__(self, config, channel):
        self.config = config
        self.workerid = config.workerinput.get("workerid", "?")
        self.testrunuid = config.workerinput["testrunuid"]
        self.log = Producer(f"worker-{self.workerid}", enabled=config.option.debug)
        self.channel = channel
        self.torun = self._make_queue()
        self.nextitem_index = None
        config.pluginmanager.register(self)

        # pump cli messages back to master if a level is set
        if config.option.log_cli_level:
            rootlog = logging.getLogger()
            myhandler = RemoteMessageHandler(self)
            rootlog.addHandler(myhandler)
            level = logging.getLevelName(config.option.log_cli_level.upper()) if not config.option.log_cli_level.isnumeric() else int(config.option.log_cli_level)
            myhandler.setLevel(level)

    def _make_queue(self):
        return self.channel.gateway.execmodel.queue.Queue()

    def sendevent(self, name, **kwargs):
        self.log("sending", name, kwargs)
        self.channel.send((name, kwargs))

    def send_log(self, record):
        self.sendevent("runtest_logmessage", record=record)

    @pytest.hookimpl
    def pytest_internalerror(self, excrepr):
        formatted_error = str(excrepr)
        for line in formatted_error.split("\n"):
            self.log("IERROR>", line)
        interactor.sendevent("internal_error", formatted_error=formatted_error)

    @pytest.hookimpl
    def pytest_sessionstart(self, session):
        self.session = session
        workerinfo = getinfodict()
        self.sendevent("workerready", workerinfo=workerinfo)

    @pytest.hookimpl(hookwrapper=True)
    def pytest_sessionfinish(self, exitstatus):
        # in pytest 5.0+, exitstatus is an IntEnum object
        self.config.workeroutput["exitstatus"] = int(exitstatus)
        yield
        self.sendevent("workerfinished", workeroutput=self.config.workeroutput)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_collection(self, session):
        self.sendevent("collectionstart")
        outcome = yield
        if outcome.excinfo:
            (_type, exc, tb) = outcome.excinfo
            msg = "{}".format(exc)
            interactor.sendevent("collect_error", formatted_error=msg)

    def handle_command(self, command):
        if command is self.SHUTDOWN_MARK:
            self.torun.put(self.SHUTDOWN_MARK)
            return

        name, kwargs = command

        self.log("received command", name, kwargs)
        if name == "runtests":
            for i in kwargs["indices"]:
                self.torun.put(i)
        elif name == "runtests_all":
            for i in range(len(self.session.items)):
                self.torun.put(i)
        elif name == "shutdown":
            self.torun.put(self.SHUTDOWN_MARK)
        elif name == "steal":
            self.steal(kwargs["indices"])

    def steal(self, indices):
        indices = set(indices)
        stolen = []

        old_queue, self.torun = self.torun, self._make_queue()

        def old_queue_get_nowait_noraise():
            with contextlib.suppress(self.channel.gateway.execmodel.queue.Empty):
                return old_queue.get_nowait()

        for i in iter(old_queue_get_nowait_noraise, None):
            if i in indices:
                stolen.append(i)
            else:
                self.torun.put(i)

        self.sendevent("unscheduled", indices=stolen)

    @pytest.hookimpl
    def pytest_runtestloop(self, session):
        self.log("entering main loop")
        self.channel.setcallback(self.handle_command, endmarker=self.SHUTDOWN_MARK)
        self.nextitem_index = self.torun.get()
        while self.nextitem_index is not self.SHUTDOWN_MARK:
            self.run_one_test()
        return True

    def get_more_work(self):
        """ as the master node for more work so we know what to tear down

            pytest tears down fixtures only if the next test does not need them
            typically this is for module or package scoped fixtures.

        """
        next =  self.torun.get()
        if next is not self.SHUTDOWN_MARK:
            next_item = self.session.items[next]
        else:
            next_item = None
        return next_item, next

    def run_one_test(self):
        items = self.session.items
        self.item_index = self.nextitem_index
        item = items[self.item_index]

        worker_title("[pytest-xdist running] %s" % item.nodeid)

        start = time.time()
        self.nextitem_index = None
        #nextitem is not known till this test is complete and more work is assigned
        # so DummyNext will ask for more work when test is complete (before teardown)
        # this helps load distrobution by not assigning work until pytest needs to know
        # what is next instead of adding one extra test to each worker.
        #nextitem = items[self.item_index+1] if self.item_index+1 < len(items) else None
        next = DummyNext(self)
        try:
            self.config.hook.pytest_runtest_protocol(item=item,nextitem=next)
        except BaseException as e:
            self.sendevent("exception", exception=pickle.dumps(sys.exc_info()))
        duration = time.time() - start

        worker_title("[pytest-xdist idle]")

        self.sendevent(
            "runtest_protocol_complete", item_index=self.item_index, duration=duration
        )
        # promote the next item of work
        self.nextitem_index = next._next_index()


    def pytest_collection_modifyitems(self, session, config, items):
        # add the group name to nodeid as suffix if --dist=loadgroup
        if config.getvalue("loadgroup"):
            for item in items:
                mark = item.get_closest_marker("xdist_group")
                if not mark:
                    continue
                gname = (
                    mark.args[0]
                    if len(mark.args) > 0
                    else mark.kwargs.get("name", "default")
                )
                item._nodeid = f"{item.nodeid}@{gname}"

    @pytest.hookimpl
    def pytest_collection_finish(self, session):
        try:
             topdir = str(self.config.rootpath)
        except AttributeError:  # pytest <= 6.1.0
            topdir = str(self.config.rootdir)

        self.sendevent(
            "collectionfinish",
            topdir=topdir,
            ids=[item.nodeid for item in session.items],
        )

    @pytest.hookimpl
    def pytest_runtest_logstart(self, nodeid, location):
        self.sendevent("logstart", nodeid=nodeid, location=location)

    @pytest.hookimpl
    def pytest_runtest_logfinish(self, nodeid, location):
        self.sendevent("logfinish", nodeid=nodeid, location=location)

    @pytest.hookimpl
    def pytest_runtest_logreport(self, report):
        data = self.config.hook.pytest_report_to_serializable(
            config=self.config, report=report
        )
        data["item_index"] = self.item_index
        data["worker_id"] = self.workerid
        data["testrun_uid"] = self.testrunuid
        assert self.session.items[self.item_index].nodeid == report.nodeid
        self.sendevent("testreport", data=data)

    @pytest.hookimpl
    def pytest_collectreport(self, report):
        # send only reports that have not passed to controller as optimization (#330)
        if not report.passed:
            data = self.config.hook.pytest_report_to_serializable(
                config=self.config, report=report
            )
            self.sendevent("collectreport", data=data)

    @pytest.hookimpl
    def pytest_warning_recorded(self, warning_message, when, nodeid, location):
        self.sendevent(
            "warning_recorded",
            warning_message_data=serialize_warning_message(warning_message),
            when=when,
            nodeid=nodeid,
            location=location,
        )


def serialize_warning_message(warning_message):
    if isinstance(warning_message.message, Warning):
        message_module = type(warning_message.message).__module__
        message_class_name = type(warning_message.message).__name__
        message_str = str(warning_message.message)
        # check now if we can serialize the warning arguments (#349)
        # if not, we will just use the exception message on the controller node
        try:
            dumps(warning_message.message.args)
        except DumpError:
            message_args = None
        else:
            message_args = warning_message.message.args
    else:
        message_str = warning_message.message
        message_module = None
        message_class_name = None
        message_args = None
    if warning_message.category:
        category_module = warning_message.category.__module__
        category_class_name = warning_message.category.__name__
    else:
        category_module = None
        category_class_name = None

    result = {
        "message_str": message_str,
        "message_module": message_module,
        "message_class_name": message_class_name,
        "message_args": message_args,
        "category_module": category_module,
        "category_class_name": category_class_name,
    }
    # access private _WARNING_DETAILS because the attributes vary between Python versions
    for attr_name in warning_message._WARNING_DETAILS:
        if attr_name in ("message", "category"):
            continue
        attr = getattr(warning_message, attr_name)
        # Check if we can serialize the warning detail, marking `None` otherwise
        # Note that we need to define the attr (even as `None`) to allow deserializing
        try:
            dumps(attr)
        except DumpError:
            result[attr_name] = repr(attr)
        else:
            result[attr_name] = attr
    return result


def getinfodict():
    import platform

    return dict(
        version=sys.version,
        version_info=tuple(sys.version_info),
        sysplatform=sys.platform,
        platform=platform.platform(),
        executable=sys.executable,
        cwd=os.getcwd(),
    )


def remote_initconfig(option_dict, args):
    option_dict["plugins"].append("no:terminal")
    return Config.fromdictargs(option_dict, args)


def setup_config(config, basetemp):
    config.option.loadgroup = config.getvalue("dist") == "loadgroup"
    config.option.looponfail = False
    config.option.usepdb = False
    config.option.dist = "no"
    config.option.distload = False
    config.option.numprocesses = None
    config.option.maxprocesses = None
    config.option.basetemp = basetemp


if __name__ == "__channelexec__":
    channel = channel  # type: ignore[name-defined] # noqa: F821
    workerinput, args, option_dict, change_sys_path = channel.receive()  # type: ignore[name-defined]

    if change_sys_path is None:
        importpath = os.getcwd()
        sys.path.insert(0, importpath)
        os.environ["PYTHONPATH"] = (
            importpath + os.pathsep + os.environ.get("PYTHONPATH", "")
        )
    else:
        sys.path = change_sys_path

    os.environ["PYTEST_XDIST_TESTRUNUID"] = workerinput["testrunuid"]
    os.environ["PYTEST_XDIST_WORKER"] = workerinput["workerid"]
    os.environ["PYTEST_XDIST_WORKER_COUNT"] = str(workerinput["workercount"])

    if hasattr(Config, "InvocationParams"):
        config = _prepareconfig(args, None)
    else:
        config = remote_initconfig(option_dict, args)
        config.args = args

    setup_config(config, option_dict.get("basetemp"))
    config._parser.prog = os.path.basename(workerinput["mainargv"][0])
    config.workerinput = workerinput  # type: ignore[attr-defined]
    config.workeroutput = {}  # type: ignore[attr-defined]
    interactor = WorkerInteractor(config, channel)  # type: ignore[name-defined]
    config.hook.pytest_cmdline_main(config=config)
