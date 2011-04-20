#!/usr/bin/env python

"""
@file itv_trial
@author Dave Foster <dfoster@asascience.com>
@brief Integration testing with trial.

itv_trial is designed to be a lightweight integration testing framework for
projects based on ION.  The goal is to be able to use the same tests, via trial,
to do integration testing on a CEI bootstrapped system running in a cloud environment,
and a local system where app_dependencies your tests require are run in separate 
capability containers.

To use, derive your test from ion.test.ItvTestCase and fill in the app_dependencies class
attribute with a list of apps your test needs. Apps are relative to the current working
directory and typically reside in the res/apps subdir of ioncore-python.

Entries in the "app_dependencies" class array may be strings pointing to the apps themselves, or
tuples, the first being the string to the app and the second being arguments to pass on
the command line, intended to be used by the app_dependencies themselves. Some samples:

    # starts a single attribute store app
    app_dependencies = ["res/apps/attributestore.app"]

    # starts two attribute store apps
    app_dependencies = [("res/apps/attributestore.app, "id=1")      # id is not used by attributestore but is used
                ("res/apps/attributestore.app, "id=2")]     #   to differentiate the two attributestore
                                                            #   app_dependencies here.

Example:

    class AttributeStoreTest(ItvTestCase):
        app_dependencies = ["res/apps/attributestore.app"]  # start these apps prior to testing.

        @defer.inlineCallbacks
        def setUp(self):
            yield self._start_container()

        @defer.inlineCallbacks
        def tearDown(self):
            yield self._stop_container()

        @defer.inlineCallbacks
        def test_set_attr(self):
            asc = AttributeStoreClient()
            yield asc.put("hi", "hellothere")

            res = yield asc.get("hi")
            self.failUnless(res == "hellothere")

        @defer.inlineCallbacks
        def test_set_attr2(self):
            # "hi" is still set here, but only if test_set_attr is run first, be careful
            asc = AttributeStoreClient()
            res = yield asc.get("hi")
            self.failUnless(res == "hellothere")

Important points:
- The sysname parameter is required to get all the app_dependencies and tests running on the same
  system. itv_trial takes care of this for you, but if you want to deploy these tests vs 
  a CEI spawned environment, you must set the environment variable ION_TEST_CASE_SYSNAME
  to be the same as the sysname the CEI environment was spawned with.
"""

import os, tempfile, signal, time
from twisted.trial.runner import TestLoader
from twisted.trial.unittest import TestSuite
from uuid import uuid4
import subprocess
import optparse

def gen_sysname():
    return str(uuid4())[:6]     # gen uuid, use at most 6 chars

def get_opts():
    """
    Get command line options.
    Sets up option parser, calls gen_sysname to create a new sysname for defaults.
    """
    p = optparse.OptionParser()

    p.add_option("--sysname",   action="store",     dest="sysname", help="Use this sysname for CCs/trial. If not specified, one is automatically generated.")
    p.add_option("--hostname",  action="store",     dest="hostname",help="Connect to the broker at this hostname. If not specified, uses localhost.")
    p.add_option("--merge",     action="store_true",dest="merge",   help="Merge the environment for all integration tests and run them in one shot.")
    p.add_option("--debug",     action="store_true",dest="debug",   help="Prints verbose debugging messages.")
    p.add_option("--debug-cc",  action="store_true",dest="debug_cc",help="If specified, instead of running trial, drops you into a CC shell after starting apps.")

    p.set_defaults(sysname=gen_sysname(), hostname="localhost", debug=False, debug_cc=False)  # make up a new random sysname
    return p.parse_args()

def get_test_classes(testargs, debug=False):
    """
    Gets a set of test classes that will be run.
    Uses the same parsing loader that trial does (which we eventually run).
    """
    totalsuite = TestLoader().loadByNames(testargs, True)
    all_testclasses = set()

    def walksuite(suite, res):
        for x in suite:
            if not isinstance(x, TestSuite):
                if debug:
                    print "Adding to test suites", x.__class__

                res.add(x.__class__)
            else:
                walksuite(x, res)

    walksuite(totalsuite, all_testclasses)

    return all_testclasses

