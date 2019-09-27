# Copyright 2019 Mycroft AI Inc.
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
"""Periodically run by skill manager to load skills into memory."""
import gc
import importlib
import os
from os.path import dirname
import sys
from time import time
from types import ModuleType

from mycroft.configuration import Configuration
from mycroft.messagebus import Message
from mycroft.util.log import LOG
from .settings import SettingsMetaUploader

SKILL_MAIN_MODULE = '__init__.py'


def module_has_been_loaded(module_name):
    """Check if a module has been loaded.

    The check is done by checking if the name is included in the sys.modules
    dict.

    Arguments:
        module_name (str): name identifying the module.

    Returns (bool): True if the module has been loaded once already.
    """
    return module_name in sys.modules


def reload_related_modules(module_name, found=[]):
    """Reload modules located in the same folder as main module.

    This function recurses through the modules in the same folder as the
    first supplied module and recurse through modules from the same folder

    Arguments:
        module_name (str): name of the module as set in sys.modules
    """
    module = sys.modules[module_name]
    module_dir = dirname(module.__file__)

    # Find all imported modules with associated files
    modules = [i for _, i in module.__dict__.items()
               if isinstance(i, ModuleType) and '__file__' in dir(i)]
    # modules in the skill's folder are consider relatives
    relatives = [m for m in modules if m.__file__.startswith(module_dir)]
    relatives = [r for r in relatives if r not in found]

    # Reload them
    for r in relatives:
        reload_related_modules(r.__name__, relatives + found)
        LOG.debug('Reloading {}'.format(r.__name__))
        importlib.reload(r)
        sys.modules[r.__name__] = r


def load_skill_module(path, skill_id):
    """Load a skill module

    This function handles the differences between python 3.4 and 3.5+ as well
    as makes sure the module is inserted into the sys.modules dict.

    Arguments:
        path: Path to the skill main file (__init__.py)
        skill_id: skill_id used as skill identifier in the module list
    """
    module_name = skill_id.replace('.', '_')
    from importlib.machinery import SourceFileLoader

    if module_has_been_loaded(module_name):
        reload_related_modules(module_name)
    mod = SourceFileLoader(module_name, path).load_module(name=module_name)
    return mod


def _get_last_modified_time(path):
    """Get the last modified date of the most recently updated file in a path.

    Exclude compiled python files, hidden directories and the settings.json
    file.

    Arguments:
        path: skill directory to check

    Returns:
        int: time of last change
    """
    all_files = []
    for root_dir, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for f in files:
            ignore_file = (
                    f.endswith('.pyc') or
                    f == 'settings.json' or
                    f.startswith('.') or
                    f.endswith('.qmlc')
            )
            if not ignore_file:
                all_files.append(os.path.join(root_dir, f))

    # check files of interest in the skill root directory
    return max(os.path.getmtime(f) for f in all_files)


