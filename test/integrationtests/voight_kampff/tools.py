import time

from mycroft.messagebus import Message


SLEEP_LENGTH = 0.25
TIMEOUT = 10


def emit_utterance(bus, utt):
    """Emit an utterance on the bus.

    Arguments:
        bus (InterceptAllBusClient): Bus instance to listen on
        dialogs (list): list of acceptable dialogs
    """
    bus.emit(Message('recognizer_loop:utterance',
                     data={'utterances': [utt],
                           'lang': 'en-us',
                           'session': '',
                           'ident': time.time()},
                     context={'client_name': 'mycroft_listener'}))


def wait_for_dialog(bus, dialogs, timeout=TIMEOUT):
    """Wait for one of the dialogs given as argument.

    Arguments:
        bus (InterceptAllBusClient): Bus instance to listen on
        dialogs (list): list of acceptable dialogs
        timeout (int): how long to wait for the messagem, defaults to 10 sec.
    """
    for t in range(int(timeout * (1 / SLEEP_LENGTH))):
        for message in bus.get_messages('speak'):
            dialog = message.data.get('meta', {}).get('dialog')
            if dialog in dialogs:
                bus.clear_messages()
                return
        time.sleep(SLEEP_LENGTH)
    bus.clear_messages()