def build_twistd_args(service, serviceargs, opts, shell=False):
    """
    Returns an array suitable for spawning a twistd cc container.
    """
    # build extraargs
    extraargs = "sysname=%s" % opts.sysname
    if len(serviceargs) > 0:
        extraargs += "," + serviceargs

    # temporary log/pid path
    tf = os.path.join(tempfile.gettempdir(), "cc-" + str(uuid4()))

    # build command line
    sargs = ["bin/twistd", "-n", "--pidfile", tf + ".pid", "--logfile", tf + ".log", "cc", "-h", opts.hostname]
    if not shell:
        sargs.append("-n")
    sargs.append("-a")
    sargs.append(extraargs)
    if service != "":
        sargs.append(service)

    return sargs

def main():
    opts, args = get_opts()
    all_testclasses = get_test_classes(args, opts.debug)

    if opts.merge:
        # merge all tests into one set
        testset = [all_testclasses]
    else:
        # split out each test on its own
        testset = [[x] for x in all_testclasses]

    for testclass in testset:
        app_dependencies = {}
        for x in testclass:
            print str(x), "%s.%s" % (x.__module__, x.__name__)
            if hasattr(x, 'app_dependencies'):
                for y in x.app_dependencies:

                    # if not specified as a (appfile, args) tuple, make it one
                    if not isinstance(y, tuple):
                        y = (y, None)

                    if not app_dependencies.has_key(y):
                        app_dependencies[y] = []

                    app_dependencies[y].append(x)

        if len(app_dependencies) > 0:
            print "The following app_dependencies will be started:"
            for service in app_dependencies.keys():
                extra = "(%s)" % ",".join([tc.__name__ for tc in app_dependencies[service]])
                print "\t", service, extra

            print "Pausing before starting..."
            time.sleep(15)

        ccs = []
        for service in app_dependencies.keys():

            # build serviceargs to pass to service (should be param=value pairs as strings)
            serviceargs=""
            if service[1]:
                params = service[1]
                if not isinstance(params, list):
                    params = [params]
                serviceargs = ",".join(params)

            # build command line
            sargs = build_twistd_args(service[0], serviceargs, opts)

            if opts.debug:
                print sargs

            # set alternate logging conf to just go to stdout
            newenv = os.environ.copy()
            newenv['ION_ALTERNATE_LOGGING_CONF'] = 'res/logging/ionlogging_stdout.conf'

            # spawn container
            po = subprocess.Popen(sargs, env=newenv)

            # add to list of open containers
            ccs.append(po)

        if len(app_dependencies) > 0:
            print "Waiting for containers to spin up..."
            time.sleep(5)

        # relay signals to trial process we're waiting for
        def handle_signal(signum, frame):
            os.kill(trialpid, signum)

        trialpid = os.fork()
        if trialpid != 0:
            print "CHILD PID IS ", trialpid
            # PARENT PROCESS: this script

            # set new signal handlers to relay signals into trial
            oldterm = signal.signal(signal.SIGTERM, handle_signal)
            #oldkill = signal.signal(signal.SIGKILL, handle_signal)
            oldint  = signal.signal(signal.SIGINT, handle_signal)

            # wait on trial
            try:
                os.waitpid(trialpid, 0)
            except OSError:
                pass

            # restore old signal handlers
            signal.signal(signal.SIGTERM, oldterm)
            #signal.signal(signal.SIGKILL, oldkill)
            signal.signal(signal.SIGINT, oldint)
        else:
            # NEW CHILD PROCESS: spawn trial, exec into nothingness
            newenv = os.environ.copy()
            newenv['ION_ALTERNATE_LOGGING_CONF'] = 'res/logging/ionlogging_stdout.conf'
            newenv["ION_TEST_CASE_SYSNAME"] = opts.sysname
            newenv["ION_TEST_CASE_BROKER_HOST"] = opts.hostname
            if not opts.debug_cc:
                trialargs = ["%s.%s" % (x.__module__, x.__name__) for x in testclass]

                os.execve("bin/trial", ["bin/trial"] + trialargs, newenv)
            else:
                # spawn an interactive twistd shell into this system
                print "DEBUG_CC:"
                sargs = build_twistd_args("", "", opts, True)
                os.execve("bin/twistd", sargs, newenv)

        def cleanup():
            print "Cleaning up app_dependencies..."
            for cc in ccs:
                print "\tClosing container with pid:", cc.pid
                os.kill(cc.pid, signal.SIGTERM)

        cleanup()

if __name__ == "__main__":
    main()