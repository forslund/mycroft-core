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
import json
import sys
import traceback

import tornado.websocket
from tornado.log import gen_log
from pyee import EventEmitter

from mycroft.messagebus.message import Message
from mycroft.filesystem import FileSystemAccess
from mycroft.util.log import LOG


EventBusEmitter = EventEmitter()

client_connections = []

def read_tokens():
    fs = FileSystemAccess('./')
    tokens = []
    with fs.open('token', 'r') as t:
        for line in t:
            tokens.append(line.strip())
    return tokens


class AuthWebSocketProtocol(tornado.websocket.WebSocketProtocol13):
    def check_auth(self):
        LOG.info('CHECK AUTH')
        tokens = read_tokens()
        auth = self.handler.request.headers.get('Authorization', '')
        auth = auth.replace('BEARER ', '')
        LOG.info(auth)
        LOG.info(tokens)
        return auth in tokens

    def accept_connection(self):
        try:
            self._handle_websocket_headers()
        except ValueError:
            self.handler.set_status(400)
            log_msg = "Missing/Invalid WebSocket headers"
            self.handler.finish(log_msg)
            gen_log.debug(log_msg)
            return

        if not self.check_auth():
            self.handler.set_status(403)
            log_msg = "Invalid auth"
            self.handler.finish(log_msg)
            gen_log.debug(log_msg)
            return
        try:
            self._accept_connection()
        except ValueError:
            gen_log.debug("Malformed WebSocket request received",
                          exc_info=True)
            self._abort()
            return


class WebsocketEventHandler(tornado.websocket.WebSocketHandler):
    def __init__(self, application, request, **kwargs):
        tornado.websocket.WebSocketHandler.__init__(
            self, application, request, **kwargs)
        self.emitter = EventBusEmitter

    def on(self, event_name, handler):
        self.emitter.on(event_name, handler)

    def on_message(self, message):
        if self not in client_connections:
            return
        LOG.debug(message)
        try:
            deserialized_message = Message.deserialize(message)
        except:
            return

        try:
            self.emitter.emit(deserialized_message.type, deserialized_message)
        except Exception as e:
            LOG.exception(e)
            traceback.print_exc(file=sys.stdout)
            pass

        for client in client_connections:
            client.write_message(message)

    def get_websocket_protocol(self):
        websocket_version = self.request.headers.get("Sec-WebSocket-Version")
        if websocket_version in ("7", "8", "13"):
            return AuthWebSocketProtocol(
                self, compression_options=self.get_compression_options())

    def open(self):
        self.write_message(Message("connected").serialize())
        client_connections.append(self)

    def on_close(self):
        LOG.info('CLOSE')
        if self in client_connections:
            client_connections.remove(self)

    def emit(self, channel_message):
        if (hasattr(channel_message, 'serialize') and
                callable(getattr(channel_message, 'serialize'))):
            self.write_message(channel_message.serialize())
        else:
            self.write_message(json.dumps(channel_message))

    def check_origin(self, origin):
        return True
