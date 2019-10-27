#!/usr/bin/python2.7
# Copyright (c) 2012-2016 Jonathan Warren
# Copyright (c) 2012-2019 The Bitmessage developers
# Distributed under the MIT/X11 software license. See the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.

# Right now, PyBitmessage only support connecting to stream 1. It doesn't
# yet contain logic to expand into further streams.

# The software version variable is now held in shared.py

import os
import sys

app_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(app_dir)
sys.path.insert(0, app_dir)


import depends
depends.check_dependencies()

import ctypes
import getopt
import multiprocessing
# Used to capture a Ctrl-C keypress so that Bitmessage can shutdown gracefully.
import signal
import socket
import threading
import time
import traceback
from struct import pack

from helper_startup import (
    isOurOperatingSystemLimitedToHavingVeryFewHalfOpenConnections
)
from singleinstance import singleinstance

import defaults
import shared
import knownnodes
import state
import shutdown
from bmconfigparser import BMConfigParser
from debug import logger  # this should go before any threads
from inventory import Inventory
# Network objects and threads
from network import (
    BMConnectionPool, Dandelion,
    AddrThread, AnnounceThread, BMNetworkThread, InvThread, ReceiveQueueThread,
    DownloadThread, UploadThread)
# Synchronous threads
from threads import (
    set_thread_name,
    addressGenerator, objectProcessor, singleCleaner, singleWorker, sqlThread)


def connectToStream(streamNumber):
    state.streamsInWhichIAmParticipating.append(streamNumber)

    if isOurOperatingSystemLimitedToHavingVeryFewHalfOpenConnections():
        # Some XP and Vista systems can only have 10 outgoing connections
        # at a time.
        state.maximumNumberOfHalfOpenConnections = 9
    else:
        state.maximumNumberOfHalfOpenConnections = 64
    try:
        # don't overload Tor
        if BMConfigParser().get(
                'bitmessagesettings', 'socksproxytype') != 'none':
            state.maximumNumberOfHalfOpenConnections = 4
    except:
        pass

    with knownnodes.knownNodesLock:
        if streamNumber not in knownnodes.knownNodes:
            knownnodes.knownNodes[streamNumber] = {}
        if streamNumber * 2 not in knownnodes.knownNodes:
            knownnodes.knownNodes[streamNumber * 2] = {}
        if streamNumber * 2 + 1 not in knownnodes.knownNodes:
            knownnodes.knownNodes[streamNumber * 2 + 1] = {}

    BMConnectionPool().connectToStream(streamNumber)


def _fixSocket():
    if sys.platform.startswith('linux'):
        socket.SO_BINDTODEVICE = 25

    if not sys.platform.startswith('win'):
        return

    # Python 2 on Windows doesn't define a wrapper for
    # socket.inet_ntop but we can make one ourselves using ctypes
    if not hasattr(socket, 'inet_ntop'):
        addressToString = ctypes.windll.ws2_32.WSAAddressToStringA

        def inet_ntop(family, host):
            if family == socket.AF_INET:
                if len(host) != 4:
                    raise ValueError("invalid IPv4 host")
                host = pack("hH4s8s", socket.AF_INET, 0, host, "\0" * 8)
            elif family == socket.AF_INET6:
                if len(host) != 16:
                    raise ValueError("invalid IPv6 host")
                host = pack("hHL16sL", socket.AF_INET6, 0, 0, host, 0)
            else:
                raise ValueError("invalid address family")
            buf = "\0" * 64
            lengthBuf = pack("I", len(buf))
            addressToString(host, len(host), None, buf, lengthBuf)
            return buf[0:buf.index("\0")]
        socket.inet_ntop = inet_ntop

    # Same for inet_pton
    if not hasattr(socket, 'inet_pton'):
        stringToAddress = ctypes.windll.ws2_32.WSAStringToAddressA

        def inet_pton(family, host):
            buf = "\0" * 28
            lengthBuf = pack("I", len(buf))
            if stringToAddress(str(host),
                               int(family),
                               None,
                               buf,
                               lengthBuf) != 0:
                raise socket.error("illegal IP address passed to inet_pton")
            if family == socket.AF_INET:
                return buf[4:8]
            elif family == socket.AF_INET6:
                return buf[8:24]
            else:
                raise ValueError("invalid address family")
        socket.inet_pton = inet_pton

    # These sockopts are needed on for IPv6 support
    if not hasattr(socket, 'IPPROTO_IPV6'):
        socket.IPPROTO_IPV6 = 41
    if not hasattr(socket, 'IPV6_V6ONLY'):
        socket.IPV6_V6ONLY = 27


