# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Daemon launched at startup to handle skill activities.

In this repo, you will not find an entry called mycroft-skills in the bin
directory.  The executable gets added to the bin directory when installed
(see setup.py)
"""
import time
from threading import Event
import mycroft.lock
from mycroft import dialog
from mycroft.api import is_paired, BackendDown, DeviceApi
from mycroft.enclosure.api import EnclosureAPI
from mycroft.configuration import Configuration
from mycroft.messagebus.client import MessageBusClient
from mycroft.messagebus.message import Message
from mycroft.util import (
    connected, wait_while_speaking, reset_sigint_handler,
    create_echo_function, wait_for_exit_signal
)
from mycroft.util.log import LOG
from mycroft.util.lang import set_active_lang

from .skill_manager import SkillManager, MsmException
from .core import FallbackSkill
from .event_scheduler import EventScheduler
from .intent_service import IntentService
from .padatious_service import PadatiousService

bus = None  # Mycroft messagebus reference, see "mycroft.messagebus"

# Remember "now" at startup.  Used to detect clock changes.
start_ticks = time.monotonic()
start_clock = time.time()


ONE_HOUR = 60 * 60  # Seconds


def speak_dialog(dialog, data=None, wait=False):
    """Speak dialog file or sentence.

    Arguments:
        dialog (str): dialog file or string to speak
        data (dict): dictionary of data to insert into dialog
        wait (bool): wait for utterance to complete. Default False
    """
    data = {'utterance': dialog.get(dialog)}
    bus.emit(Message("speak", data))
    if wait:
        wait_while_speaking()


def trigger_pairing():
    """Send a "pair my device" utterance to trigger pairing."""
    payload = {
        'utterances': ["pair my device"],
        'lang': "en-us"
    }
    bus.emit(Message("recognizer_loop:utterance", payload))


class SystemInterface:
    """Class for interacting with the system.

    The object instance communicates  with system tools such as the
    mycroft-admin-service and ntp to get information and perform actions.

    Arguments:
        bus: Mycroft bus client
    """
    def __init__(self, bus):
        self.bus = bus

    def perform_ntp_sync(self):
        """Send message to service handling ntp to try to sync clock."""
        bus.wait_for_response(Message('system.ntp.sync'),
                              'system.ntp.sync.complete', 15)

    def clock_is_skewed(self):
        """Check if the time skewed significantly."""
        skew = abs((time.monotonic() - start_ticks) -
                   (time.time() - start_clock))
        return skew > ONE_HOUR

    def update_system(self, platform, pairing_status):
        """Send a system update message.

        Arguments:
            platform (str): the mycroft platform
            pairing_status (bool): True if system is already paired

        Returns:
            True if update started
        """
        self.bus.emit(Message('system.update'))
        msg = Message('system.update', {
            'paired': pairing_status,
            'platform': platform
        })
        resp = self.bus.wait_for_response(msg, 'system.update.processing')

        if resp and (resp.data or {}).get('processing', True):
            self.bus.wait_for_response(Message('system.update.waiting'),
                                       'system.update.complete', 1000)
            return True
        else:
            return False

    def reboot(self):
        """Send a reboot instruction to the relevant handler.

        TODO: the visual indicators here is tied heavily to the Mark-1 and
        could possibly be moved into the mycroft-admin-service.
        """
        enclosure = EnclosureAPI(self.bus)
        # provide visual indicators of the reboot
        enclosure.mouth_text(dialog.get("message_rebooting"))
        enclosure.eyes_color(70, 65, 69)  # soft gray
        enclosure.eyes_spin()
        # give the system time to finish processing enclosure messages
        time.sleep(1.0)
        self.bus.emit(Message("system.reboot"))


def register_intent_services(bus):
    """Start up the all intent services and connect them as needed.

    Arguments:
        bus: messagebus client to register the services on
    """
    service = IntentService(bus)
    try:
        PadatiousService(bus, service)
    except Exception as e:
        LOG.exception('Failed to create padatious handlers '
                      '({})'.format(repr(e)))

    # Register handler to trigger fallback system
    bus.on('intent_failure', FallbackSkill.make_intent_failure_handler(bus))


def pre_connection_startup(bus):
    """Perform startup actions not requiring network connection.

    Returns:
        tuple with references to started services
    """
    register_intent_services(bus)
    event_scheduler = EventScheduler(bus)

    # Create a thread that monitors the loaded skills, looking for updates
    try:
        skill_manager = SkillManager(bus)
        skill_manager.load_priority()
    except MsmException:
        # skill manager couldn't be created, wait for network connection and
        # retry
        skill_manager = None
        LOG.info('Msm is uninitialized and requires network connection ',
                 'to fetch skill information\n'
                 'Will retry to create after internet connection is ',
                 'established.')
    return skill_manager, event_scheduler


def post_connection_startup(bus, skill_manager):
    """Perform startup actions requiring network connection.

    Arguments:
        bus: message bus to operate on
        skill_manager: inited skill manager or None if pre connection
                       startup failed

    This includes
    - reinitialize skill_manager if needed
    - update ntp, and reboot if the skew is more than 1 hour
    - trigger system update
    - start the skills manager
    """
    system = SystemInterface(bus)
    enclosure = EnclosureAPI(bus)
    config = Configuration.get()
    platform = config['enclosure'].get("platform", "unknown")

    if skill_manager is None:
        # msm wasn't initialized and skill_manager couldn't be started
        # retry startup with internet connection.
        skill_manager = SkillManager(bus)
        skill_manager.load_priority()

    try:
        paired = is_paired()
    except BackendDown:
        speak_dialog('backend.down')
        paired = True  # Consider paired for the time being

    if platform in ['mycroft_mark_1', 'picroft', 'mycroft_mark_2pi']:
        system.perform_ntp_sync()

        if paired:
            enclosure.mouth_text(dialog.get("message_synching.clock"))
    if not paired:
        # If an update is needed do not proceed with skill loading
        # and pairing
        if system.update_system(platform, is_paired):
            # If an update is needed do not proceed
            return

    if system.clock_is_skewed():
        speak_dialog('time.changed.reboot', wait=True)
        system.reboot()
        return

    # Start skill management
    skill_manager.start()

    bus.emit(Message('mycroft.internet.connected'))

    # check for pairing, if not automatically start pairing
    if not paired:
        trigger_pairing()
    else:
        # Let backend know the current version of this device
        DeviceApi().update_version()


def start_bus(bus):
    """Start the bus client daemon and wait for connection.

    Arguments:
        bus: messagebus client to start
    """
    bus_connected = Event()

    def set_connected():
        nonlocal bus_connected
        bus_connected.set()

    bus.on('message', create_echo_function('SKILLS'))
    # Call set_connected function when connection is established
    bus.once('open', set_connected)
    bus.run_as_daemon()

    # Wait for connection
    bus_connected.wait()
    LOG.info('Connected to messagebus')


def main():
    """Start up skills and skill related services.

    - Setup process signal handling
    - Prevent multiple instances of the skill
    - Connects to messagebus
    - Set default language
    - Starts:
     - SkillManager to load/reloading of skills when needed
     - adapt intent service
     - padatious intent service
    """
    global bus
    reset_sigint_handler()
    # Create PID file, prevent multiple instances of this service
    mycroft.lock.Lock('skills')
    # Connect this Skill management process to the Mycroft Messagebus
    bus = MessageBusClient()
    Configuration.init(bus)
    config = Configuration.get()
    # Set the active lang to match the configured one
    set_active_lang(config.get('lang', 'en-us'))

    # Connect to the messagebus
    start_bus(bus)

    # Startup services
    skill_manager, event_scheduler = pre_connection_startup(bus)
    while not connected():
        time.sleep(1)
    post_connection_startup(bus, skill_manager)
    LOG.info('Skill process started')

    # Wait for SIGINT
    wait_for_exit_signal()
    shutdown(event_scheduler, skill_manager)


def shutdown(event_scheduler, skill_manager):
    """Shutdown skills and event scheduler cleanly."""
    if event_scheduler:
        LOG.debug('Shutting down event scheduler')
        event_scheduler.shutdown()

    # Terminate all running threads that update skills
    if skill_manager:
        LOG.debug('Shutting down skill manager')
        skill_manager.stop()
        skill_manager.join()


if __name__ == "__main__":
    main()
