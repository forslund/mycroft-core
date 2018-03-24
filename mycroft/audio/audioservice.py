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

import imp
import sys
import time
from os import listdir
from os.path import abspath, dirname, basename, isdir, join
from threading import Lock
import subprocess

from mycroft.configuration import Configuration
from mycroft.messagebus.message import Message
from mycroft.util.log import LOG


MAINMODULE = '__init__'
sys.path.append(abspath(dirname(__file__)))


def create_service_descriptor(service_folder):
    """Prepares a descriptor that can be used together with imp.

        Args:
            service_folder: folder that shall be imported.

        Returns:
            Dict with import information
    """
    info = imp.find_module(MAINMODULE, [service_folder])
    return {"name": basename(service_folder), "info": info}


def get_services(services_folder):
    """
        Load and initialize services from all subfolders.

        Args:
            services_folder: base folder to look for services in.

        Returns:
            Sorted list of audio services.
    """
    LOG.info("Loading services from " + services_folder)
    services = []
    possible_services = listdir(services_folder)
    for i in possible_services:
        location = join(services_folder, i)
        if (isdir(location) and
                not MAINMODULE + ".py" in listdir(location)):
            for j in listdir(location):
                name = join(location, j)
                if (not isdir(name) or
                        not MAINMODULE + ".py" in listdir(name)):
                    continue
                try:
                    services.append(create_service_descriptor(name))
                except Exception:
                    LOG.error('Failed to create service from ' + name,
                              exc_info=True)
        if (not isdir(location) or
                not MAINMODULE + ".py" in listdir(location)):
            continue
        try:
            services.append(create_service_descriptor(location))
        except Exception:
            LOG.error('Failed to create service from ' + location,
                      exc_info=True)
    return sorted(services, key=lambda p: p.get('name'))


def load_services(config, bus, path=None):
    """
        Search though the service directory and load any services.

        Args:
            config: configuration dict for the audio backends.
            bus: Mycroft messagebus

        Returns:
            List of started services.
    """
    if path is None:
        path = dirname(abspath(__file__)) + '/services/'
    service_directories = get_services(path)
    service = []
    for descriptor in service_directories:
        LOG.info('Loading ' + descriptor['name'])
        try:
            service_module = imp.load_module(descriptor["name"] + MAINMODULE,
                                             *descriptor["info"])
        except Exception as e:
            LOG.error('Failed to import module ' + descriptor['name'] + '\n' +
                      repr(e))
            continue

        if (hasattr(service_module, 'autodetect') and
                callable(service_module.autodetect)):
            try:
                s = service_module.autodetect(config, bus)
                service += s
            except Exception as e:
                LOG.error('Failed to autodetect. ' + repr(e))
        if hasattr(service_module, 'load_service'):
            try:
                s = service_module.load_service(config, bus)
                service += s
            except Exception as e:
                LOG.error('Failed to load service. ' + repr(e))

    return service


