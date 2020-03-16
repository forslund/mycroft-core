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
from os.path import join, exists, basename
from glob import glob
import re
import time

from behave import given, when, then

from mycroft.messagebus import Message
from mycroft.audio import wait_while_speaking


TIMEOUT = 10
SLEEP_LENGTH = 0.25


def find_dialog(skill_path, dialog):
    """Check the usual location for dialogs.

    TODO: subfolders
    """
    if exists(join(skill_path, 'dialog')):
        return join(skill_path, 'dialog', 'en-us', dialog)
    else:
        return join(skill_path, 'locale', 'en-us', dialog)


def load_dialog_file(dialog_path):
    """Load dialog files and get the contents."""
    with open(dialog_path) as f:
        lines = f.readlines()
    return [l.strip().lower() for l in lines
            if l.strip() != '' and l.strip()[0] != '#']


def load_dialog_list(skill_path, dialog):
    """Load dialog from files into a single list.

    Arguments:
        skill (MycroftSkill): skill to load dialog from
        dialog (list): Dialog names (str) to load

    Returns:
        tuple (list of Expanded dialog strings, debug string)
    """
    dialog_path = find_dialog(skill_path, dialog)

    debug = 'Opening {}\n'.format(dialog_path)
    return load_dialog_file(dialog_path), debug


def dialog_from_sentence(sentence, skill_path):
    """Find dialog file from example sentence."""
    dialog_paths = join(skill_path, 'dialog', 'en-us', '*.dialog')
    best = (None, 0)
    for path in glob(dialog_paths):
        patterns = load_dialog_file(path)
        match, _ = _match_dialog_patterns(patterns, sentence.lower())
        if match is not False:
            if len(patterns[match]) > best[1]:
                best = (path, len(patterns[match]))
    if best[0] is not None:
        return basename(best[0])
    else:
        return None


def _match_dialog_patterns(dialogs, sentence):
    """Match sentence against a list of dialog patterns.

    Returns index of found match.
    """
    # Allow custom fields to be anything
    d = [re.sub(r'{.*?\}', r'.*', t) for t in dialogs]
    # Remove left over '}'
    d = [re.sub(r'\}', r'', t) for t in d]
    d = [re.sub(r' .* ', r' .*', t) for t in d]
    # Merge consequtive .*'s into a single .*
    d = [re.sub(r'\.\*( \.\*)+', r'.*', t) for t in d]
    # Remove double whitespaces
    d = ['^' + ' '.join(t.split()) for t in d]
    debug = 'MATCHING: {}\n'.format(sentence)
    for i, r in enumerate(d):
        match = re.match(r, sentence)
        debug += '---------------\n'
        debug += '{} {}\n'.format(r, match is not None)
        if match:
            return i, debug
    else:
        return False, debug


@given('an english speaking user')
def given_english(context):
    context.lang = 'en-us'


@when('the user says "{text}"')
def when_user_says(context, text):
    context.bus.emit(Message('recognizer_loop:utterance',
                             data={'utterances': [text],
                                   'lang': 'en-us',
                                   'session': '',
                                   'ident': time.time()},
                             context={'client_name': 'mycroft_listener'}))


def then_wait(msg_type, then_func, context, timeout=TIMEOUT):
    count = 0
    debug = ''
    while count < int(timeout * (1 / SLEEP_LENGTH)):
        for message in context.bus.get_messages(msg_type):
            status, test_dbg = then_func(message)
            debug += test_dbg
            if status:
                context.matched_message = message
                context.bus.remove_message(message)
                return True, debug
        time.sleep(SLEEP_LENGTH)
        count += 1
    # Timed out return debug from test
    return False, debug


def mycroft_responses(context):
    responses = ''
    messages = context.bus.get_messages('speak')
    if len(messages) > 0:
        responses = 'Mycroft responded with:\n'
        for m in messages:
            responses += 'Mycroft: '
            if 'dialog' in m.data['meta']:
                responses += '{}.dialog'.format(m.data['meta']['dialog'])
            responses += '({})\n'.format(m.data['meta'].get('skill'))
            responses += '"{}"\n'.format(m.data['utterance'])
    return responses


def print_mycroft_responses(context):
    print(mycroft_responses(context))


@then('"{skill}" should reply with dialog from "{dialog}"')
def then_dialog(context, skill, dialog):
    def check_dialog(message):
        utt_dialog = message.data.get('meta', {}).get('dialog')
        return (utt_dialog == dialog.replace('.dialog', ''), '')

    passed, debug = then_wait('speak', check_dialog, context)
    if not passed:
        assert_msg = debug
        assert_msg += mycroft_responses(context)

    assert passed, assert_msg or 'Mycroft didn\'t respond'


@then('"{skill}" should reply with "{example}"')
def then_example(context, skill, example):
    skill_path = context.msm.find_skill(skill).path
    dialog = dialog_from_sentence(example, skill_path)
    print('Matching with the dialog file: {}'.format(dialog))
    assert dialog is not None, 'No matching dialog...'
    then_dialog(context, skill, dialog)


@then('"{skill}" should reply with anything')
def then_anything(context, skill):
    def check_any_messages(message):
        return (True, '') if message else (False, '')

    passed = then_wait('speak', check_any_messages, context)
    assert passed, 'No speech received at all'


@then('"{skill}" should reply with exactly "{text}"')
def then_exactly(context, skill, text):
    def check_exact_match(message):
        utt = message.data['utterance'].lower()
        debug = 'Comparing {} with expected {}\n'.format(utt, text)
        return (True, debug) if utt == text.lower() else (False, debug)

    passed, debug = then_wait('speak', check_exact_match, context)
    if not passed:
        assert_msg = debug
        assert_msg += mycroft_responses(context)
    assert passed, assert_msg


@then('mycroft reply should contain "{text}"')
def then_contains(context, text):
    def check_contains(message):
        utt = message.data['utterance'].lower()
        debug = 'Checking if "{}" contains "{}"\n'.format(utt, text)
        return (True, debug) if text.lower() in utt else (False, debug)

    passed, debug = then_wait('speak', check_contains, context)

    if not passed:
        assert_msg = 'No speech contained the expected content'
        assert_msg += mycroft_responses(context)

    assert passed, assert_msg


@then('the user replies with "{text}"')
@then('the user replies "{text}"')
@then('the user says "{text}"')
def then_user_follow_up(context, text):
    time.sleep(2)
    wait_while_speaking()
    context.bus.emit(Message('recognizer_loop:utterance',
                             data={'utterances': [text],
                                   'lang': 'en-us',
                                   'session': '',
                                   'ident': time.time()},
                             context={'client_name': 'mycroft_listener'}))


@then('mycroft should send the message "{message_type}"')
def then_messagebus_message(context, message_type):
    cnt = 0
    while context.bus.get_messages(message_type) == []:
        if cnt > int(TIMEOUT * (1.0 / SLEEP_LENGTH)):
            assert False, "Message not found"
            break
        else:
            cnt += 1

        time.sleep(SLEEP_LENGTH)