class SkillLoader:
    def __init__(self, bus, skill_directory):
        self.bus = bus
        self.skill_directory = skill_directory
        self.skill_id = os.path.basename(skill_directory)
        self.load_attempted = False
        self.loaded = False
        self.last_modified = 0
        self.last_loaded = 0
        self.instance = None
        self.active = True

    @property
    def config(self):
        return Configuration.get()

    @property
    def is_blacklisted(self):
        """Boolean value representing whether or not a skill is blacklisted."""
        blacklist = self.config['skills'].get('blacklisted_skills', [])
        if self.skill_id in blacklist:
            return True
        else:
            return False

    def reload_needed(self):
        """Load an unloaded skill or reload unloaded/changed skill.

        Returns:
             bool: if the skill was loaded/reloaded
        """
        try:
            self.last_modified = _get_last_modified_time(self.skill_directory)
        except FileNotFoundError as e:
            LOG.error('Failed to get last_modification time '
                      '({})'.format(repr(e)))
            self.last_modified = self.last_loaded

        modified = self.last_modified > self.last_loaded

        # create local reference to avoid threading issues
        instance = self.instance

        reload_allowed = (
                self.active and
                (instance is None or instance.reload_skill)
        )
        return modified and reload_allowed

    def reload(self):
        LOG.info('ATTEMPTING TO RELOAD SKILL: ' + self.skill_id)
        if self.instance:
            self._unload()
        self._load()

    def load(self):
        LOG.info('ATTEMPTING TO LOAD SKILL: ' + self.skill_id)
        self._load()

    def _unload(self):
        """Remove listeners and stop threads before loading"""
        self._execute_instance_shutdown()
        if self.config.get("debug", False):
            self._garbage_collect()
        self.loaded = False
        self._emit_skill_shutdown_event()

    def unload(self):
        if self.instance:
            self._execute_instance_shutdown()
        self.loaded = False

    def activate(self):
        self.active = True
        self.load()

    def deactivate(self):
        self.active = False
        self.unload()

    def _execute_instance_shutdown(self):
        """Call the shutdown method of the skill being reloaded."""
        try:
            self.instance.default_shutdown()
        except Exception:
            log_msg = 'An error occurred while shutting down {}'
            LOG.exception(log_msg.format(self.instance.name))
        else:
            LOG.info('Skill {} shut down successfully'.format(self.skill_id))

    def _garbage_collect(self):
        """Invoke Python garbage collector to remove false references"""
        gc.collect()
        # Remove two local references that are known
        refs = sys.getrefcount(self.instance) - 2
        if refs > 0:
            log_msg = (
                "After shutdown of {} there are still {} references "
                "remaining. The skill won't be cleaned from memory."
            )
            LOG.warning(log_msg.format(self.instance.name, refs))

    def _emit_skill_shutdown_event(self):
        message = Message(
            "mycroft.skills.shutdown",
            data=dict(path=self.skill_directory, id=self.skill_id)
        )
        self.bus.emit(message)

    def _load(self):
        self._prepare_for_load()
        if self.is_blacklisted:
            self._skip_load()
        else:
            skill_module = self._load_skill_source()
            if skill_module and self._create_skill_instance(skill_module):
                self._check_for_first_run()
                self.loaded = True

        self.last_loaded = time()
        self._communicate_load_status()
        self._upload_settings_meta()

    def _prepare_for_load(self):
        self.load_attempted = True
        self.loaded = False
        self.instance = None

    def _skip_load(self):
        log_msg = 'Skill {} is blacklisted - it will not be loaded'
        LOG.info(log_msg.format(self.skill_id))

    def _load_skill_source(self):
        """Use Python's import library to load a skill's source code."""
        main_file_path = os.path.join(self.skill_directory, SKILL_MAIN_MODULE)
        if not os.path.exists(main_file_path):
            error_msg = 'Failed to load {} due to a missing file.'
            LOG.error(error_msg.format(self.skill_id))
        else:
            try:
                skill_module = load_skill_module(main_file_path, self.skill_id)
            except Exception as e:
                LOG.exception('Failed to load skill: '
                              '{} ({})'.format(self.skill_id, repr(e)))
            else:
                module_is_skill = (
                    hasattr(skill_module, 'create_skill') and
                    callable(skill_module.create_skill)
                )
                if module_is_skill:
                    return skill_module
        return None  # Module wasn't loaded

    def _create_skill_instance(self, skill_module):
        """Use v2 skills framework to create the skill."""
        try:
            self.instance = skill_module.create_skill()
        except Exception as e:
            log_msg = 'Skill __init__ failed with {}'
            LOG.exception(log_msg.format(repr(e)))
            self.instance = None

        if self.instance:
            self.instance.skill_id = self.skill_id
            self.instance.bind(self.bus)
            try:
                self.instance.load_data_files()
                # Set up intent handlers
                # TODO: can this be a public method?
                self.instance._register_decorated()
                self.instance.register_resting_screen()
                self.instance.initialize()
            except Exception as e:
                # If an exception occurs, make sure to clean up the skill
                self.instance.default_shutdown()
                self.instance = None
                log_msg = 'Skill initialization failed with {}'
                LOG.exception(log_msg.format(repr(e)))

        return self.instance is not None

    def _check_for_first_run(self):
        """The very first time a skill is run, speak the intro."""
        first_run = self.instance.settings.get(
            "__mycroft_skill_firstrun",
            True
        )
        if first_run:
            LOG.info("First run of " + self.skill_id)
            self.instance.settings["__mycroft_skill_firstrun"] = False
            self.instance.settings.store()
            intro = self.instance.get_intro_message()
            if intro:
                self.instance.speak(intro)

    def _communicate_load_status(self):
        if self.loaded:
            message = Message(
                'mycroft.skills.loaded',
                data=dict(
                    path=self.skill_directory,
                    id=self.skill_id,
                    name=self.instance.name,
                    modified=self.last_modified
                )
            )
            self.bus.emit(message)
            LOG.info('Skill {} loaded successfully'.format(self.skill_id))
        else:
            message = Message(
                'mycroft.skills.loading_failure',
                data=dict(path=self.skill_directory, id=self.skill_id)
            )
            self.bus.emit(message)
            LOG.error('Skill {} failed to load'.format(self.skill_id))

    def _upload_settings_meta(self):
        if self.loaded:
            settings_meta = SettingsMetaUploader(
                self.skill_directory,
                self.instance.name
            )
            settings_meta.upload()
            self.instance.settings_meta = settings_meta