class AudioService(object):
    """ Audio Service class.
        Handles playback of audio and selecting proper backend for the uri
        to be played.
    """

    def __init__(self, bus):
        """
            Args:
                bus: Mycroft messagebus
        """
        self.bus = bus
        self.config = Configuration.get().get("Audio")
        self.service_lock = Lock()

        self.default = None
        self.service = []
        self.current = None
        self.volume_is_low = False
        self.volume_lock = Lock()

        # Setup control of pulse audio
        self.pulse_proc = None
        self.pulse_ducking = self.config.get('pulseaudio', False)

        bus.once('open', self.load_services_callback)

    def load_services_callback(self):
        """
            Main callback function for loading services. Sets up the globals
            service and default and registers the event handlers for the
            subsystem.
        """

        self.service = load_services(self.config, self.bus)
        # Register end of track callback
        for s in self.service:
            s.set_track_start_callback(self.track_start)

        # Find default backend
        default_name = self.config.get('default-backend', '')
        LOG.info('Finding default backend...')
        for s in self.service:
            if s.name == default_name:
                self.default = s
                LOG.info('Found ' + self.default.name)
                break
        else:
            self.default = None
            LOG.info('no default found')

        # Setup event handlers
        self.bus.on('mycroft.audio.service.play', self._play)
        self.bus.on('mycroft.audio.service.queue', self._queue)
        self.bus.on('mycroft.audio.service.pause', self._pause)
        self.bus.on('mycroft.audio.service.resume', self._resume)
        self.bus.on('mycroft.audio.service.stop', self._stop)
        self.bus.on('mycroft.audio.service.next', self._next)
        self.bus.on('mycroft.audio.service.prev', self._prev)
        self.bus.on('mycroft.audio.service.track_info', self._track_info)
        self.bus.on('recognizer_loop:audio_output_start', self._lower_volume)
        self.bus.on('recognizer_loop:record_begin', self._lower_volume)
        self.bus.on('recognizer_loop:audio_output_end', self._restore_volume)
        self.bus.on('recognizer_loop:record_end', self._restore_volume)

    def track_start(self, track):
        """
            Callback method called from the services to indicate start of
            playback of a track.
        """
        self.bus.emit(Message('mycroft.audio.playing_track',
                              data={'track': track}))

    def _pause(self, message=None):
        """
            Handler for mycroft.audio.service.pause. Pauses the current audio
            service.

            Args:
                message: message bus message, not used but required
        """
        if self.current:
            self.current.pause()

    def _resume(self, message=None):
        """
            Handler for mycroft.audio.service.resume.

            Args:
                message: message bus message, not used but required
        """
        if self.current:
            self.current.resume()

    def _next(self, message=None):
        """
            Handler for mycroft.audio.service.next. Skips current track and
            starts playing the next.

            Args:
                message: message bus message, not used but required
        """
        if self.current:
            self.current.next()

    def _prev(self, message=None):
        """
            Handler for mycroft.audio.service.prev. Starts playing the previous
            track.

            Args:
                message: message bus message, not used but required
        """
        if self.current:
            self.current.previous()

    def _stop(self, message=None):
        """
            Handler for mycroft.stop. Stops any playing service.

            Args:
                message: message bus message, not used but required
        """
        LOG.debug('stopping all playing services')
        with self.service_lock:
            if self.current:
                name = self.current.name
                if self.current.stop():
                    self.bus.emit(Message("mycroft.stop.handled",
                                          {"by": "audio:" + name}))

                self.current = None

    def _lower_volume(self, message=None):
        """
            Is triggered when mycroft starts to speak and reduces the volume.

            Args:
                message: message bus message, not used but required
        """
        with self.volume_lock:
            self.volume_is_low = True
            # if a backend is playing and ducking isn't provided by pulse
            if self.current and not self.pulse_ducking:
                LOG.debug('lowering volume')
                self.current.lower_volume()
            try:
                if self.pulse_ducking:
                    LOG.info('!!!!!!!!!!!!! DUCKING!')
                    self.pulse_duck()
            except Exception as exc:
                LOG.error(exc)

    def _restore_volume(self, message=None):
        """
            Is triggered when mycroft is done speaking and restores the volume

            Args:
                message: message bus message, not used but required
        """
        with self.volume_lock:
            # if an audio backend is active and has been lowered
            if self.current and self.volume_is_low:
                self.volume_is_low = False
                LOG.debug('restoring volume')
                self.current.restore_volume()
            if self.pulse_ducking:
                self.pulse_unduck()

    def pulse_duck(self):
        """ Trigger pulse audio ducking. """
        # Create a dummy stream to lower volume
        if not self.pulse_proc:
            self.pulse_proc = subprocess.Popen(['paplay',
                                                '--property=media.role=phone',
                                                '/dev/zero', '--raw'])

    def pulse_unduck(self):
        """ Release ducking. """
        # remove the dummy stream to restore volume
        if self.pulse_proc:
            self.pulse_proc.kill()
            self.pulse_proc.communicate()
            self.pulse_proc = None

    def play(self, tracks, prefered_service, repeat=False):
        """
            play starts playing the audio on the prefered service if it
            supports the uri. If not the next best backend is found.

            Args:
                tracks: list of tracks to play.
                repeat: should the playlist repeat
                prefered_service: indecates the service the user prefer to play
                                  the tracks.
        """
        self._stop()

        if isinstance(tracks[0], str):
            uri_type = tracks[0].split(':')[0]
        else:
            uri_type = tracks[0][0].split(':')[0]

        # check if user requested a particular service
        if prefered_service and uri_type in prefered_service.supported_uris():
            selected_service = prefered_service
        # check if default supports the uri
        elif self.default and uri_type in self.default.supported_uris():
            LOG.debug("Using default backend ({})".format(self.default.name))
            selected_service = self.default
        else:  # Check if any other service can play the media
            LOG.debug("Searching the services")
            for s in self.service:
                if uri_type in s.supported_uris():
                    LOG.debug("Service {} supports URI {}".format(s, uri_type))
                    selected_service = s
                    break
            else:
                LOG.info('No service found for uri_type: ' + uri_type)
                return
        if not selected_service.supports_mime_hints:
            tracks = [t[0] if isinstance(t, list) else t for t in tracks]
        selected_service.clear_list()
        selected_service.add_list(tracks)
        selected_service.play(repeat)
        self.current = selected_service

    def _queue(self, message):
        if self.current:
            tracks = message.data['tracks']
            self.current.add_list(tracks)
        else:
            self._play(message)

    def _play(self, message):
        """
            Handler for mycroft.audio.service.play. Starts playback of a
            tracklist. Also  determines if the user requested a special
            service.

            Args:
                message: message bus message, not used but required
        """
        tracks = message.data['tracks']
        repeat = message.data.get('repeat', False)
        # Find if the user wants to use a specific backend
        for s in self.service:
            if ('utterance' in message.data and
                    s.name in message.data['utterance']):
                prefered_service = s
                LOG.debug(s.name + ' would be prefered')
                break
        else:
            prefered_service = None
        self.play(tracks, prefered_service, repeat)

    def _track_info(self, message):
        """
            Returns track info on the message bus.

            Args:
                message: message bus message, not used but required
        """
        if self.current:
            track_info = self.current.track_info()
        else:
            track_info = {}
        self.bus.emit(Message('mycroft.audio.service.track_info_reply',
                              data=track_info))

    def shutdown(self):
        for s in self.service:
            try:
                LOG.info('shutting down ' + s.name)
                s.shutdown()
            except Exception as e:
                LOG.error('shutdown of ' + s.name + ' failed: ' + repr(e))

        # remove listeners
        self.ws.remove('mycroft.audio.service.play', self._play)
        self.ws.remove('mycroft.audio.service.queue', self._queue)
        self.ws.remove('mycroft.audio.service.pause', self._pause)
        self.ws.remove('mycroft.audio.service.resume', self._resume)
        self.ws.remove('mycroft.audio.service.stop', self._stop)
        self.ws.remove('mycroft.audio.service.next', self._next)
        self.ws.remove('mycroft.audio.service.prev', self._prev)
        self.ws.remove('mycroft.audio.service.track_info', self._track_info)
        self.ws.remove('recognizer_loop:audio_output_start',
                       self._lower_volume)
        self.ws.remove('recognizer_loop:record_begin', self._lower_volume)
        self.ws.remove('recognizer_loop:audio_output_end',
                       self._restore_volume)
        self.ws.remove('recognizer_loop:record_end', self._restore_volume)
        self.ws.remove('mycroft.stop', self._stop)
        self._restore_volume()


def main():
    """ Main function. Run when file is invoked. """
    ws = WebsocketClient()
    Configuration.init(ws)
    speech.init(ws)

    def echo(message):
        """ Echo message bus messages. """
        try:
            _message = json.loads(message)
            if 'mycroft.audio.service' not in _message.get('type'):
                return
            message = json.dumps(_message)
        except Exception as e:
            LOG.exception(e)
        LOG.debug(message)

    LOG.info("Staring Audio Services")
    ws.on('message', echo)
    audio = AudioService(ws)  # Connect audio service instance to message bus
    try:
        ws.run_forever()
    except KeyboardInterrupt as e:
        LOG.exception(e)
    finally:
        speech.shutdown()
        audio.shutdown()
        sys.exit()


if __name__ == "__main__":
    main()