def signal_handler(signum, frame):
    """Single handler for any signal sent to pybitmessage"""
    process = multiprocessing.current_process()
    thread = threading.current_thread()
    logger.error(
        'Got signal %i in %s/%s',
        signum, process.name, thread.name
    )
    if process.name == "RegExParser":
        # on Windows this isn't triggered, but it's fine,
        # it has its own process termination thing
        raise SystemExit
    if "PoolWorker" in process.name:
        raise SystemExit
    if thread.name not in ("PyBitmessage", "MainThread"):
        return
    logger.error("Got signal %i", signum)
    # there are possible non-UI variants to run bitmessage which should shutdown
    # especially test-mode
    if shared.thisapp.daemon or not state.enableGUI:
        shutdown.doCleanShutdown()
    else:
        print('# Thread: %s(%d)' % (thread.name, thread.ident))
        for filename, lineno, name, line in traceback.extract_stack(frame):
            print('File: "%s", line %d, in %s' % (filename, lineno, name))
            if line:
                print('  %s' % line.strip())
        print('Unfortunately you cannot use Ctrl+C when running the UI'
              ' because the UI captures the signal.')


class Main:
    @staticmethod
    def start_proxyconfig(config):
        """Check socksproxytype and start any proxy configuration plugin"""
        proxy_type = config.safeGet('bitmessagesettings', 'socksproxytype')
        if proxy_type not in ('none', 'SOCKS4a', 'SOCKS5'):
            # pylint: disable=relative-import
            from plugins.plugin import get_plugin
            try:
                proxyconfig_start = time.time()
                if not get_plugin('proxyconfig', name=proxy_type)(config):
                    raise TypeError
            except TypeError:
                logger.error(
                    'Failed to run proxy config plugin %s',
                    proxy_type, exc_info=True)
                shutdown.doCleanShutdown()
                sys.exit(2)
            else:
                logger.info(
                    'Started proxy config plugin %s in %s sec',
                    proxy_type, time.time() - proxyconfig_start)

    def start(self):
        _fixSocket()

        config = BMConfigParser()
        daemon = config.safeGetBoolean('bitmessagesettings', 'daemon')

        try:
            opts, args = getopt.getopt(
                sys.argv[1:], "hcdt",
                ["help", "curses", "daemon", "test"])

        except getopt.GetoptError:
            self.usage()
            sys.exit(2)

        for opt, arg in opts:
            if opt in ("-h", "--help"):
                self.usage()
                sys.exit()
            elif opt in ("-d", "--daemon"):
                daemon = True
            elif opt in ("-c", "--curses"):
                state.curses = True
            elif opt in ("-t", "--test"):
                state.testmode = True
                if os.path.isfile(os.path.join(
                        state.appdata, 'unittest.lock')):
                    daemon = True
                state.enableGUI = False  # run without a UI
                # Fallback: in case when no api command was issued
                state.last_api_response = time.time()
                # Apply special settings
                config.set(
                    'bitmessagesettings', 'apienabled', 'true')
                config.set(
                    'bitmessagesettings', 'apiusername', 'username')
                config.set(
                    'bitmessagesettings', 'apipassword', 'password')
                config.set(
                    'bitmessagesettings', 'apinotifypath',
                    os.path.join(app_dir, 'tests', 'apinotify_handler.py')
                )

        if daemon:
            state.enableGUI = False  # run without a UI

        if state.enableGUI and not state.curses and not depends.check_pyqt():
            sys.exit(
                'PyBitmessage requires PyQt unless you want'
                ' to run it as a daemon and interact with it'
                ' using the API. You can download PyQt from '
                'http://www.riverbankcomputing.com/software/pyqt/download'
                ' or by searching Google for \'PyQt Download\'.'
                ' If you want to run in daemon mode, see '
                'https://bitmessage.org/wiki/Daemon\n'
                'You can also run PyBitmessage with'
                ' the new curses interface by providing'
                ' \'-c\' as a commandline argument.'
            )
        # is the application already running?  If yes then exit.
        shared.thisapp = singleinstance("", daemon)

        if daemon:
            with shared.printLock:
                print('Running as a daemon. Send TERM signal to end.')
            self.daemonize()

        self.setSignalHandler()

        set_thread_name("PyBitmessage")

        state.dandelion = config.safeGetInt('network', 'dandelion')
        # dandelion requires outbound connections, without them,
        # stem objects will get stuck forever
        if state.dandelion and not config.safeGetBoolean(
                'bitmessagesettings', 'sendoutgoingconnections'):
            state.dandelion = 0

        if state.testmode or config.safeGetBoolean(
                'bitmessagesettings', 'extralowdifficulty'):
            defaults.networkDefaultProofOfWorkNonceTrialsPerByte = int(
                defaults.networkDefaultProofOfWorkNonceTrialsPerByte / 100)
            defaults.networkDefaultPayloadLengthExtraBytes = int(
                defaults.networkDefaultPayloadLengthExtraBytes / 100)

        knownnodes.readKnownNodes()

        # Not needed if objproc is disabled
        if state.enableObjProc:

            # Start the address generation thread
            addressGeneratorThread = addressGenerator()
            # close the main program even if there are threads left
            addressGeneratorThread.daemon = True
            addressGeneratorThread.start()

            # Start the thread that calculates POWs
            singleWorkerThread = singleWorker()
            # close the main program even if there are threads left
            singleWorkerThread.daemon = True
            singleWorkerThread.start()

        # Start the SQL thread
        sqlLookup = sqlThread()
        # DON'T close the main program even if there are threads left.
        # The closeEvent should command this thread to exit gracefully.
        sqlLookup.daemon = False
        sqlLookup.start()

        Inventory()  # init
        # init, needs to be early because other thread may access it early
        Dandelion()

        # Enable object processor and SMTP only if objproc enabled
        if state.enableObjProc:

            # SMTP delivery thread
            if daemon and config.safeGet(
                    'bitmessagesettings', 'smtpdeliver', '') != '':
                from class_smtpDeliver import smtpDeliver
                smtpDeliveryThread = smtpDeliver()
                smtpDeliveryThread.start()

            # SMTP daemon thread
            if daemon and config.safeGetBoolean(
                    'bitmessagesettings', 'smtpd'):
                from class_smtpServer import smtpServer
                smtpServerThread = smtpServer()
                smtpServerThread.start()

            # Start the thread that calculates POWs
            objectProcessorThread = objectProcessor()
            # DON'T close the main program even the thread remains.
            # This thread checks the shutdown variable after processing
            # each object.
            objectProcessorThread.daemon = False
            objectProcessorThread.start()

        # Start the cleanerThread
        singleCleanerThread = singleCleaner()
        # close the main program even if there are threads left
        singleCleanerThread.daemon = True
        singleCleanerThread.start()

        # Not needed if objproc disabled
        if state.enableObjProc:
            shared.reloadMyAddressHashes()
            shared.reloadBroadcastSendersForWhichImWatching()

            # API is also objproc dependent
            if config.safeGetBoolean('bitmessagesettings', 'apienabled'):
                import api  # pylint: disable=relative-import
                singleAPIThread = api.singleAPI()
                # close the main program even if there are threads left
                singleAPIThread.daemon = True
                singleAPIThread.start()

        # start network components if networking is enabled
        if state.enableNetwork:
            self.start_proxyconfig(config)
            BMConnectionPool()
            asyncoreThread = BMNetworkThread()
            asyncoreThread.daemon = True
            asyncoreThread.start()
            for i in range(config.getint('threads', 'receive')):
                receiveQueueThread = ReceiveQueueThread(i)
                receiveQueueThread.daemon = True
                receiveQueueThread.start()
            announceThread = AnnounceThread()
            announceThread.daemon = True
            announceThread.start()
            state.invThread = InvThread()
            state.invThread.daemon = True
            state.invThread.start()
            state.addrThread = AddrThread()
            state.addrThread.daemon = True
            state.addrThread.start()
            state.downloadThread = DownloadThread()
            state.downloadThread.daemon = True
            state.downloadThread.start()
            state.uploadThread = UploadThread()
            state.uploadThread.daemon = True
            state.uploadThread.start()

            connectToStream(1)

            if config.safeGetBoolean('bitmessagesettings', 'upnp'):
                import upnp
                upnpThread = upnp.uPnPThread()
                upnpThread.start()
        else:
            # Populate with hardcoded value (same as connectToStream above)
            state.streamsInWhichIAmParticipating.append(1)

        if not daemon and state.enableGUI:
            if state.curses:
                if not depends.check_curses():
                    sys.exit()
                print('Running with curses')
                import bitmessagecurses
                bitmessagecurses.runwrapper()
            elif state.kivy:
                config.remove_option('bitmessagesettings', 'dontconnect')
                from bitmessagekivy.mpybit import NavigateApp
                NavigateApp().run()
            else:
                import bitmessageqt
                bitmessageqt.run()
        else:
            config.remove_option('bitmessagesettings', 'dontconnect')

        if daemon:
            while state.shutdown == 0:
                time.sleep(1)
                if (
                        state.testmode and time.time() - state.last_api_response >= 30):
                    self.stop()
        elif not state.enableGUI:
            from tests import core as test_core  # pylint: disable=relative-import
            test_core_result = test_core.run(self)
            state.enableGUI = True
            self.stop()
            test_core.cleanup()
            sys.exit(
                'Core tests failed!'
                if test_core_result.errors or test_core_result.failures
                else 0
            )

    def daemonize(self):
        grandfatherPid = os.getpid()
        parentPid = None
        try:
            if os.fork():
                # unlock
                shared.thisapp.cleanup()
                # wait until grandchild ready
                while True:
                    time.sleep(1)
                os._exit(0)
        except AttributeError:
            # fork not implemented
            pass
        else:
            parentPid = os.getpid()
            shared.thisapp.lock()  # relock

        os.umask(0)
        try:
            os.setsid()
        except AttributeError:
            # setsid not implemented
            pass
        try:
            if os.fork():
                # unlock
                shared.thisapp.cleanup()
                # wait until child ready
                while True:
                    time.sleep(1)
                os._exit(0)
        except AttributeError:
            # fork not implemented
            pass
        else:
            shared.thisapp.lock()  # relock
        shared.thisapp.lockPid = None  # indicate we're the final child
        sys.stdout.flush()
        sys.stderr.flush()
        if not sys.platform.startswith('win'):
            si = file(os.devnull, 'r')
            so = file(os.devnull, 'a+')
            se = file(os.devnull, 'a+', 0)
            os.dup2(si.fileno(), sys.stdin.fileno())
            os.dup2(so.fileno(), sys.stdout.fileno())
            os.dup2(se.fileno(), sys.stderr.fileno())
        if parentPid:
            # signal ready
            os.kill(parentPid, signal.SIGTERM)
            os.kill(grandfatherPid, signal.SIGTERM)

    def setSignalHandler(self):
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        # signal.signal(signal.SIGINT, signal.SIG_DFL)

    def usage(self):
        print 'Usage: ' + sys.argv[0] + ' [OPTIONS]'
        print '''
Options:
  -h, --help            show this help message and exit
  -c, --curses          use curses (text mode) interface
  -d, --daemon          run in daemon (background) mode
  -t, --test            dryrun, make testing

All parameters are optional.
'''

    def stop(self):
        with shared.printLock:
            print('Stopping Bitmessage Deamon.')
        shutdown.doCleanShutdown()

    # TODO: nice function but no one is using this
    def getApiAddress(self):
        if not BMConfigParser().safeGetBoolean(
                'bitmessagesettings', 'apienabled'):
            return None
        address = BMConfigParser().get('bitmessagesettings', 'apiinterface')
        port = BMConfigParser().getint('bitmessagesettings', 'apiport')
        return {'address': address, 'port': port}


def main():
    mainprogram = Main()
    mainprogram.start()


if __name__ == "__main__":
    main()


# So far, the creation of and management of the Bitmessage protocol and this
# client is a one-man operation. Bitcoin tips are quite appreciated.
# 1H5XaDA6fYENLbknwZyjiYXYPQaFjjLX2u
